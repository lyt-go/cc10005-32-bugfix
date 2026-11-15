"""采集器健康业务规则：状态流转、字段校验与筛选口径都收在这里。

状态机（只允许单向推进，禁止回退）：
    草稿 → 报告中 → 待确认 → 已确认 → 已归档
其中「待确认」有两个来源：
1. 报告确认时指标不完整，不能直接确认，先落待确认，补齐后再确认；
2. 已确认报告的采样窗口不再覆盖当前时间，惰性过期退回待确认。
"""
from __future__ import annotations

from typing import Any

from app.services import linkage
from app.store import store

MODULE = "healthcheck"
REQUIRED_FIELDS = ["报告编号", "采集器名称", "CPU占用率"]
STATUS_ORDER = ["草稿", "报告中", "待确认", "已确认", "已归档"]
# 动作与目标状态；每个动作允许的来源状态单独列清楚，不再允许任意状态互相跳转。
ACTION_RULES = {
    "生成报告": {"target": "报告中", "from": ("草稿",)},
    "确认报告": {"target": "已确认", "from": ("报告中", "待确认")},
    "归档报告": {"target": "已归档", "from": ("报告中", "待确认", "已确认")},
}
NEGATIVE_ACTIONS = []


class HealthcheckService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.locked():
            # 列表读取时顺手做一次惰性过期，保证导航页与详情页看到的状态一致。
            linkage.expire_stale_reports()
            rows = store.rows(MODULE)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
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
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 联动指标允许登记时一并带上；不带则为空，确认环节会强制补齐校验。
            for field in ("内存占用MB", "网络IO速率", "活跃连接数", "错误队列长度", "检查时间戳",
                          "采样窗口开始", "采样窗口结束", "CPU峰值"):
                if field in values:
                    entry[field] = values.get(field)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return dict(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集器健康可执行范围"
        with store.locked():
            # 任何动作前先处理过期：已确认但窗口失效的报告必须先退回待确认。
            linkage.expire_stale_reports()
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"健康报告 {entry_id} 不存在或已归档"
            rule = ACTION_RULES[action]
            target = rule["target"]
            current = str(entry.get("status"))
            if current == target:
                return None, f"健康报告已是「{target}」，重复{action}不生效"
            if current not in rule["from"]:
                current_idx = STATUS_ORDER.index(current) if current in STATUS_ORDER else -1
                target_idx = STATUS_ORDER.index(target)
                hint = "状态只能单向推进，不允许回退" if target_idx <= current_idx else "请先完成前置状态"
                return None, f"健康报告当前为「{current}」，不能执行{action}（{hint}）"

            if action == "确认报告":
                missing = linkage.missing_metrics(entry)
                if missing:
                    # 指标不完整：落「待确认」并保持当前档位相关方不动，拒绝直接确认。
                    entry["status"] = "待确认"
                    entry["pending"] = True
                    entry["abnormal"] = True
                    linkage._append_audit(
                        action="健康报告-确认驳回",
                        target=str(entry.get("报告编号")),
                        detail=f"缺少 {'、'.join(missing)}，退回待确认，限速恢复不得引用该报告",
                        result="驳回",
                    )
                    return dict(entry), (
                        f"健康报告缺少 {'、'.join(missing)}，已退回待确认；"
                        "采样窗口、CPU峰值和错误队列长度补齐前不得用于限速恢复"
                    )

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            linkage._append_audit(
                action=f"健康报告-{action}",
                target=str(entry.get("报告编号")),
                detail=f"状态由「{current}」推进为「{target}」",
                result="成功",
            )
            return dict(entry), f"健康报告已{action}"
