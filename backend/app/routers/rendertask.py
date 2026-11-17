"""渲染任务接口：维护渲染作业，覆盖提交排队、启动渲染、终止渲染等动作。

页面落地侧通过 ``/api/rendertask/snapshots/{snapshot_id}`` 直接核对某个过滤规则
快照落下来的清单与记录。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.rendertask import RendertaskService

router = APIRouter(prefix="/api/rendertask", tags=["渲染任务"])

service = RendertaskService()

LIST_FIELDS = [
    "作业编号", "目标URL", "浏览器版本", "视口宽度", "视口高度",
    "等待策略", "脚本注入", "超时阈值秒", "过滤规则快照", "规则编号", "准入结果",
]
STATUSES = ["草稿", "排队中", "渲染中", "已完成", "已超时"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按作业编号检索"),
    status: str | None = Query(default=None, description="草稿、排队中、渲染中、已完成、已超时"),
    snapshot_id: int | None = Query(default=None, description="按过滤规则快照筛选页面落地侧清单"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按作业编号、状态与过滤规则快照过滤渲染任务列表。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, snapshot_id=snapshot_id, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/snapshots/{snapshot_id}")
def snapshot_landing(snapshot_id: int) -> dict[str, Any]:
    """页面落地侧清单：某个过滤规则快照同步下来的条目与快照摘要。"""
    view = service.snapshot_view(snapshot_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"过滤规则快照 {snapshot_id} 不存在")
    return view


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出渲染任务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rendertask", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条渲染作业明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"渲染作业 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条渲染作业；带快照时先做准入校验，违规URL直接拒绝登记。"""
    entry, missing, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(
            ok=False,
            message=f"渲染作业未保存：{'；'.join(item['reason'] for item in errors)}",
            errors=errors,
        )
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}", errors=[])
    return ActionResult(ok=True, message="渲染作业已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条渲染作业执行提交排队、启动渲染、终止渲染；违规URL不会进入执行参数。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

