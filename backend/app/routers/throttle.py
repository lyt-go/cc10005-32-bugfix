"""动态限速接口：维护限速规则，覆盖评审、启用、降档、恢复、关闭等动作。

限速恢复额外提供三条凭证通道（书签 / 上传 / 导入），它们与页面按钮共用同一套
原子状态机，并提供凭证回读重放，保证回读后速率决策不变、原始文件版本保留。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.recovery import RecoveryService
from app.services.throttle import ThrottleService
from app.store import store

router = APIRouter(prefix="/api/throttle", tags=["动态限速"])

service = ThrottleService()
recovery_service = RecoveryService()

LIST_FIELDS = ["规则编号", "目标域组", "初始速率", "最大速率", "加速因子", "减速因子", "错误率阈值", "降速恢复间隔"]
STATUSES = ["草稿", "规则评审中", "已启用", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已启用、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤动态限速列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 具体路径必须排在 /{entry_id} 之前，否则会被当成 entry_id 捕获（导出接口原有隐患一并修掉）。
@router.get("/eligible-reports")
def eligible_reports() -> dict[str, Any]:
    """返回当前可用于限速恢复的健康报告：已确认、指标完整且窗口覆盖当前时间。"""
    from app.services import linkage

    with store.locked():
        linkage.expire_stale_reports()
        reports = linkage.find_eligible_reports()
        return {"items": [dict(report) for report in reports], "total": len(reports)}


@router.get("/decisions")
def list_decisions(rule_no: str | None = None) -> dict[str, Any]:
    """速率决策流水：每次启用/降档/恢复/拦截都可追溯，禁止只改速率不记决策。"""
    with store.locked():
        rows = store.rows("rate_decision")
        if rule_no:
            rows = [row for row in rows if row.get("规则编号") == rule_no]
        return {"items": [dict(row) for row in rows], "total": len(rows)}


@router.get("/recovery/artifacts")
def list_artifacts(rule_no: str | None = None) -> dict[str, Any]:
    """恢复凭证清单：书签、上传、导入三种通道的原始版本与处理结果。"""
    items = recovery_service.list_artifacts(rule_no=rule_no)
    return {"items": items, "total": len(items)}


@router.post("/recovery/artifacts/{artifact_id}/replay", response_model=ActionResult)
def replay_artifact(artifact_id: int) -> ActionResult:
    """回读重放凭证：用留存的原始内容再走一遍恢复链路，核对速率决策不变。"""
    artifact, message = recovery_service.replay(artifact_id)
    if artifact is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=artifact)


@router.post("/recovery/bookmark", response_model=ActionResult)
def recovery_bookmark(payload: EntryPayload) -> ActionResult:
    """书签通道：把恢复意图存成服务端书签并执行原子恢复，重复书签不会二次生效。"""
    raw = json.dumps(payload.values, ensure_ascii=False).encode("utf-8")
    artifact, message = recovery_service.submit(channel="bookmark", raw=raw)
    if artifact is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=artifact.get("处理结果") == "成功", message=message, entry=artifact)


@router.post("/recovery/import", response_model=ActionResult)
def recovery_import(payload: EntryPayload) -> ActionResult:
    """导入通道：结构化 JSON 导入恢复意图，原始载荷按版本留存。"""
    raw = json.dumps(payload.values, ensure_ascii=False).encode("utf-8")
    artifact, message = recovery_service.submit(channel="import", raw=raw)
    if artifact is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=artifact.get("处理结果") == "成功", message=message, entry=artifact)


@router.post("/recovery/upload", response_model=ActionResult)
async def recovery_upload(request: Request) -> ActionResult:
    """上传通道：请求体即原始文件字节，文件名走 X-Filename 头，原文件版本原样留存。"""
    raw = await request.body()
    filename = request.headers.get("X-Filename")
    artifact, message = recovery_service.submit(channel="upload", raw=raw, filename=filename)
    if artifact is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=artifact.get("处理结果") == "成功", message=message, entry=artifact)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出动态限速清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "throttle", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条限速规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"限速规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条限速规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="限速规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条限速规则执行动作；状态回退、未确认恢复等非法操作会被拦下并说明原因。

    启用规则/恢复速率可在 values 里带 ``健康报告id``；限速降档可带
    ``降档速率`` 与 ``错误队列长度``，全部在同一把锁内原子处理。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
