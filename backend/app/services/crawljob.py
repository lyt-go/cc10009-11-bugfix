"""抓取作业业务规则：状态流转、字段校验与筛选口径都收在这里。

作业启动前必须先解析到一份已生效的过滤规则快照，并用快照里的规则重新校验
起始URL与候选URL。只有准入URL会进入作业执行参数，违规URL单独记录为拦截项，
不会混进执行参数里。
"""
from __future__ import annotations

from typing import Any

from app.services import url_filter_policy as policy
from app.services.urlfilter import resolve_snapshot
from app.store import store

MODULE = "crawljob"
REQUIRED_FIELDS = ["作业编号", "目标站点", "起始URL"]
STATUS_ORDER = ["草稿", "作业审批中", "执行中", "已完成", "已停止"]
ACTION_RULES = {"提交作业审批": "作业审批中", "启动作业": "执行中", "停止作业": "已停止"}
NEGATIVE_ACTIONS: list[str] = []

OPTIONAL_FIELDS = [
    "爬取深度", "URL去重策略", "页面解析器", "下载超时秒", "最大页面数",
    "候选URL", "过滤规则快照", "过滤规则编号",
]


def _present(row: dict[str, Any]) -> dict[str, Any]:
    entry = dict(row)
    params = row.get("执行参数") or {}
    entry["执行参数URL数"] = len(params.get("准入URL清单", []))
    entry["拦截URL数"] = len(params.get("拦截URL清单", []))
    entry["执行参数快照"] = params.get("过滤规则快照")
    return entry


def _candidate_list(row: dict[str, Any]) -> list[str]:
    value = row.get("候选URL")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return policy.split_candidate_urls(value)


class CrawljobService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("作业编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
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
        errors: list[dict[str, Any]] = []
        start_url = str(values.get("起始URL") or "").strip()
        start_problem = policy.url_shape_error(start_url)
        if start_problem:
            errors.append(policy.error("起始URL", f"起始URL{start_problem}", value=start_url))
        candidates_value = values.get("候选URL")
        candidates = policy.split_candidate_urls(candidates_value)
        for index, raw in enumerate(candidates, start=1):
            problem = policy.url_shape_error(raw)
            if problem:
                errors.append(policy.error("候选URL", f"第 {index} 项{problem}", index=index, value=raw))
        if len(candidates) > policy.MAX_CANDIDATES:
            errors.append(policy.error("候选URL", f"候选URL最多 {policy.MAX_CANDIDATES} 个", limit=policy.MAX_CANDIDATES))
        depth = str(values.get("爬取深度") or "").strip()
        if depth and not depth.isdigit():
            errors.append(policy.error("爬取深度", f"爬取深度必须是整数：{depth}", value=depth))
        max_pages = str(values.get("最大页面数") or "").strip()
        if max_pages and not max_pages.isdigit():
            errors.append(policy.error("最大页面数", f"最大页面数必须是整数：{max_pages}", value=max_pages))
        if errors:
            return None, [item["field"] for item in errors], errors

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field_name in REQUIRED_FIELDS + OPTIONAL_FIELDS:
            if field_name in values:
                entry[field_name] = values.get(field_name)
        entry["起始URL"] = start_url
        entry["候选URL"] = candidates
        entry["过滤规则快照"] = values.get("过滤规则快照")
        entry["过滤规则编号"] = str(values.get("过滤规则编号") or "").strip()
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["执行参数"] = None
        rows.append(entry)
        return _present(entry), [], []

    def _resolve_snapshot_for(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        snapshot_id = entry.get("过滤规则快照")
        rule_code = str(entry.get("过滤规则编号") or "").strip()
        rule_code = rule_code or None
        snapshot, message = resolve_snapshot(
            snapshot_id=int(snapshot_id) if str(snapshot_id or "").strip().isdigit() else None,
            rule_code=rule_code,
        )
        return snapshot, message

    def _build_execution_params(
        self,
        entry: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        rule = dict(snapshot.get("规则快照") or {})
        execution_urls: list[str] = []
        start_url = str(entry.get("起始URL") or "").strip()
        if start_url:
            execution_urls.append(start_url)
        execution_urls.extend(_candidate_list(entry))

        decisions, decision_errors = policy.build_decisions(execution_urls, rule)
        if decision_errors:
            return None, f"作业URL校验未通过：{'；'.join(item['reason'] for item in decision_errors)}", decision_errors

        start_decision = next((item for item in decisions if item.original.strip() == start_url), None)
        if start_decision is not None and not start_decision.admitted:
            return (
                None,
                f"起始URL未通过过滤规则快照 {snapshot['id']}：{start_decision.reason}，作业不能启动",
                [policy.error("起始URL", start_decision.reason, value=start_url)],
            )

        admitted = [item for item in decisions if item.admitted]
        rejected = [item for item in decisions if not item.admitted]
        params = {
            "过滤规则快照": snapshot["id"],
            "规则编号": snapshot.get("规则编号"),
            "规则版本": snapshot.get("规则版本"),
            "快照校验和": snapshot.get("快照校验和"),
            "命中优先级": list(policy.MATCH_PRIORITY),
            "冲突策略": rule.get(policy.CONFLICT_POLICY) or policy.CONFLICT_POLICY_DEFAULT,
            "准入URL清单": [item.normalized for item in admitted],
            "准入明细": [item.as_dict() for item in admitted],
            "拦截URL清单": [item.as_dict() for item in rejected],
            "候选URL数": sum(item.duplicate_count for item in decisions),
            "去重后URL数": len(decisions),
            "生成时间": snapshot.get("生成时间"),
        }
        return params, "作业执行参数已按过滤规则快照生成", []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"作业定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于抓取作业可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "启动作业":
            snapshot, message = self._resolve_snapshot_for(entry)
            if snapshot is None:
                return None, f"作业不能启动：{message}"
            params, message, errors = self._build_execution_params(entry, snapshot)
            if params is None:
                return None, message
            entry["执行参数"] = params
            entry["过滤规则快照"] = snapshot["id"]
            entry["过滤规则编号"] = snapshot.get("规则编号")
            snapshot["关联作业数"] = int(snapshot.get("关联作业数") or 0) + 1
            entry["abnormal"] = False
        elif action == "停止作业":
            entry["执行参数"] = None
            entry["abnormal"] = False

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return _present(entry), f"作业定义已{action}"
