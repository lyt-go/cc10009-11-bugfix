"""URL过滤与URL准入的回归测试（标准库 unittest，无需额外依赖）。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import threading
import unittest

from app.services import url_filter_policy as policy
from app.services.crawljob import CrawljobService
from app.services.rendertask import RendertaskService
from app.services.urlfilter import UrlfilterService
from app.store import store


BASE_RULE = {
    policy.RULE_CODE: "URLF-TEST",
    policy.INCLUDE: r"^https://site\.example\.com/",
    policy.EXCLUDE: "secret=",
    policy.DOMAIN_WHITELIST: "site.example.com",
    policy.PATH_BLACKLIST: "/admin",
    policy.PARAM_STRIP: "utm_source",
    policy.CASE_MODE: "域名小写",
    policy.TRAILING_SLASH: "去除",
    policy.MATCH_SCOPE: "全站",
    policy.CONFLICT_POLICY: "拒绝优先",
    policy.CANDIDATES: [
        "https://site.example.com/a?utm_source=x&b=2",
        "https://site.example.com/a?b=2",
        "https://site.example.com/admin/x",
        "https://site.example.com/secret=1",
        "https://evil.example.com/x",
    ],
}


class StoreSnapshot:
    """测试之间恢复全局内存仓库，避免用例相互污染。"""

    def __init__(self) -> None:
        self.tables = copy.deepcopy(store._tables)
        self.aux = copy.deepcopy(store._aux_tables)

    def restore(self) -> None:
        store._tables = copy.deepcopy(self.tables)
        store._aux_tables = copy.deepcopy(self.aux)


class UrlfilterBase(unittest.TestCase):
    def setUp(self) -> None:
        self._snapshot = StoreSnapshot()
        self.service = UrlfilterService()

    def tearDown(self) -> None:
        self._snapshot.restore()

    def create_and_activate(self, values: dict | None = None) -> tuple[int, int]:
        entry, _, errors = self.service.create_entry(values or dict(BASE_RULE))
        self.assertIsNotNone(entry, errors)
        rule_id = entry["id"]
        entry, message = self.service.run_action(rule_id, "生效规则")
        self.assertIsNotNone(entry, message)
        return rule_id, int(entry["当前快照"])


class RuleValidationTests(UrlfilterBase):
    def test_full_fields_are_saved_and_presented(self) -> None:
        entry, _, errors = self.service.create_entry(dict(BASE_RULE))
        self.assertEqual(errors, [])
        assert entry is not None
        for field in policy.RULE_VALUE_FIELDS:
            self.assertIn(field, entry)
        self.assertTrue(entry["字段完整"])
        self.assertEqual(entry[policy.CASE_MODE], "域名小写")
        self.assertEqual(entry[policy.TRAILING_SLASH], "去除")
        self.assertEqual(entry[policy.MATCH_SCOPE], "全站")

    def test_invalid_values_return_locatable_reasons_without_partial_save(self) -> None:
        bad = dict(BASE_RULE, **{
            policy.RULE_CODE: "URLF-BAD",
            policy.INCLUDE: "(",
            policy.DOMAIN_WHITELIST: "a.com,,https://b.com",
            policy.PARAM_STRIP: "a=1",
            policy.TRAILING_SLASH: "随便",
        })
        entry, _, errors = self.service.create_entry(bad)
        self.assertIsNone(entry)
        fields = {item["field"] for item in errors}
        self.assertEqual(
            fields,
            {policy.INCLUDE, policy.DOMAIN_WHITELIST, policy.PARAM_STRIP, policy.TRAILING_SLASH},
        )
        # 校验失败时一条记录都不能落库，避免“一半保存”。
        self.assertFalse(any(row.get(policy.RULE_CODE) == "URLF-BAD" for row in store.rows("urlfilter")))

    def test_duplicate_rule_code_is_rejected(self) -> None:
        self.service.create_entry(dict(BASE_RULE))
        entry, _, errors = self.service.create_entry(dict(BASE_RULE))
        self.assertIsNone(entry)
        self.assertIn(policy.RULE_CODE, {item["field"] for item in errors})


class AdmissionTests(UrlfilterBase):
    def test_conflict_resolves_to_single_deny(self) -> None:
        rule = dict(BASE_RULE, **{policy.EXCLUDE: "/admin"})
        first = policy.evaluate_url("https://site.example.com/admin/x", rule)
        second = policy.evaluate_url("https://site.example.com/admin/x", rule)
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertFalse(first.admitted)
        self.assertTrue(first.conflict)
        self.assertEqual(first.primary_hit.field, policy.PATH_BLACKLIST)

    def test_normalization_parameters_change_the_url(self) -> None:
        decision = policy.evaluate_url(
            "https://Site.Example.com/a/?b=2&utm_source=x&a=1#frag",
            dict(BASE_RULE, **{policy.EXCLUDE: "secret="}),
        )
        self.assertEqual(decision.normalized, "https://site.example.com/a?a=1&b=2")
        self.assertEqual(decision.normalization["removed_params"], ["utm_source"])
        self.assertEqual(decision.normalization["original_path"], "/a/")
        self.assertEqual(decision.normalization["final_path"], "/a")

    def test_candidate_errors_are_locatable(self) -> None:
        _, errors = policy.build_decisions(
            ["ftp://site.example.com/a", "https://site.example.com/a b"],
            BASE_RULE,
        )
        self.assertEqual(len(errors), 2)
        self.assertEqual([item["index"] for item in errors], [1, 2])


class SnapshotTests(UrlfilterBase):
    def test_snapshot_contains_full_rule_and_deduplicated_urls(self) -> None:
        rule_id, snapshot_id = self.create_and_activate()
        snapshot = self.service.snapshot_rule(snapshot_id)
        assert snapshot is not None
        for field in policy.RULE_CONTENT_FIELDS:
            self.assertIn(field, snapshot["规则快照"])
        for field in (policy.CASE_MODE, policy.TRAILING_SLASH, policy.PARAM_STRIP, policy.PATH_BLACKLIST):
            self.assertIn(field, snapshot["规则快照"])
        urls = self.service.list_snapshot_urls(snapshot_id)
        self.assertEqual(len(urls), 4)  # 5 个候选中两条规范化后重复
        self.assertEqual(sum(item["重复次数"] for item in urls), 5)
        decisions = {item["规范化URL"]: item["决定"] for item in urls}
        self.assertEqual(decisions["https://site.example.com/a?b=2"], "准入")
        self.assertEqual(decisions["https://site.example.com/admin/x"], "拒绝")
        self.assertEqual(decisions["https://site.example.com/secret=1"], "拒绝")
        self.assertEqual(decisions["https://evil.example.com/x"], "拒绝")

    def test_repeated_activation_is_idempotent(self) -> None:
        rule_id, snapshot_id = self.create_and_activate()
        before = len(store.aux_rows("urlfilter_snapshot"))
        self.service.run_action(rule_id, "生效规则")
        self.service.run_action(rule_id, "生效规则")
        self.assertEqual(len(store.aux_rows("urlfilter_snapshot")), before)

    def test_concurrent_activation_creates_one_snapshot(self) -> None:
        entry, _, errors = self.service.create_entry(dict(BASE_RULE))
        self.assertIsNotNone(entry, errors)
        rule_id = entry["id"]
        barrier = threading.Barrier(12)
        results: list[tuple] = []

        def worker() -> None:
            barrier.wait()
            results.append(self.service.run_action(rule_id, "生效规则"))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        snapshots = [item for item in store.aux_rows("urlfilter_snapshot") if item["规则id"] == rule_id]
        landing = [row for row in store.rows("rendertask") if row.get("规则编号") == BASE_RULE[policy.RULE_CODE]]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(landing), 1)

    def test_verify_replays_every_url_against_snapshot_rule(self) -> None:
        _, snapshot_id = self.create_and_activate()
        result, message = self.service.verify_snapshot(snapshot_id)
        self.assertIsNotNone(result, message)
        self.assertTrue(result["一致"], result["不一致项"])
        self.assertEqual(result["复核URL数"], 4)

    def test_new_version_keeps_old_snapshot_and_urls(self) -> None:
        rule_id, old_snapshot = self.create_and_activate()
        old_urls = self.service.list_snapshot_urls(old_snapshot)
        updated, message, errors = self.service.update_entry(
            rule_id,
            {policy.CANDIDATES: ["https://site.example.com/new"]},
            expected_version=1,
        )
        self.assertIsNotNone(updated, (message, errors))
        self.assertEqual(updated["version"], 2)
        _, new_message = self.service.run_action(rule_id, "生效规则")
        self.assertIn("快照", new_message)
        new_row = store.find("urlfilter", rule_id)
        self.assertNotEqual(int(new_row["当前快照"]), old_snapshot)
        self.assertEqual(self.service.list_snapshot_urls(old_snapshot), old_urls)
        result, _ = self.service.verify_snapshot(old_snapshot)
        self.assertTrue(result["一致"])

    def test_stale_version_update_is_rejected(self) -> None:
        rule_id, _ = self.create_and_activate()
        self.service.update_entry(rule_id, {policy.CANDIDATES: []}, expected_version=1)
        entry, message, errors = self.service.update_entry(
            rule_id, {policy.CANDIDATES: []}, expected_version=1
        )
        self.assertIsNone(entry)
        self.assertIn("版本", message)
        self.assertEqual(errors[0]["field"], "版本")


class LandingAndJobTests(UrlfilterBase):
    def test_landing_sync_is_idempotent_and_skips_rejected_urls(self) -> None:
        _, snapshot_id = self.create_and_activate()
        landing = [row for row in store.rows("rendertask") if row.get("过滤规则快照") == snapshot_id]
        self.assertEqual(len(landing), 1)
        self.assertTrue(all(row["准入结果"] == "准入" for row in landing))
        first_count = len(store.rows("rendertask"))
        self.service.sync_snapshot(snapshot_id)
        self.service.sync_snapshot(snapshot_id)
        self.assertEqual(len(store.rows("rendertask")), first_count)
        records = self.service.list_sync_records(snapshot_id)
        self.assertGreaterEqual(len(records), 1)

    def test_landing_rejects_url_outside_snapshot(self) -> None:
        _, snapshot_id = self.create_and_activate()
        service = RendertaskService()
        entry, missing, errors = service.create_entry({
            "作业编号": "LAND-TEST",
            "目标URL": "https://site.example.com/admin/x",
            "浏览器版本": "chromium",
            "过滤规则快照": snapshot_id,
        })
        self.assertIsNone(entry)
        self.assertTrue(errors)

    def test_job_execution_params_exclude_rejected_urls(self) -> None:
        _, snapshot_id = self.create_and_activate()
        service = CrawljobService()
        entry, missing, errors = service.create_entry({
            "作业编号": "CRAW-TEST",
            "目标站点": "site",
            "起始URL": "https://site.example.com/start?utm_source=x",
            "候选URL": ["https://site.example.com/admin/y", "https://site.example.com/ok"],
            "过滤规则快照": snapshot_id,
        })
        self.assertIsNotNone(entry, (missing, errors))
        started, message = service.run_action(entry["id"], "启动作业")
        self.assertIsNotNone(started, message)
        params = started["执行参数"]
        self.assertEqual(
            params["准入URL清单"],
            ["https://site.example.com/start", "https://site.example.com/ok"],
        )
        self.assertEqual(
            [item["规范化URL"] for item in params["拦截URL清单"]],
            ["https://site.example.com/admin/y"],
        )

    def test_job_cannot_start_with_rejected_start_url(self) -> None:
        _, snapshot_id = self.create_and_activate()
        service = CrawljobService()
        entry, _, errors = service.create_entry({
            "作业编号": "CRAW-BLOCKED",
            "目标站点": "site",
            "起始URL": "https://site.example.com/admin/z",
            "过滤规则快照": snapshot_id,
        })
        self.assertIsNotNone(entry, errors)
        started, message = service.run_action(entry["id"], "启动作业")
        self.assertIsNone(started)
        self.assertIn("起始URL", message)


if __name__ == "__main__":
    unittest.main()
