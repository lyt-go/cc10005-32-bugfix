"""动态限速业务规则：状态流转、健康联动、降档/恢复与决策留痕。

状态机（只允许单向推进，禁止回退）：
    草稿 → 规则评审中 → 已启用 → 已关闭

「已启用」内部还有一条只降不回退的档位链路：
    高速度（当前速率=最大速率）→ 限速降档 → 低速度 → 恢复速率 → 高速度
恢复速率不是状态回退，而是同一「已启用」状态下带版本号的档位恢复，且：
- 必须引用「已确认、覆盖当前窗口、指标完整、错误队列已清零」的健康报告；
- 降档、健康校验、速率写入、决策与审计在同一把锁内原子完成；
- 每个降档版本只能恢复一次，重复提交只留痕不生效；
- 多份报告窗口冲突时，只放行最早满足完整条件的那份，两份报告都保留供审计。
"""
from __future__ import annotations

from typing import Any

from app.services import linkage
from app.store import store

MODULE = "throttle"
REQUIRED_FIELDS = ["规则编号", "目标域组", "初始速率"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已关闭"]
# 动作 -> 目标状态与允许的来源状态；来源不匹配一律拒绝，彻底堵住回退与跳级。
ACTION_RULES = {
    "提交规则评审": {"target": "规则评审中", "from": ("草稿",)},
    "启用规则": {"target": "已启用", "from": ("规则评审中",)},
    "限速降档": {"target": "已启用", "from": ("已启用",)},
    "恢复速率": {"target": "已启用", "from": ("已启用",)},
    "关闭规则": {"target": "已关闭", "from": ("草稿", "规则评审中", "已启用")},
}


def _as_rate(value: Any) -> int | None:
    rate = linkage.to_int(value)
    return rate if rate is not None and rate >= 0 else None


class ThrottleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.locked():
            linkage.expire_stale_reports()
            rows = store.rows(MODULE)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [dict(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.locked():
            linkage.expire_stale_reports()
            entry = store.find(MODULE, entry_id)
            return dict(entry) if entry else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.locked():
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in ("最大速率", "加速因子", "减速因子", "错误率阈值", "降速恢复间隔"):
                if field in values:
                    entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            # 档位与恢复联动的簿记字段：版本号从 0 开始，恢复过的版本用于幂等判断。
            entry["当前速率"] = _as_rate(values.get("初始速率"))
            entry["降档速率"] = None
            entry["降档版本"] = 0
            entry["已恢复版本"] = 0
            entry["降档时错误队列"] = None
            entry["恢复引用报告"] = None
            rows.append(entry)
            return dict(entry), []

    # ------------------------------------------------------------------
    # 动作入口
    # ------------------------------------------------------------------
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于动态限速可执行范围"
        with store.locked():
            # 读健康状态前先做惰性过期，已确认但窗口失效的报告先退回待确认。
            linkage.expire_stale_reports()
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"限速规则 {entry_id} 不存在或已归档"
            rule = ACTION_RULES[action]
            target, current = rule["target"], str(entry.get("status"))
            if current == "已关闭":
                return self._reject(entry, action, "限速规则已关闭，不允许再执行任何状态动作")
            if current == target and action not in ("限速降档", "恢复速率"):
                return self._reject(entry, action, f"限速规则已是「{target}」，重复提交不生效")
            if current not in rule["from"]:
                current_idx = STATUS_ORDER.index(current) if current in STATUS_ORDER else -1
                target_idx = STATUS_ORDER.index(target)
                hint = "状态只能按评审、启用、关闭单向推进，不允许回退" if target_idx <= current_idx else "请先完成前置状态"
                return self._reject(entry, action, f"限速规则当前为「{current}」，不能执行{action}（{hint}）")

            if action == "提交规则评审":
                entry["status"] = target
                entry["pending"] = True
                linkage._append_audit(
                    action="动态限速-提交规则评审",
                    target=str(entry.get("规则编号")),
                    detail="状态由「草稿」推进为「规则评审中」",
                    result="成功",
                )
                return dict(entry), "限速规则已提交规则评审"

            if action == "关闭规则":
                rate_before = entry.get("当前速率")
                entry["status"] = "已关闭"
                entry["pending"] = False
                linkage.append_decision(
                    rule=entry, action=action, result="成功",
                    detail="规则关闭，速率决策停止下发",
                    rate_before=rate_before, rate_after=entry.get("当前速率"),
                )
                return dict(entry), "限速规则已关闭"

            if action == "启用规则":
                return self._enable(entry, values)

            if action == "限速降档":
                return self._downshift(entry, values)

            # 恢复速率：书签/上传/导入与页面动作共用同一条原子链路。
            return self._recover(entry, values)

    # ------------------------------------------------------------------
    # 启用：评审中 -> 已启用（高速度），同样需要健康报告背书
    # ------------------------------------------------------------------
    def _enable(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        report_id = linkage.to_int(values.get("健康报告id"))
        report, reason = linkage.pick_recovery_report(report_id)
        if report is None:
            return self._reject(entry, "启用规则", f"启用被拦截：{reason}")
        high = _as_rate(entry.get("最大速率")) or _as_rate(entry.get("初始速率"))
        if high is None:
            return self._reject(entry, "启用规则", "规则缺少可用的最大速率/初始速率，无法写入速率决策")
        rate_before = entry.get("当前速率")
        entry["status"] = "已启用"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["当前速率"] = high
        entry["恢复引用报告"] = report.get("报告编号")
        linkage.append_decision(
            rule=entry, action="启用规则", result="成功",
            detail=(
                f"引用已确认报告 {report.get('报告编号')}（检查时间 {report.get('检查时间戳')}，"
                f"错误队列长度 {report.get('错误队列长度')}）启用高速度"
            ),
            report=report, rate_before=rate_before, rate_after=high,
        )
        return dict(entry), f"限速规则已启用，当前速率写入 {high}"

    # ------------------------------------------------------------------
    # 降档：已启用高速度 -> 低速度，版本号 +1，原子写入
    # ------------------------------------------------------------------
    def _downshift(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        current_rate = _as_rate(entry.get("当前速率"))
        if current_rate is None:
            return self._reject(entry, "限速降档", "当前速率缺失，无法降档")
        low = _as_rate(values.get("降档速率"))
        if low is None:
            initial = _as_rate(entry.get("初始速率"))
            low = initial if (initial is not None and initial < current_rate) else max(current_rate // 2, 0)
        if low >= current_rate:
            return self._reject(
                entry, "限速降档",
                f"降档速率 {low} 不低于当前速率 {current_rate}，拒绝写入，限速保持当前档位",
            )
        queue_len = linkage.to_int(values.get("错误队列长度"))
        version = int(entry.get("降档版本", 0)) + 1
        entry["降档版本"] = version
        entry["已恢复版本"] = int(entry.get("已恢复版本", 0))
        entry["降档速率"] = low
        entry["当前速率"] = low
        entry["abnormal"] = True
        entry["pending"] = True
        entry["降档时错误队列"] = queue_len
        linkage.append_decision(
            rule=entry, action="限速降档", result="成功",
            detail=f"降档版本 v{version}，速率 {current_rate} -> {low}，降档时错误队列长度={queue_len}",
            rate_before=current_rate, rate_after=low,
        )
        return dict(entry), f"已降档至 {low}（降档版本 v{version}），恢复前必须引用当前窗口已确认的健康报告"

    # ------------------------------------------------------------------
    # 恢复：低速度 -> 高速度，报告背书 + 幂等 + 最早完整版本仲裁，全原子
    # ------------------------------------------------------------------
    def _recover(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        version = int(entry.get("降档版本", 0))
        recovered = int(entry.get("已恢复版本", 0))
        if version == 0:
            return self._reject(entry, "恢复速率", "规则尚未经历降档，不存在可恢复的降档版本，限速保持当前档位")
        if recovered >= version:
            return self._reject(
                entry, "恢复速率",
                f"降档版本 v{version} 已恢复过，重复恢复只生效一次，速率决策保持不变",
            )
        report_id = linkage.to_int(values.get("健康报告id"))
        report, reason = linkage.pick_recovery_report(report_id)
        if report is None:
            return self._reject(entry, "恢复速率", f"恢复被拦截：{reason}")
        high = _as_rate(entry.get("最大速率")) or _as_rate(entry.get("初始速率"))
        if high is None:
            return self._reject(entry, "恢复速率", "规则缺少可用的最大速率/初始速率，无法写入速率决策")
        rate_before = entry.get("当前速率")
        # 关键写入集中在最后一步：前面全部是校验，任一项不满足都不会改速率。
        entry["当前速率"] = high
        entry["降档速率"] = None
        entry["abnormal"] = False
        entry["pending"] = False
        entry["已恢复版本"] = version
        entry["恢复引用报告"] = report.get("报告编号")
        decision = linkage.append_decision(
            rule=entry, action="恢复速率", result="成功",
            detail=(
                f"降档版本 v{version} 恢复完成，引用报告 {report.get('报告编号')}，"
                f"检查时间 {report.get('检查时间戳')}，错误队列长度 {report.get('错误队列长度')}，"
                f"速率 {rate_before} -> {high}"
            ),
            report=report, rate_before=rate_before, rate_after=high,
        )
        entry["最近决策id"] = decision["id"]
        return dict(entry), f"降档版本 v{version} 已恢复，当前速率写入 {high}"

    def _reject(self, entry: dict[str, Any], action: str, reason: str) -> tuple[dict[str, Any] | None, str]:
        """被拦截的动作也写决策与审计：禁止只更新速率不记录决策，也禁止静默放行。"""
        linkage.append_decision(
            rule=entry, action=action, result="拦截",
            detail=reason,
            rate_before=entry.get("当前速率"), rate_after=entry.get("当前速率"),
        )
        return None, reason
