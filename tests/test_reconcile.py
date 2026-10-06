import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "primary_object_id": "SAT-1",
            "secondary_object_id": "DEB-9",
            "tca": "2026-09-28T12:00:00+00:00",
            "miss_distance_m": 120,
            "covariance_m": 100,
            "fuel_budget_m_s": 5,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A", "Org-B"],
        }, "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _source(self, external_id, distance, covariance, observed_at):
        return {
            "source_type": "radar",
            "external_id": external_id,
            "observed_at": observed_at,
            "miss_distance_m": distance,
            "covariance_m": covariance,
        }

    def test_source_upsert_latest_wins(self):
        first = self.service.add_source(
            self.item["id"], self._source("R-1", 100, 100, "2026-09-20T00:00:00+00:00"),
            "analyst-1", "analyst",
        )
        self.assertTrue(first["changed"])
        second = self.service.add_source(
            self.item["id"], self._source("R-1", 50, 120, "2026-09-21T00:00:00+00:00"),
            "analyst-1", "analyst",
        )
        self.assertTrue(second["changed"])
        sources = self.repo.list_sources(self.item["id"])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["payload"]["miss_distance_m"], 50)
        self.assertEqual(sources[0]["payload"]["covariance_m"], 120)

    def test_idempotent_duplicate_submission(self):
        payload = self._source("R-1", 100, 100, "2026-09-20T00:00:00+00:00")
        first = self.service.add_source(self.item["id"], payload, "analyst-1", "analyst")
        self.assertTrue(first["changed"])
        after_first = self.repo.get_item(self.item["id"])
        second = self.service.add_source(self.item["id"], payload, "analyst-1", "analyst")
        self.assertFalse(second["changed"])
        after_second = self.repo.get_item(self.item["id"])
        self.assertEqual(after_second["version"], after_first["version"])
        self.assertEqual(after_second["payload"]["miss_distance_m"], 100)

    def test_source_change_invalidates_and_recomputes(self):
        self.service.act(self.item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", self.item["version"])
        before = self.repo.get_item(self.item["id"])
        self.assertEqual(before["payload"]["assessment"]["level"], "high")
        self.service.add_source(
            self.item["id"], self._source("R-1", 2000, 100, "2026-09-22T00:00:00+00:00"),
            "analyst-1", "analyst",
        )
        after = self.repo.get_item(self.item["id"])
        self.assertEqual(after["payload"]["miss_distance_m"], 2000)
        self.assertEqual(after["payload"]["assessment"]["level"], "low")
        self.assertFalse(after["payload"]["assessment_stale"])

    def test_source_change_makes_prior_opinions_stale(self):
        assessed = self.service.act(self.item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", self.item["version"])
        self.service.act(self.item["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"}, "operator-1", "operator", assessed["version"])
        self.service.add_source(
            self.item["id"], self._source("R-1", 2000, 100, "2026-09-22T00:00:00+00:00"),
            "analyst-1", "analyst",
        )
        item = self.repo.get_item(self.item["id"])
        self.assertTrue(all(entry.get("stale") for entry in item["payload"]["opinions"]))
        # 旧意见不再算作已确认
        self.service.act(self.item["id"], "approve", {
            "fuel_cost_m_s": 1, "maneuver_window": "w",
        }, "coordinator-1", "coordinator", item["version"])
        item = self.repo.get_item(self.item["id"])
        self.assertEqual(item["status"], "pending_confirmation")
        self.assertEqual(set(item["payload"]["pending_confirmation"]["pending"]), {"Org-A", "Org-B"})

    def test_reconcile_recovers_from_sources(self):
        self.service.add_source(
            self.item["id"], self._source("R-1", 100, 100, "2026-09-20T00:00:00+00:00"),
            "analyst-1", "analyst",
        )
        good = self.repo.get_item(self.item["id"])
        self.assertIsNotNone(good["payload"]["assessment"])
        # 绕过校验写入一条无效观测，使重算失败
        self.repo.upsert_source(
            self.item["id"], "radar", "R-1",
            {"miss_distance_m": 100, "covariance_m": 0, "region": None, "operator": None},
            "2026-09-21T00:00:00+00:00", "analyst-1", "analyst",
        )
        self.service.act(self.item["id"], "reconcile", {}, "analyst-1", "analyst", self.repo.get_item(self.item["id"])["version"])
        failed = self.repo.get_item(self.item["id"])
        self.assertTrue(failed["payload"]["assessment_stale"])
        self.assertIsNone(failed["payload"]["assessment"])
        # 来源记录仍在；修正后再次重算即可恢复
        self.service.add_source(
            self.item["id"], self._source("R-1", 100, 100, "2026-09-22T00:00:00+00:00"),
            "analyst-1", "analyst",
        )
        recovered = self.repo.get_item(self.item["id"])
        self.assertFalse(recovered["payload"]["assessment_stale"])
        self.assertIsNotNone(recovered["payload"]["assessment"])

    def test_all_operators_confirm_releases_action(self):
        assessed = self.service.act(self.item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", self.item["version"])
        held = self.service.act(self.item["id"], "approve", {
            "fuel_cost_m_s": 1, "maneuver_window": "w",
        }, "coordinator-1", "coordinator", assessed["version"])
        self.assertEqual(held["status"], "pending_confirmation")
        self.service.act(self.item["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"}, "operator-1", "operator", held["version"])
        last = self.service.act(self.item["id"], "record_opinion", {"operator": "Org-B", "opinion": "approve"}, "operator-1", "operator", self.repo.get_item(self.item["id"])["version"])
        self.assertEqual(last["status"], "coordinating")

    def test_reject_blocks_release(self):
        assessed = self.service.act(self.item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", self.item["version"])
        opinion = self.service.act(self.item["id"], "record_opinion", {"operator": "Org-A", "opinion": "reject", "reason": "unsafe"}, "operator-1", "operator", assessed["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(self.item["id"], "approve", {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "coordinator-1", "coordinator", opinion["version"])
        self.assertEqual(context.exception.code, "unresolved_conflict")


if __name__ == "__main__":
    unittest.main()
