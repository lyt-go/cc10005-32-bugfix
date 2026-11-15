"""限速恢复凭证：书签、上传文件、导入文件三种通道共用一套处理链路。

设计目标（对应线上事故的复核要求）：
- 三种通道提交的都是同一份“恢复意图”载荷，后端解析后走与页面按钮完全相同的
  原子状态机，未确认健康报告、错误队列未放行、状态回退等场景一律拦不住地被拒绝；
- 原始内容按 SHA-256 去重并原样留存（保留原文件版本，不覆盖、不改写）；
- 同一载荷重复提交/回读重放时，速率决策保持不变，不会二次恢复；
- 每次提交（无论成功还是拦截）都留速率决策与操作审计，禁止只更新速率不记录决策。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services import linkage
from app.services.throttle import ThrottleService
from app.store import store

MODULE = "recovery_artifact"
CHANNELS = ("bookmark", "upload", "import")


class RecoveryService:
    def list_artifacts(self, *, rule_no: str | None = None) -> list[dict[str, Any]]:
        with store.locked():
            rows = store.rows(MODULE)
            if rule_no:
                rows = [row for row in rows if row.get("规则编号") == rule_no]
            return [dict(row) for row in rows]

    def get_artifact(self, artifact_id: int) -> dict[str, Any] | None:
        with store.locked():
            row = store.find(MODULE, artifact_id)
            return dict(row) if row else None

    def submit(
        self,
        *,
        channel: str,
        raw: bytes,
        filename: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if channel not in CHANNELS:
            return None, f"不支持的恢复通道「{channel}」，仅支持书签、上传、导入"
        if not raw or not raw.strip():
            return None, "恢复凭证内容为空，未写入任何速率决策"
        digest = hashlib.sha256(raw).hexdigest()
        with store.locked():
            # 同通道同内容重放：直接回读既有版本，保持速率决策不变（重复恢复只生效一次）。
            # 去重按“通道 + 内容哈希”判定：书签/上传/导入是三条独立核对通道，
            # 各自保留自己的文件版本，互不覆盖。
            for existed in store.rows(MODULE):
                if existed.get("内容哈希") == digest and existed.get("通道") == channel:
                    return dict(existed), f"凭证已存在（版本 v{existed.get('版本')}），回读不改变速率决策"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return None, f"恢复凭证不是合法 JSON（{exc}），未写入任何速率决策"
            if not isinstance(payload, dict):
                return None, "恢复凭证必须是 JSON 对象，未写入任何速率决策"

            rule_no = str(payload.get("规则编号") or "").strip()
            rule = self._find_rule(rule_no)
            if rule is None:
                return None, f"限速规则 {rule_no or '(空)'} 不存在，未写入任何速率决策"

            artifact_id = store.next_id(MODULE)
            version = sum(1 for row in store.rows(MODULE) if row.get("规则编号") == rule_no) + 1
            artifact: dict[str, Any] = {
                "id": artifact_id,
                "凭证编号": f"RCV-{artifact_id:04d}",
                "通道": channel,
                "文件名": filename,
                "规则编号": rule_no,
                "健康报告编号": str(payload.get("健康报告编号") or ""),
                "版本": version,
                "内容哈希": digest,
                "原始内容": raw.decode("utf-8"),
                "提交时间": linkage.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            report = self._find_report(str(payload.get("健康报告编号") or ""))
            values: dict[str, Any] = {}
            if report is not None:
                values["健康报告id"] = report.get("id")
            if "降档速率" in payload:
                values["降档速率"] = payload.get("降档速率")
            if "错误队列长度" in payload:
                values["错误队列长度"] = payload.get("错误队列长度")

            # 与页面按钮同一个服务入口，保证三条通道的判定口径完全一致。
            entry, message = ThrottleService().run_action(int(rule["id"]), "恢复速率", values)
            artifact["处理结果"] = "成功" if entry is not None else "拦截"
            artifact["处理说明"] = message
            artifact["处理后状态"] = entry.get("status") if entry else rule.get("status")
            artifact["处理后速率"] = entry.get("当前速率") if entry else rule.get("当前速率")
            store.rows(MODULE).append(artifact)
            return dict(artifact), message

    def replay(self, artifact_id: int) -> tuple[dict[str, Any] | None, str]:
        """回读重放：用留存的原始内容再走一遍链路，验证速率决策不发生变化。"""
        with store.locked():
            artifact = store.find(MODULE, artifact_id)
            if artifact is None:
                return None, f"恢复凭证 {artifact_id} 不存在"
            before_rate = self._find_rule(str(artifact.get("规则编号")))
            before = before_rate.get("当前速率") if before_rate else None
            # 相同字节必然命中哈希去重，返回同一凭证；这里显式再提交一次做验证。
            replayed, message = self.submit(
                channel=str(artifact.get("通道")),
                raw=str(artifact.get("原始内容")).encode("utf-8"),
                filename=artifact.get("文件名"),
            )
            after_row = self._find_rule(str(artifact.get("规则编号")))
            after = after_row.get("当前速率") if after_row else None
            if before != after:
                return None, f"回读前后速率决策发生变化（{before} -> {after}），凭证重放失败"
            return replayed, f"回读校验通过，速率决策保持 {before} 不变：{message}"

    # ------------------------------------------------------------------
    def _find_rule(self, rule_no: str) -> dict[str, Any] | None:
        if not rule_no:
            return None
        for row in store.rows("throttle"):
            if row.get("规则编号") == rule_no:
                return row
        return None

    def _find_report(self, report_no: str) -> dict[str, Any] | None:
        if not report_no:
            return None
        for row in store.rows("healthcheck"):
            if row.get("报告编号") == report_no:
                return row
        return None
