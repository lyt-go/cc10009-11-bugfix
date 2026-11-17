"""渲染任务（页面落地侧）业务规则：消费过滤规则快照，拦截违规URL。

页面落地侧清单里的每一条都记录来源快照；启动渲染前按快照规则重新复核目标URL，
违规URL不会进入执行参数，也不会被当成可渲染条目。
"""
from __future__ import annotations

import copy
from typing import Any

from app.services import url_filter_policy as policy
from app.services.urlfilter import get_service
from app.store import store

MODULE = "rendertask"
REQUIRED_FIELDS = ["作业编号", "目标URL", "浏览器版本"]
STATUS_ORDER = ["草稿", "排队中", "渲染中", "已完成", "已超时"]
ACTION_RULES = {"提交排队": "排队中", "启动渲染": "渲染中", "终止渲染": "已超时"}
NEGATIVE_ACTIONS: list[str] = []


def _snapshot_id(row: dict[str, Any]) -> int | None:
    value = row.get("过滤规则快照")
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() else None


def _present(row: dict[str, Any]) -> dict[str, Any]:
    entry = dict(row)
    entry["执行参数URL"] = (row.get("执行参数") or {}).get("目标URL")
    entry["执行参数快照"] = (row.get("执行参数") or {}).get("过滤规则快照")
    entry["快照准入"] = (row.get("执行参数") or {}).get("准入校验")
    return entry


class RendertaskService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        snapshot_id: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("作业编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if snapshot_id is not None:
            rows = [row for row in rows if _snapshot_id(row) == int(snapshot_id)]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [_present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return _present(row) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing, []
        target_url = str(values.get("目标URL") or "").strip()
        problems: list[dict[str, Any]] = []
        shape_error = policy.url_shape_error(target_url)
        if shape_error:
            problems.append(policy.error("目标URL", f"目标URL{shape_error}", value=target_url))
        snapshot_id = values.get("过滤规则快照")
        snapshot = None
        if snapshot_id is not None and str(snapshot_id).strip():
            snapshot = get_service().snapshot_rule(int(snapshot_id))
            if snapshot is None:
                problems.append(policy.error("过滤规则快照", f"过滤规则快照 {snapshot_id} 不存在", value=snapshot_id))
            else:
                decision = policy.evaluate_url(target_url, snapshot.get("规则快照") or {})
                if not decision.admitted:
                    problems.append(policy.error(
                        "目标URL", f"目标URL未通过快照 {snapshot_id} 准入：{decision.reason}", value=target_url
                    ))
        if problems:
            return None, [item["field"] for item in problems], problems

        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field_name in REQUIRED_FIELDS + ["视口宽度", "视口高度", "等待策略", "脚本注入", "超时阈值秒"]:
            if field_name in values:
                entry[field_name] = values.get(field_name)
        entry["目标URL"] = target_url
        entry["过滤规则快照"] = int(snapshot_id) if snapshot is not None else None
        entry["快照版本"] = snapshot.get("规则版本") if snapshot else None
        entry["规则编号"] = snapshot.get("规则编号") if snapshot else None
        entry["准入结果"] = "准入"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["执行参数"] = None
        rows.append(entry)
        return _present(entry), [], []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"渲染作业 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于渲染任务可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "启动渲染":
            snapshot_id = _snapshot_id(entry)
            if snapshot_id is None:
                return None, "渲染作业未关联过滤规则快照，不能进入执行参数"
            snapshot = get_service().snapshot_rule(snapshot_id)
            if snapshot is None:
                return None, f"过滤规则快照 {snapshot_id} 不存在，渲染作业不能启动"
            target_url = str(entry.get("目标URL") or "").strip()
            decision = policy.evaluate_url(target_url, snapshot.get("规则快照") or {})
            if not decision.admitted:
                entry["abnormal"] = True
                return None, f"违规URL不能进入作业执行参数：{decision.reason}"
            entry["执行参数"] = {
                "过滤规则快照": snapshot_id,
                "规则编号": snapshot.get("规则编号"),
                "规则版本": snapshot.get("规则版本"),
                "快照校验和": snapshot.get("快照校验和"),
                "目标URL": decision.normalized,
                "准入校验": {
                    "决定": "准入",
                    "命中字段": decision.primary_hit.field,
                    "命中值": decision.primary_hit.value,
                    "命中说明": decision.primary_hit.detail,
                },
            }
            entry["异常原因"] = None
            entry["abnormal"] = False
        elif action == "终止渲染":
            entry["执行参数"] = None
            entry["abnormal"] = False

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS or bool(entry.get("abnormal"))
        return _present(entry), f"渲染作业已{action}"

    def list_landing(self, snapshot_id: int) -> tuple[list[dict[str, Any]], int]:
        """页面落地侧清单：只列某份快照同步过来的条目，附带快照摘要。"""
        items, total = self.list_entries(snapshot_id=snapshot_id, page=1, size=10000)
        return items, total

    def snapshot_view(self, snapshot_id: int) -> dict[str, Any] | None:
        snapshot = get_service().snapshot_rule(snapshot_id)
        if snapshot is None:
            return None
        items, total = self.list_landing(snapshot_id)
        return {
            "快照id": snapshot_id,
            "规则编号": snapshot.get("规则编号"),
            "规则版本": snapshot.get("规则版本"),
            "快照校验和": snapshot.get("快照校验和"),
            "统计": copy.deepcopy(snapshot.get("统计")),
            "落地条目数": total,
            "落地清单": items,
        }
