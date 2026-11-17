"""URL过滤接口：维护过滤规则，覆盖提交规则评审、生效规则、废弃规则等动作。

除基础列表/登记/动作外，这里还暴露了四类可复核能力：

* ``GET /policy``           —— 上限、匹配范围、冲突条件、非法值标准的固定口径
* ``POST /admission``       —— 用规则或快照对候选URL做唯一准入计算
* ``GET /snapshots/...``    —— 过滤规则快照、去重URL明细与逐项复核
* ``GET /sync-records``     —— 快照同步到页面落地侧清单的记录
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.url_filter_policy import policy_summary
from app.services.urlfilter import UrlfilterService

router = APIRouter(prefix="/api/urlfilter", tags=["URL过滤"])

service = UrlfilterService()

LIST_FIELDS = [
    "规则编号", "包含模式", "排除模式", "域名白名单", "路径黑名单",
    "参数剥离规则", "大小写处理", "尾部斜杠策略", "候选URL",
    "匹配范围", "规则上限", "冲突策略",
]
STATUSES = ["草稿", "规则评审中", "已生效", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤URL过滤列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/policy")
def get_policy() -> dict[str, Any]:
    """URL过滤的固定口径：字段、上限、匹配范围、冲突条件、非法值标准。"""
    return policy_summary()


@router.get("/snapshots")
def list_snapshots(
    rule_id: int | None = Query(default=None, description="按规则id筛选"),
    status: str | None = Query(default=None, description="快照状态"),
    page: int = 1,
    size: int = 200,
) -> dict[str, Any]:
    """过滤规则快照列表：每份快照对应一次确定的处理范围。"""
    if size > 500:
        raise HTTPException(status_code=400, detail="每页最多 500 条，请缩小分页范围")
    items = service.list_snapshots(rule_id=rule_id, status=status, page=page, size=size)
    return {"module": "urlfilter", "total": len(items), "page": page, "size": size, "items": items}


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int, include_urls: bool = Query(default=True)) -> dict[str, Any]:
    """读取单份快照：规则字段、去重URL明细、同步记录。"""
    detail = service.get_snapshot(snapshot_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"过滤规则快照 {snapshot_id} 不存在")
    if not include_urls:
        detail.pop("URL明细", None)
    return detail


@router.get("/snapshots/{snapshot_id}/urls")
def list_snapshot_urls(
    snapshot_id: int,
    decision: str | None = Query(default=None, description="准入 / 拒绝"),
    keyword: str | None = Query(default=None, description="按原始或规范化URL检索"),
    page: int = 1,
    size: int = 200,
) -> dict[str, Any]:
    """快照的去重URL清单：可从原规则逐项复核命中字段和原因。"""
    snapshot = service.snapshot_rule(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"过滤规则快照 {snapshot_id} 不存在")
    if size > 1000:
        raise HTTPException(status_code=400, detail="每页最多 1000 条，请缩小分页范围")
    items = service.list_snapshot_urls(snapshot_id, decision=decision, keyword=keyword, page=page, size=size)
    return {
        "module": "urlfilter",
        "snapshot": {
            "id": snapshot_id,
            "规则编号": snapshot.get("规则编号"),
            "规则版本": snapshot.get("规则版本"),
            "统计": snapshot.get("统计"),
            "快照校验和": snapshot.get("快照校验和"),
        },
        "total": len(items),
        "page": page,
        "size": size,
        "items": items,
    }


@router.post("/snapshots/{snapshot_id}/verify")
def verify_snapshot(snapshot_id: int) -> ActionResult:
    """按快照中的规则字段重新计算命中，确认去重URL清单没有错位。"""
    result, message = service.verify_snapshot(snapshot_id)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=bool(result["一致"]), message=message, data=result)


@router.post("/snapshots/{snapshot_id}/sync")
def sync_snapshot(snapshot_id: int) -> ActionResult:
    """把快照同步到页面落地侧清单；重复同步幂等，不会生成第二份范围。"""
    record, message = service.sync_snapshot(snapshot_id)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, data=record)


@router.get("/sync-records")
def list_sync_records(snapshot_id: int | None = Query(default=None)) -> dict[str, Any]:
    """快照同步记录：哪个快照在什么时间刷新了页面落地侧清单。"""
    items = service.list_sync_records(snapshot_id)
    return {"module": "urlfilter", "total": len(items), "items": items}


@router.post("/admission")
def calculate_admission(payload: EntryPayload) -> ActionResult:
    """URL准入计算：规则或快照 + 候选URL，返回去重后的唯一命中结果。"""
    values = payload.values
    result, message = service.evaluate(
        rule_id=_optional_int(values.get("规则id")),
        snapshot_id=_optional_int(values.get("过滤规则快照") or values.get("快照id")),
        candidates=values.get("候选URL"),
    )
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, data=result)


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"参数必须是整数，当前值：{value}") from exc



@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出URL过滤清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "urlfilter", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条过滤规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"过滤规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条过滤规则，缺字段或值非法时逐项说明原因而不是静默丢弃。"""
    entry, missing, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(
            ok=False,
            message=f"过滤规则未保存：{'；'.join(item['reason'] for item in errors)}",
            errors=errors,
        )
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}", errors=[])
    return ActionResult(ok=True, message="过滤规则已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """覆盖保存整条规则：完整字段与版本号一起提交，避免字段丢失与版本分叉。"""
    expected = _optional_int(payload.values.get("version") or payload.values.get("版本"))
    entry, message, errors = service.update_entry(entry_id, payload.values, expected_version=expected)
    if entry is None:
        return ActionResult(ok=False, message=message, errors=errors)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条过滤规则执行提交规则评审、生效规则、废弃规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
