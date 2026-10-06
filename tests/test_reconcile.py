import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src import rules
from src.domain import ConflictError, DomainError
from src.http_api import build_handler
from src.repository import Repository
from src.service import Service
from http.server import ThreadingHTTPServer


def make_source(source_type, external_id, observed_at, distance, covariance, **extra):
    payload = {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "miss_distance_m": distance,
        "covariance_m": covariance,
    }
    payload.update(extra)
    return payload


class ReconcileChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "primary_object_id": "SAT-7",
            "secondary_object_id": "DEB-2",
            "tca": "2026-10-08T12:00:00+00:00",
            "miss_distance_m": 1000,
            "covariance_m": 500,
            "fuel_budget_m_s": 6,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A", "Org-B"],
        }, "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _source(self, *args, **kwargs):
        result, created = self.service.add_source(self.item["id"], make_source(*args, **kwargs), "analyst-1", "analyst")
        return result, created

    def test_latest_observation_per_source_is_used(self):
        # 同一来源先报近距，后报远距；对账必须取后者。
        self._source("radar", "R-1", "2026-10-07T00:00:00Z", 10, 100)
        result, created = self._source("radar", "R-1", "2026-10-07T01:00:00Z", 800, 100)
        self.assertTrue(created)
        self.assertAlmostEqual(result["reconciled"]["miss_distance_m"], 800.0, places=3)
        # 旧于已知最新观测的晚到记录不能回退数据。
        with self.assertRaises(ConflictError) as context:
            self._source("radar", "R-1", "2026-10-07T00:30:00Z", 5, 100)
        self.assertEqual(context.exception.code, "stale_observation")

    def test_multi_source_precision_weighted_fusion(self):
        # 高精度（协方差小）的来源应主导融合距离。
        self._source("radar", "R-1", "2026-10-07T01:00:00Z", 100, 25)
        result, _ = self._source("optical", "O-9", "2026-10-07T01:05:00Z", 200, 100)
        # 权重 1/25=0.04 与 1/100=0.01；融合距离=(100*0.04+200*0.01)/0.05=120。
        self.assertAlmostEqual(result["reconciled"]["miss_distance_m"], 120.0, places=3)
        # 融合协方差=1/0.05=20。
        self.assertAlmostEqual(result["reconciled"]["covariance_m"], 20.0, places=6)
        self.assertEqual(result["reconciled"]["source_count"], 2)

    def test_concurrent_duplicate_submissions_are_idempotent(self):
        body = make_source("radar", "R-2", "2026-10-07T02:00:00Z", 300, 80)
        outcomes = []

        def submit():
            try:
                result, created = self.service.add_source(self.item["id"], json.loads(json.dumps(body)), "a", "analyst")
                outcomes.append((created, result.get("duplicated", False)))
            except Exception as exc:  # pragma: no cover - 测试失败时暴露
                outcomes.append(("error", str(exc)))

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        created = [outcome for outcome in outcomes if outcome == (True, False)]
        duplicates = [outcome for outcome in outcomes if outcome == (False, True)]
        self.assertEqual(len(created), 1, outcomes)
        self.assertEqual(len(duplicates), 7, outcomes)
        sources = self.repo.list_sources(self.item["id"])
        self.assertEqual(len(sources), 1)
        item = self.service.get_item(self.item["id"])
        # 未评估过的待处理实体，基线数据被对账刷新但不产生评估。
        self.assertIsNone(item["payload"]["assessment_state"])
        self.assertAlmostEqual(item["payload"]["miss_distance_m"], 300.0, places=3)

    def test_same_payload_repeat_does_not_change_result(self):
        self._source("radar", "R-3", "2026-10-07T03:00:00Z", 300, 80)
        before = self.service.get_item(self.item["id"])
        result, created = self._source("radar", "R-3", "2026-10-07T03:00:00Z", 300, 80)
        after = self.service.get_item(self.item["id"])
        self.assertFalse(created)
        self.assertTrue(result["duplicated"])
        self.assertEqual(after["version"], before["version"])

    def test_timezone_variants_compare_by_instant(self):
        # 同一时刻的 Z 与 +00:00 写法视为重复，不产生第二条记录。
        self._source("radar", "R-7", "2026-10-07T03:00:00Z", 300, 80)
        result, created = self._source("radar", "R-7", "2026-10-07T03:00:00+00:00", 300, 80)
        self.assertFalse(created)
        self.assertTrue(result["duplicated"])
        # +02:00 的 03:00 实际早于 UTC 03:00，晚到提交应判旧。
        with self.assertRaises(ConflictError):
            self._source("radar", "R-7", "2026-10-07T03:00:00+02:00", 300, 80)
        # UTC 04:00（字符串更大）确实更新，可提交。
        result, created = self._source("radar", "R-7", "2026-10-07T04:00:00Z", 310, 80)
        self.assertTrue(created)

    def test_source_change_invalidates_assessment_opinions_and_maneuver(self):
        item = self.item
        result, _ = self._source("radar", "R-4", "2026-10-07T04:00:00Z", 10, 100)
        item = result["item"]
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 10}, "a", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "high")
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2, "maneuver_window": "2026-10-08T06:00:00Z/07:00:00Z",
        }, "c", "coordinator", item["version"])
        self.assertEqual(item["status"], "maneuver_pending")
        item = self.service.act(item["id"], "record_opinion", {
            "operator": "Org-A", "opinion": "approve",
        }, "operator-1", "operator", item["version"])
        self.assertEqual(item["payload"]["opinions"][0]["opinion"], "approve")

        # 晚到的新观测：风险已大幅降低（远距），链路必须回退重算。
        result, _ = self._source("radar", "R-4", "2026-10-07T05:00:00Z", 2000, 100)
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "assessed", "失效后不能停在待确认/协调中")
        self.assertEqual(item["payload"]["assessment"]["level"], "low")
        self.assertEqual(item["payload"]["opinions"], [])
        self.assertFalse(item["payload"]["conflict"])
        self.assertNotIn("approved_maneuver", item["payload"])
        # 未重新评估/批准前，规避动作不能执行。
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "execute", {"command_ref": "C"}, "operator-1", "operator", item["version"])
        self.assertEqual(context.exception.code, "invalid_state")
        # 新来源指纹已更新，再交同一旧观测被拒（stale）。
        with self.assertRaises(ConflictError):
            self._source("radar", "R-4", "2026-10-07T04:30:00Z", 10, 100)

    def test_maneuver_waits_for_all_operators(self):
        item = self.item
        result, _ = self._source("radar", "R-5", "2026-10-07T06:00:00Z", 20, 100)
        item = result["item"]
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 8}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 1, "maneuver_window": "w",
        }, "c", "coordinator", item["version"])
        # 未到全部确认：execute 被状态机拒绝。
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "execute", {"command_ref": "C"}, "operator-1", "operator", item["version"])
        self.assertEqual(context.exception.code, "invalid_state")
        # 阻断意见也保持不放行。
        item = self.service.act(item["id"], "record_opinion", {
            "operator": "Org-A", "opinion": "reject",
        }, "operator-1", "operator", item["version"])
        self.assertEqual(item["status"], "maneuver_pending")
        # 协调方重新提建议，两个运营方都 approve 才释放。
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 1, "maneuver_window": "w2",
        }, "c", "coordinator", item["version"])
        item = self.service.act(item["id"], "record_opinion", {
            "operator": "Org-A", "opinion": "approve",
        }, "operator-1", "operator", item["version"])
        self.assertEqual(item["status"], "maneuver_pending")
        item = self.service.act(item["id"], "record_opinion", {
            "operator": "Org-B", "opinion": "approve",
        }, "operator-2", "operator", item["version"])
        self.assertEqual(item["status"], "coordinating")

    def test_failed_recompute_can_recover_from_sources(self):
        item = self.item
        result, _ = self._source("radar", "R-6", "2026-10-07T07:00:00Z", 30, 100)
        item = result["item"]
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 8}, "a", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment_state"], "current")

        # 让重算失败（模拟规则引擎故障：轨道过期），来源记录必须仍然落库。
        original = rules.assess

        def failing_assess(payload):
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")

        rules.assess = failing_assess
        try:
            self._source("radar", "R-6", "2026-10-07T08:00:00Z", 40, 100)
        finally:
            rules.assess = original

        item = self.service.get_item(item["id"])
        self.assertEqual(item["payload"]["assessment_state"], "failed")
        self.assertEqual(item["payload"]["assessment_error"]["code"], "stale_track")
        self.assertEqual(item["status"], "assessed")
        # 失败期间不能继续评估或批准。
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "assess", {"hours_to_tca": 8}, "a", "analyst", item["version"])
        self.assertEqual(context.exception.code, "assessment_failed")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "approve", {"fuel_cost_m_s": 1, "maneuver_window": "w"},
                             "c", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "assessment_not_current")
        # 引擎恢复后，从来源记录重算恢复。
        item = self.service.act(item["id"], "recover_assessment", {}, "analyst-1", "analyst")
        self.assertEqual(item["payload"]["assessment_state"], "current")
        self.assertNotIn("assessment_error", item["payload"])
        self.assertAlmostEqual(item["payload"]["miss_distance_m"], 40.0, places=3)
        self.assertIsNotNone(item["payload"]["assessment"])

    def test_recovery_requires_failure_state(self):
        with self.assertRaises(DomainError) as context:
            self.service.act(self.item["id"], "recover_assessment", {}, "analyst-1", "analyst")
        self.assertEqual(context.exception.code, "assessment_current")


class SourceHttpConcurrencyTest(unittest.TestCase):
    """通过真实 HTTP 服务验证并发重复提交的对外行为。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "primary_object_id": "SAT-8",
            "secondary_object_id": "DEB-4",
            "tca": "2026-10-09T12:00:00+00:00",
            "miss_distance_m": 500,
            "covariance_m": 200,
            "fuel_budget_m_s": 3,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A"],
        }, "analyst-1", "analyst")
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.service, static_dir))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        os.unlink(self.tmp.name)

    def test_duplicate_posts_over_http(self):
        body = json.dumps(make_source("radar", "HTTP-1", "2026-10-07T09:00:00Z", 250, 90))
        statuses = []
        lock = threading.Lock()

        def post():
            conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request(
                "POST", "/api/items/%d/sources" % self.item["id"], body,
                {"Content-Type": "application/json", "X-User-Id": "a", "X-Role": "analyst"},
            )
            response = conn.getresponse()
            response.read()
            conn.close()
            with lock:
                statuses.append(response.status)

        threads = [threading.Thread(target=post) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(statuses), [200, 200, 200, 200, 201])
        self.assertEqual(len(self.repo.list_sources(self.item["id"])), 1)


if __name__ == "__main__":
    unittest.main()
