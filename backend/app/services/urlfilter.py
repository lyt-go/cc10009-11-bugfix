"""URL过滤业务规则：完整字段保存、确定性命中、规则快照与跨模块同步。

设计要点：

* 规则保存一次校验全部字段，不做“先存一半、后面再补”的部分写入。
* 命中结果由 :mod:`app.services.url_filter_policy` 纯函数计算，冲突固定拒绝优先。
* 规则生效时生成不可变快照：规则字段、候选URL、去重后的URL明文结果一起落库；
  同一规则、同一版本、同一内容与同一候选URL只保留一份快照（幂等键）。
* 规则换版只新增快照，旧快照及其URL清单保留原命中记录，不会被新版静默覆盖。
* 页面落地侧清单从快照同步，违规URL不会进入抓取作业的执行参数。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any

from app.services import url_filter_policy as policy
from app.store import store

MODULE = "urlfilter"
SNAPSHOT_TABLE = "urlfilter_snapshot"
SNAPSHOT_URL_TABLE = "urlfilter_snapshot_url"
SYNC_TABLE = "urlfilter_sync_record"

REQUIRED_FIELDS = ["规则编号", "包含模式", "排除模式"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS: list[str] = []

# 所有会改变规则、快照或落地清单的操作串行化，避免并发提交生成第二份范围。
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _as_rule(row: dict[str, Any]) -> dict[str, Any]:
    """把存储行整理成一条完整规则（补齐默认值，避免旧记录字段丢失）。"""
    rule = {
        policy.RULE_CODE: row.get(policy.RULE_CODE),
        policy.INCLUDE: row.get(policy.INCLUDE),
        policy.EXCLUDE: row.get(policy.EXCLUDE),
        policy.DOMAIN_WHITELIST: row.get(policy.DOMAIN_WHITELIST),
        policy.PATH_BLACKLIST: row.get(policy.PATH_BLACKLIST),
        policy.PARAM_STRIP: row.get(policy.PARAM_STRIP),
        policy.CASE_MODE: row.get(policy.CASE_MODE) or policy.CASE_MODE_DEFAULT,
        policy.TRAILING_SLASH: row.get(policy.TRAILING_SLASH) or policy.TRAILING_SLASH_DEFAULT,
        policy.CANDIDATES: policy.candidate_list(row),
        policy.MATCH_SCOPE: row.get(policy.MATCH_SCOPE) or policy.MATCH_SCOPE_DEFAULT,
        policy.RULE_LIMIT: int(row.get(policy.RULE_LIMIT) or policy.DEFAULT_RULE_LIMIT),
        policy.CONFLICT_POLICY: row.get(policy.CONFLICT_POLICY) or policy.CONFLICT_POLICY_DEFAULT,
        policy.REMARK: row.get(policy.REMARK) or "",
    }
    return rule


def _present(row: dict[str, Any]) -> dict[str, Any]:
    """详情/列表统一输出：规则字段一个不少，附带版本与快照摘要。"""
    rule = _as_rule(row)
    presented = {
        "id": row.get("id"),
        "status": row.get("status"),
        "pending": row.get("pending"),
        "abnormal": row.get("abnormal"),
        "version": int(row.get("version") or 1),
        "当前快照": row.get("当前快照"),
        "当前快照版本": row.get("当前快照版本"),
        "生效时间": row.get("生效时间"),
        "更新时间": row.get("更新时间"),
        "字段完整": all(field in row for field in policy.RULE_VALUE_FIELDS),
    }
    presented.update(rule)
    # 候选URL在列表里只给数量，明细接口再展开，避免列表过重。
    presented["候选URL数"] = len(rule[policy.CANDIDATES])
    return presented


def _validation_message(errors: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in errors:
        location = f"第 {item['index']} 项" if "index" in item else ""
        parts.append(f"{item['field']}{(' ' + location) if location else ''}：{item['reason']}")
    return "；".join(parts)


class UrlfilterService:
    # ------------------------------------------------------------ 查询

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
            rows = [row for row in rows if keyword in str(row.get(policy.RULE_CODE, ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [_present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        presented = _present(row)
        snapshots = self.list_snapshots(rule_id=entry_id)
        presented["快照"] = snapshots
        presented["最新快照"] = snapshots[0] if snapshots else None
        return presented

    def policy(self) -> dict[str, Any]:
        return policy.policy_summary()

    # ------------------------------------------------------------ 写入

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
        with _LOCK:
            rows = store.rows(MODULE)
            normalized, errors = policy.validate_rule_values(values, existing_rows=rows)
            if errors:
                return None, [item["field"] for item in errors], errors
            entry = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "version": 1,
                "当前快照": None,
                "当前快照版本": None,
                "生效时间": None,
                "更新时间": _now(),
            }
            entry.update(normalized)
            entry[policy.CANDIDATES] = list(normalized[policy.CANDIDATES])
            rows.append(entry)
            return _present(entry), [], []

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """整条规则覆盖保存；版本不一致时拒绝写入，避免两份范围依据并存。"""
        with _LOCK:
            row = store.find(MODULE, entry_id)
            if row is None:
                return None, f"过滤规则 {entry_id} 不存在或已归档", []
            current_version = int(row.get("version") or 1)
            if expected_version is not None and int(expected_version) != current_version:
                return (
                    None,
                    f"规则 {row.get(policy.RULE_CODE)} 版本已变化：提交版本 {expected_version}，当前版本 {current_version}，请刷新后重试",
                    [policy.error("版本", f"版本冲突：提交 {expected_version}，当前 {current_version}")],
                )
            if row.get("status") == "已废弃":
                return None, f"过滤规则 {row.get(policy.RULE_CODE)} 已废弃，不能再修改", [
                    policy.error("status", "已废弃规则不可修改")
                ]

            merged = _as_rule(row)
            for field_name in policy.RULE_VALUE_FIELDS:
                if field_name in values:
                    merged[field_name] = values[field_name]
            normalized, errors = policy.validate_rule_values(
                merged, existing_rows=store.rows(MODULE), entry_id=entry_id
            )
            if errors:
                return None, _validation_message(errors), errors

            before = policy.material_rule_fields(_as_rule(row))
            before[policy.CANDIDATES] = policy.candidate_list(row)
            after = policy.material_rule_fields(normalized)
            after[policy.CANDIDATES] = list(normalized[policy.CANDIDATES])
            changed = before != after
            if not changed:
                return _present(row), "规则内容未变化，未生成新版本", []

            row.update(normalized)
            row[policy.CANDIDATES] = list(normalized[policy.CANDIDATES])
            row["version"] = current_version + 1
            row["更新时间"] = _now()
            if row.get("status") == "已生效":
                # 生效中的规则改内容必须重新评审；旧快照继续保留。
                row["status"] = "规则评审中"
                row["pending"] = True
            return _present(row), f"过滤规则已保存，当前版本 {row['version']}", []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with _LOCK:
            row = store.find(MODULE, entry_id)
            if row is None:
                return None, f"过滤规则 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于URL过滤可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"

            rule = _as_rule(row)
            normalized, errors = policy.validate_rule_values(
                rule, existing_rows=store.rows(MODULE), entry_id=entry_id
            )
            if errors:
                return None, f"规则校验未通过，动作未执行：{_validation_message(errors)}"

            current_status = row.get("status")
            if action == "生效规则":
                if current_status == "已废弃":
                    return None, "已废弃的过滤规则不能重新生效，请登记新规则"
                snapshot, reused, message = self._ensure_snapshot_locked(row, normalized)
                if snapshot is None:
                    return None, message
                row["status"] = "已生效"
                row["pending"] = False
                row["abnormal"] = False
                row["当前快照"] = snapshot["id"]
                row["当前快照版本"] = snapshot["规则版本"]
                row["生效时间"] = row.get("生效时间") or snapshot["生成时间"]
                row["更新时间"] = _now()
                return _present(row), message if reused else f"过滤规则已生效，{message}"

            if action == "提交规则评审":
                if current_status == "已生效":
                    return None, "规则已生效，如需修改请先保存新内容并重新评审"
                row["status"] = "规则评审中"
                row["pending"] = True
                row["abnormal"] = False
                row["更新时间"] = _now()
                return _present(row), "过滤规则已提交规则评审"

            # 废弃规则：只改状态，不删除/覆盖历史快照与去重URL清单。
            row["status"] = "已废弃"
            row["pending"] = False
            row["abnormal"] = False
            row["更新时间"] = _now()
            return _present(row), "过滤规则已废弃，历史快照与URL清单保留"

    # ------------------------------------------------------------ 快照

    def _ensure_snapshot_locked(
        self,
        row: dict[str, Any],
        normalized_rule: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        """在锁内按幂等键生成快照；已存在时直接复用，不产生第二份范围。"""
        rule = normalized_rule or _as_rule(row)
        rule_id = int(row["id"])
        version = int(row.get("version") or 1)
        candidates = policy.candidate_list(rule)
        fingerprint = policy.rule_fingerprint(rule, candidates)
        idempotency = policy.snapshot_idempotency_key(rule_id, version, fingerprint, candidates)

        existing = self._find_snapshot_by_key(idempotency)
        if existing is not None:
            return existing, True, f"相同规则内容已存在快照 {existing['id']}（版本 {version}），未重复生成范围"

        same_version = [
            item for item in store.aux_rows(SNAPSHOT_TABLE)
            if int(item.get("规则id", 0)) == rule_id and int(item.get("规则版本", 0)) == version
        ]
        if same_version:
            # 同一版本出现不同内容，说明写入顺序被破坏；宁可失败也不分叉。
            return (
                None,
                False,
                f"规则 {rule.get(policy.RULE_CODE)} 版本 {version} 已存在不同内容的快照，拒绝生成第二个版本依据",
            )

        decisions, decision_errors = policy.build_decisions(candidates, rule)
        if decision_errors:
            return None, False, f"候选URL校验未通过：{_validation_message(decision_errors)}"

        snapshot_id = store.aux_next_id(SNAPSHOT_TABLE)
        summary = policy.summarize_decisions(decisions)
        created_at = _now()
        url_rows: list[dict[str, Any]] = []
        next_url_id = store.aux_next_id(SNAPSHOT_URL_TABLE)
        for index, decision in enumerate(decisions, start=1):
            url_rows.append({
                "id": next_url_id + index - 1,
                "快照id": snapshot_id,
                "规则id": rule_id,
                "规则编号": rule.get(policy.RULE_CODE),
                "规则版本": version,
                "序号": index,
                "原始URL": decision.original,
                "规范化URL": decision.normalized,
                "决定": "准入" if decision.admitted else "拒绝",
                "命中字段": decision.primary_hit.field,
                "命中值": decision.primary_hit.value,
                "命中说明": decision.primary_hit.detail,
                "命中项": [hit.as_dict() for hit in decision.hits],
                "冲突": decision.conflict,
                "原因": decision.reason,
                "重复次数": decision.duplicate_count,
                "规范化说明": decision.normalization,
                "复核指纹": policy.snapshot_checksum({
                    "规则指纹": fingerprint,
                    "原始URL": decision.original,
                    "规范化URL": decision.normalized,
                    "决定": decision.admitted,
                    "命中字段": decision.primary_hit.field,
                    "命中值": decision.primary_hit.value,
                }),
            })

        # 快照同时保存规则字段、候选URL，以及这三个规范化参数，
        # 保证去重URL清单可以脱离当前规则被逐项复核。
        rule_snapshot = policy.material_rule_fields(rule)
        rule_snapshot[policy.CANDIDATES] = list(candidates)
        rule_snapshot["规范化参数"] = {
            policy.CASE_MODE: rule.get(policy.CASE_MODE),
            policy.TRAILING_SLASH: rule.get(policy.TRAILING_SLASH),
            policy.PARAM_STRIP: rule.get(policy.PARAM_STRIP),
            policy.MATCH_SCOPE: rule.get(policy.MATCH_SCOPE),
            policy.CONFLICT_POLICY: rule.get(policy.CONFLICT_POLICY),
        }
        checksum = policy.snapshot_checksum({
            "规则id": rule_id,
            "规则编号": rule.get(policy.RULE_CODE),
            "规则版本": version,
            "规则指纹": fingerprint,
            "规则快照": rule_snapshot,
            "URL": [
                {
                    "原始URL": item["原始URL"],
                    "规范化URL": item["规范化URL"],
                    "决定": item["决定"],
                    "命中字段": item["命中字段"],
                    "命中值": item["命中值"],
                }
                for item in url_rows
            ],
        })
        snapshot = {
            "id": snapshot_id,
            "规则id": rule_id,
            "规则编号": rule.get(policy.RULE_CODE),
            "规则版本": version,
            "内容指纹": fingerprint,
            "幂等键": idempotency,
            "快照校验和": checksum,
            "规则快照": rule_snapshot,
            "统计": summary,
            "生成时间": created_at,
            "状态": "已生效",
            "关联作业数": 0,
            "同步状态": "待同步",
        }
        store.aux_rows(SNAPSHOT_TABLE).append(snapshot)
        store.aux_rows(SNAPSHOT_URL_TABLE).extend(url_rows)
        sync_record = self._sync_snapshot_to_landing_locked(snapshot, url_rows)
        snapshot["同步状态"] = sync_record["同步结果"]
        return snapshot, False, f"已生成快照 {snapshot_id}：{summary['去重后URL数']} 个去重URL，准入 {summary['准入数']} 个，拒绝 {summary['拒绝数']} 个"

    def _find_snapshot_by_key(self, idempotency: str) -> dict[str, Any] | None:
        for snapshot in store.aux_rows(SNAPSHOT_TABLE):
            if snapshot.get("幂等键") == idempotency:
                return snapshot
        return None

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        snapshot = store.aux_find(SNAPSHOT_TABLE, snapshot_id)
        if snapshot is None:
            return None
        detail = copy.deepcopy(snapshot)
        detail["URL明细"] = self.list_snapshot_urls(snapshot_id)
        detail["同步记录"] = self.list_sync_records(snapshot_id)
        return detail

    def list_snapshots(
        self,
        *,
        rule_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 200,
    ) -> list[dict[str, Any]]:
        rows = list(store.aux_rows(SNAPSHOT_TABLE))
        if rule_id is not None:
            rows = [row for row in rows if int(row.get("规则id", 0)) == int(rule_id)]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        start = max(page - 1, 0) * size
        return [self._snapshot_summary(row) for row in rows[start:start + size]]

    def _snapshot_summary(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        summary = copy.deepcopy(snapshot)
        summary.pop("规则快照", None)
        summary["规则快照摘要"] = {
            "包含模式": snapshot.get("规则快照", {}).get(policy.INCLUDE),
            "排除模式": snapshot.get("规则快照", {}).get(policy.EXCLUDE),
            "域名白名单": snapshot.get("规则快照", {}).get(policy.DOMAIN_WHITELIST),
            "路径黑名单": snapshot.get("规则快照", {}).get(policy.PATH_BLACKLIST),
            "参数剥离规则": snapshot.get("规则快照", {}).get(policy.PARAM_STRIP),
            "大小写处理": snapshot.get("规则快照", {}).get(policy.CASE_MODE),
            "尾部斜杠策略": snapshot.get("规则快照", {}).get(policy.TRAILING_SLASH),
        }
        return summary

    def list_snapshot_urls(
        self,
        snapshot_id: int,
        *,
        decision: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 500,
    ) -> list[dict[str, Any]]:
        rows = [
            copy.deepcopy(row) for row in store.aux_rows(SNAPSHOT_URL_TABLE)
            if int(row.get("快照id", 0)) == int(snapshot_id)
        ]
        if decision:
            rows = [row for row in rows if row.get("决定") == decision]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("原始URL", "")) or keyword in str(row.get("规范化URL", ""))
            ]
        rows.sort(key=lambda row: int(row.get("序号", 0)))
        start = max(page - 1, 0) * size
        return rows[start:start + size]

    def verify_snapshot(self, snapshot_id: int) -> tuple[dict[str, Any] | None, str]:
        """按快照里的规则字段逐项复算，确认去重URL清单没有命中错位。"""
        snapshot = store.aux_find(SNAPSHOT_TABLE, snapshot_id)
        if snapshot is None:
            return None, f"过滤规则快照 {snapshot_id} 不存在"
        rule = copy.deepcopy(snapshot.get("规则快照") or {})
        stored_rows = self.list_snapshot_urls(snapshot_id, page=1, size=policy.MAX_CANDIDATES)
        mismatches: list[dict[str, Any]] = []
        for row in stored_rows:
            decision = policy.evaluate_url(row.get("原始URL", ""), rule)
            expected = "准入" if decision.admitted else "拒绝"
            if (
                decision.normalized != row.get("规范化URL")
                or expected != row.get("决定")
                or decision.primary_hit.field != row.get("命中字段")
                or str(decision.primary_hit.value) != str(row.get("命中值"))
            ):
                mismatches.append({
                    "序号": row.get("序号"),
                    "原始URL": row.get("原始URL"),
                    "期望": {
                        "规范化URL": decision.normalized,
                        "决定": expected,
                        "命中字段": decision.primary_hit.field,
                        "命中值": decision.primary_hit.value,
                    },
                    "实际": {
                        "规范化URL": row.get("规范化URL"),
                        "决定": row.get("决定"),
                        "命中字段": row.get("命中字段"),
                        "命中值": row.get("命中值"),
                    },
                })
        summary = policy.summarize_decisions([
            policy.evaluate_url(row.get("原始URL", ""), rule) for row in stored_rows
        ])
        # 复核的是去重后的清单；候选总量从每行的重复次数还原，便于和快照统计对齐。
        summary["候选URL数"] = sum(int(row.get("重复次数") or 1) for row in stored_rows)
        recalculated = policy.snapshot_checksum({
            "规则id": snapshot.get("规则id"),
            "规则编号": snapshot.get("规则编号"),
            "规则版本": snapshot.get("规则版本"),
            "规则指纹": snapshot.get("内容指纹"),
            "规则快照": snapshot.get("规则快照"),
            "URL": [
                {
                    "原始URL": row.get("原始URL"),
                    "规范化URL": row.get("规范化URL"),
                    "决定": row.get("决定"),
                    "命中字段": row.get("命中字段"),
                    "命中值": row.get("命中值"),
                }
                for row in stored_rows
            ],
        })
        ok = not mismatches and recalculated == snapshot.get("快照校验和")
        result = {
            "快照id": snapshot_id,
            "规则编号": snapshot.get("规则编号"),
            "规则版本": snapshot.get("规则版本"),
            "可复核": True,
            "一致": ok,
            "复核URL数": len(stored_rows),
            "不一致项": mismatches,
            "快照校验和": snapshot.get("快照校验和"),
            "复算校验和": recalculated,
            "统计": summary,
        }
        if ok:
            return result, f"快照 {snapshot_id} 逐项复核一致，共 {len(stored_rows)} 条去重URL"
        return result, f"快照 {snapshot_id} 复核发现 {len(mismatches)} 处不一致，请检查命中错位"

    # ------------------------------------------------------------ URL准入

    def evaluate(
        self,
        *,
        rule_id: int | None = None,
        snapshot_id: int | None = None,
        candidates: Any = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """临时准入计算：不落库，返回去重后的逐项命中结果。"""
        rule: dict[str, Any] | None = None
        source = ""
        snapshot = None
        if snapshot_id is not None:
            snapshot = store.aux_find(SNAPSHOT_TABLE, snapshot_id)
            if snapshot is None:
                return None, f"过滤规则快照 {snapshot_id} 不存在"
            rule = copy.deepcopy(snapshot.get("规则快照") or {})
            source = f"快照 {snapshot_id}（规则 {snapshot.get('规则编号')} 版本 {snapshot.get('规则版本')}）"
        elif rule_id is not None:
            row = store.find(MODULE, rule_id)
            if row is None:
                return None, f"过滤规则 {rule_id} 不存在或已归档"
            rule = _as_rule(row)
            source = f"规则 {rule.get(policy.RULE_CODE)} 版本 {row.get('version')}"
        else:
            return None, "请提供规则编号或过滤规则快照"
        assert rule is not None
        raw_candidates = policy.split_candidate_urls(candidates)
        if not raw_candidates:
            return None, "候选URL不能为空"
        if len(raw_candidates) > policy.MAX_CANDIDATES:
            return None, f"单次最多处理 {policy.MAX_CANDIDATES} 个候选URL，当前 {len(raw_candidates)} 个"
        decisions, errors = policy.build_decisions(raw_candidates, rule)
        if errors:
            return None, f"候选URL校验未通过：{_validation_message(errors)}"
        summary = policy.summarize_decisions(decisions)
        return {
            "规则来源": source,
            "规则编号": rule.get(policy.RULE_CODE),
            "规则版本": int(snapshot.get("规则版本") or 1) if snapshot is not None else int(
                (store.find(MODULE, rule_id) or {}).get("version") or 1
            ),
            "统计": summary,
            "命中优先级": list(policy.MATCH_PRIORITY),
            "拒绝原因优先级": list(policy.DENY_REASON_PRIORITY),
            "URL明细": [decision.as_dict() for decision in decisions],
        }, "URL准入计算完成"

    # ------------------------------------------------------------ 页面落地同步

    def _sync_snapshot_to_landing_locked(
        self,
        snapshot: dict[str, Any],
        url_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """把快照的准入URL同步到页面落地侧清单；重复同步幂等。"""
        landing_rows = store.rows("rendertask")
        existing_keys = {
            (row.get("过滤规则快照"), row.get("目标URL"))
            for row in landing_rows
        }
        admitted = [row for row in url_rows if row.get("决定") == "准入"]
        rejected = [row for row in url_rows if row.get("决定") != "准入"]
        added: list[dict[str, Any]] = []
        skipped = 0
        next_base = max((int(row.get("id", 0)) for row in landing_rows), default=0)
        for index, url_row in enumerate(admitted, start=1):
            key = (snapshot["id"], url_row["规范化URL"])
            if key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)
            added.append({
                "id": next_base + len(added) + 1,
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "作业编号": f"LAND-{snapshot['id']:04d}-{index:04d}",
                "目标URL": url_row["规范化URL"],
                "原始URL": url_row["原始URL"],
                "浏览器版本": "chromium-latest",
                "视口宽度": "1440",
                "视口高度": "900",
                "等待策略": "networkidle",
                "脚本注入": "",
                "超时阈值秒": "30",
                "过滤规则快照": snapshot["id"],
                "快照版本": snapshot["规则版本"],
                "规则编号": snapshot["规则编号"],
                "准入结果": "准入",
                "同步时间": _now(),
                "来源": "URL过滤快照同步",
            })
        landing_rows.extend(added)

        record_id = store.aux_next_id(SYNC_TABLE)
        record = {
            "id": record_id,
            "快照id": snapshot["id"],
            "规则id": snapshot["规则id"],
            "规则编号": snapshot["规则编号"],
            "规则版本": snapshot["规则版本"],
            "快照校验和": snapshot["快照校验和"],
            "目标模块": "rendertask",
            "目标清单": "页面落地侧清单",
            "新增数": len(added),
            "已存在数": skipped,
            "拦截数": len(rejected),
            "去重后URL数": len(url_rows),
            "同步结果": "已同步" if not rejected else f"已同步，拦截 {len(rejected)} 个违规URL",
            "同步时间": _now(),
            "关联业务刷新": True,
        }
        store.aux_rows(SYNC_TABLE).append(record)
        return record

    def sync_snapshot(self, snapshot_id: int) -> tuple[dict[str, Any] | None, str]:
        """手动重放同步：同一快照重复同步不会生成第二份清单。"""
        with _LOCK:
            snapshot = store.aux_find(SNAPSHOT_TABLE, snapshot_id)
            if snapshot is None:
                return None, f"过滤规则快照 {snapshot_id} 不存在"
            url_rows = self.list_snapshot_urls(snapshot_id, page=1, size=policy.MAX_CANDIDATES)
            record = self._sync_snapshot_to_landing_locked(snapshot, url_rows)
            snapshot["同步状态"] = record["同步结果"]
            return record, (
                f"快照 {snapshot_id} 已同步到页面落地侧清单：新增 {record['新增数']} 条，"
                f"已存在 {record['已存在数']} 条，拦截 {record['拦截数']} 条"
            )

    def list_sync_records(self, snapshot_id: int | None = None) -> list[dict[str, Any]]:
        rows = [copy.deepcopy(row) for row in store.aux_rows(SYNC_TABLE)]
        if snapshot_id is not None:
            rows = [row for row in rows if int(row.get("快照id", 0)) == int(snapshot_id)]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        return rows

    def snapshot_rule(self, snapshot_id: int) -> dict[str, Any] | None:
        """供作业侧消费：返回快照里的规则字段和统计。"""
        snapshot = store.aux_find(SNAPSHOT_TABLE, snapshot_id)
        if snapshot is None:
            return None
        return copy.deepcopy(snapshot)

    def latest_snapshot_for_rule(self, rule_id: int) -> dict[str, Any] | None:
        rows = [row for row in store.aux_rows(SNAPSHOT_TABLE) if int(row.get("规则id", 0)) == int(rule_id)]
        if not rows:
            return None
        rows.sort(key=lambda row: (int(row.get("规则版本", 0)), int(row.get("id", 0))), reverse=True)
        return copy.deepcopy(rows[0])

    def latest_snapshot_by_code(self, rule_code: str) -> dict[str, Any] | None:
        rows = [
            row for row in store.aux_rows(SNAPSHOT_TABLE)
            if str(row.get("规则编号", "")).strip().lower() == rule_code.strip().lower()
        ]
        if not rows:
            return None
        rows.sort(key=lambda row: (int(row.get("规则版本", 0)), int(row.get("id", 0))), reverse=True)
        return copy.deepcopy(rows[0])

    def ensure_effective_snapshots(self) -> list[int]:
        """服务启动时补齐生效规则尚未生成的快照（幂等）。"""
        created: list[int] = []
        with _LOCK:
            for row in store.rows(MODULE):
                if row.get("status") != "已生效":
                    continue
                if row.get("当前快照") and store.aux_find(SNAPSHOT_TABLE, int(row["当前快照"])) is not None:
                    continue
                snapshot, _, _ = self._ensure_snapshot_locked(row)
                if snapshot is not None:
                    row["当前快照"] = snapshot["id"]
                    row["当前快照版本"] = snapshot["规则版本"]
                    created.append(snapshot["id"])
        return created


_service = UrlfilterService()


def get_service() -> UrlfilterService:
    return _service


def resolve_snapshot(
    snapshot_id: int | None = None,
    rule_code: str | None = None,
    rule_id: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """按快照id/规则编号/规则id定位快照，供抓取作业与页面落地侧复用。"""
    service = get_service()
    if snapshot_id is not None:
        snapshot = service.snapshot_rule(int(snapshot_id))
        if snapshot is None:
            return None, f"过滤规则快照 {snapshot_id} 不存在"
        return snapshot, ""
    if rule_code:
        snapshot = service.latest_snapshot_by_code(rule_code)
        if snapshot is None:
            return None, f"规则编号 {rule_code} 尚未生成已生效快照"
        return snapshot, ""
    if rule_id is not None:
        snapshot = service.latest_snapshot_for_rule(int(rule_id))
        if snapshot is None:
            return None, f"过滤规则 {rule_id} 尚未生成已生效快照"
        return snapshot, ""
    return None, "请提供过滤规则快照、规则编号或规则id"
