"""健康报告与动态限速联动的共享领域规则。

这里只放“怎么判断、怎么记”的纯规则，状态写入由两个 service 在 ``store.locked()``
内调用，保证降档、健康确认、速率写入、审计与决策记录在同一把锁内原子完成。

关键约定：
- 健康报告必须带齐 采样窗口（起止时间）、CPU 峰值、错误队列长度 才允许确认；
- 已确认报告只在自身采样窗口覆盖当前时间时有效，过期后退回「待确认」；
- 限速恢复只能引用“已确认 + 覆盖当前窗口 + 指标完整 + 错误队列已放行”的报告；
- 同一规则存在多份可用报告时，只放行最早满足完整条件（检查时间最早）的那份；
- 任何速率变更都必须留速率决策记录与操作审计，禁止只改速率不记决策。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

# 健康报告确认与恢复引用必须齐备的指标项，缺任一项一律保持当前档位/状态。
REPORT_REQUIRED_METRICS = ("采样窗口开始", "采样窗口结束", "CPU峰值", "错误队列长度")

# 报告确认后有效的时间窗口长度：检查时间之后 2 小时内可被限速恢复引用。
REPORT_VALIDITY_HOURS = 2


def now() -> datetime:
    """集中取当前时间，测试里 monkeypatch 本函数即可固定时间。"""
    return datetime.now()


def parse_dt(value: Any) -> datetime | None:
    """尽量把字符串/时间值解析成 datetime；解析不了返回 None，调用方按缺失处理。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def to_int(value: Any) -> int | None:
    """错误队列长度等数值字段统一走这里；非数字一律视为缺失，不猜测默认值。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def missing_metrics(report: dict[str, Any]) -> list[str]:
    """返回报告缺失的关键指标；空列表表示指标完整。"""
    missing: list[str] = []
    for field in REPORT_REQUIRED_METRICS:
        value = report.get(field)
        if value is None or str(value).strip() == "":
            missing.append(field)
    return missing


def report_window(report: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    return parse_dt(report.get("采样窗口开始")), parse_dt(report.get("采样窗口结束"))


def report_covers_now(report: dict[str, Any], moment: datetime | None = None) -> bool:
    """报告的采样窗口是否覆盖当前时间；窗口不可解析视为不覆盖。"""
    moment = moment or now()
    start, end = report_window(report)
    if start is None or end is None:
        return False
    return start <= moment <= end


def report_is_current(report: dict[str, Any], moment: datetime | None = None) -> bool:
    """已确认报告是否仍可被恢复引用：窗口覆盖当前时间，且检查时间未超有效期。"""
    moment = moment or now()
    if not report_covers_now(report, moment):
        return False
    checked_at = parse_dt(report.get("检查时间戳"))
    if checked_at is None:
        return False
    age_hours = (moment - checked_at).total_seconds() / 3600
    return 0 <= age_hours <= REPORT_VALIDITY_HOURS


def _append_audit(
    *,
    action: str,
    target: str,
    detail: str,
    result: str,
    operator: str = "系统联动",
) -> dict[str, Any]:
    rows = store.rows("audittrail")
    next_id = store.next_id("audittrail")
    entry = {
        "id": next_id,
        "status": "已记录",
        "pending": False,
        "abnormal": result != "成功",
        "记录编号": f"AUDI-LINK-{next_id:04d}",
        "操作类型": action,
        "操作对象": target,
        "操作人": operator,
        "操作时间": now().strftime("%Y-%m-%d %H:%M:%S"),
        "操作IP": "127.0.0.1",
        "变更详情": detail,
        "操作结果": result,
    }
    rows.append(entry)
    return entry


def append_decision(
    *,
    rule: dict[str, Any],
    action: str,
    result: str,
    detail: str,
    report: dict[str, Any] | None = None,
    rate_before: Any = None,
    rate_after: Any = None,
) -> dict[str, Any]:
    """每次限速动作都写一条速率决策；被拦截的动作也要留痕，禁止静默放行。"""
    rows = store.rows("rate_decision")
    decision = {
        "id": store.next_id("rate_decision"),
        "规则编号": rule.get("规则编号"),
        "动作": action,
        "结果": result,
        "详情": detail,
        "引用健康报告": report.get("报告编号") if report else None,
        "报告检查时间": report.get("检查时间戳") if report else None,
        "报告错误队列长度": report.get("错误队列长度") if report else None,
        "变更前速率": rate_before,
        "变更后速率": rate_after,
        "决策时间": now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    rows.append(decision)
    _append_audit(
        action=f"动态限速-{action}",
        target=str(rule.get("规则编号")),
        detail=detail,
        result=result,
    )
    return decision


def expire_stale_reports(moment: datetime | None = None) -> list[dict[str, Any]]:
    """惰性过期：已确认但窗口不再覆盖当前时间的报告退回「待确认」，原报告保留。

    退回而不是删除：过期报告仍是审计依据，重新确认会生成新的确认记录而不是覆盖。
    """
    moment = moment or now()
    expired: list[dict[str, Any]] = []
    for report in store.rows("healthcheck"):
        if report.get("status") != "已确认":
            continue
        if not report_covers_now(report, moment):
            report["status"] = "待确认"
            report["pending"] = True
            report["abnormal"] = True
            expired.append(report)
            _append_audit(
                action="健康报告-过期退回",
                target=str(report.get("报告编号")),
                detail=f"采样窗口 {report.get('采样窗口开始')} ~ {report.get('采样窗口结束')} 已不覆盖当前时间，退回待确认",
                result="成功",
            )
    return expired


def find_eligible_reports(
    *,
    moment: datetime | None = None,
) -> list[dict[str, Any]]:
    """列出当前可用于限速恢复的报告：已确认、指标完整、窗口覆盖当前时间且未超期。"""
    moment = moment or now()
    eligible: list[dict[str, Any]] = []
    for report in store.rows("healthcheck"):
        if report.get("status") != "已确认":
            continue
        if missing_metrics(report):
            continue
        if not report_is_current(report, moment):
            continue
        eligible.append(report)
    # 最早满足完整条件：检查时间最早者优先，同时间按 id 兜底保证顺序稳定。
    eligible.sort(key=lambda item: (parse_dt(item.get("检查时间戳")) or now(), int(item.get("id", 0))))
    return eligible


def pick_recovery_report(
    referenced_id: int | None,
    *,
    moment: datetime | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """恢复时校验引用的报告，并在多窗口冲突时只放行最早满足完整条件的版本。"""
    moment = moment or now()
    eligible = find_eligible_reports(moment=moment)
    if not eligible:
        return None, "当前没有已确认且覆盖当前窗口、指标完整的健康报告，限速保持当前档位"
    earliest = eligible[0]
    if referenced_id is None:
        return earliest, ""
    referenced = store.find("healthcheck", referenced_id)
    if referenced is None:
        return None, f"健康报告 {referenced_id} 不存在或已归档，无法作为恢复依据"
    if referenced.get("status") != "已确认":
        return None, f"健康报告 {referenced.get('报告编号')} 未确认（当前状态：{referenced.get('status')}），不能引用恢复"
    missing = missing_metrics(referenced)
    if missing:
        return None, f"健康报告 {referenced.get('报告编号')} 缺少 {'、'.join(missing)}，指标不完整，限速保持当前档位"
    if not report_is_current(referenced, moment):
        return None, (
            f"健康报告 {referenced.get('报告编号')} 的采样窗口 {referenced.get('采样窗口开始')} ~ "
            f"{referenced.get('采样窗口结束')} 不覆盖当前时间或已过有效期，请重新确认当前窗口的报告"
        )
    queue_len = to_int(referenced.get("错误队列长度"))
    if queue_len is None or queue_len > 0:
        return None, (
            f"健康报告 {referenced.get('报告编号')} 的错误队列长度为 {referenced.get('错误队列长度')}，"
            "异常错误队列尚未放行，限速保持当前档位"
        )
    if int(referenced.get("id", 0)) != int(earliest.get("id", 0)):
        return None, (
            f"存在多份窗口冲突的报告，只允许最早满足完整条件的 {earliest.get('报告编号')} "
            f"（检查时间 {earliest.get('检查时间戳')}）推进恢复，两份报告均保留供审计"
        )
    return referenced, ""
