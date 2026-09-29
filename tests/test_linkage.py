import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LinkageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.dispatcher = Actor("dispatcher-1", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data, actor=None):
        return self.service.create(actor or self.admin, kind, data)

    def act(self, entity, action, data=None, actor=None):
        return self.service.transition(actor or self.admin, entity["id"], action, data or {})

    def build_scene(self, fan_data=None, refuges=(("refuge-1", "M-02", 2),), missing=("M-01", "M-03")):
        """M-01(污染源) - M-02 - M-03 通道可用；M-03 - M-04 已封锁，M-04 不受影响。"""
        incident = self.create("incident", {"id": "inc-1", "area_code": "M-01", "severity": "critical", "summary": "gas leak"})
        self.create("passage", {"id": "passage-1", "from_location": "M-01", "to_location": "M-02", "width_m": 3})
        self.create("passage", {"id": "passage-2", "from_location": "M-02", "to_location": "M-03", "width_m": 3})
        passage3 = self.create("passage", {"id": "passage-3", "from_location": "M-03", "to_location": "M-04", "width_m": 3})
        self.act(passage3, "block", {"reason": "collapsed"})
        fan_payload = {"id": "fan-m2", "name": "fan-m2", "area_code": "M-02", "capacity": 100}
        fan_payload.update(fan_data or {})
        fan = self.create("ventilation", fan_payload)
        self.act(fan, "stop")
        for refuge_id, location, capacity in refuges:
            self.create("refuge", {"id": refuge_id, "location_code": location, "capacity": capacity})
        for index, location in enumerate(missing, start=1):
            worker = self.create("worker", {"id": "worker-%d" % index, "name": "Worker %d" % index, "location_code": location, "team": "A"})
            self.act(worker, "mark_missing")
        return incident

    def steps_of(self, linkage):
        return {step["name"]: step for step in linkage["data"]["steps"]}

    def test_linkage_created_once_per_incident_and_area(self):
        self.build_scene()
        first = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=self.dispatcher)
        self.assertEqual(first["status"], "active")
        self.assertEqual(first["data"]["affected_areas"], ["M-01", "M-02", "M-03"])
        self.assertEqual([step["name"] for step in first["data"]["steps"]],
                         ["isolate_passages", "start_fans", "occupy_refuges", "dispatch_tasks"])
        self.assertTrue(all(step["status"] == "pending" for step in first["data"]["steps"]))

        duplicate = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=self.dispatcher)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(len(self.service.list("linkage")), 1)

        other_area = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-03"}, actor=self.dispatcher)
        self.assertNotEqual(other_area["id"], first["id"])
        self.assertEqual(len(self.service.list("linkage")), 2)

    def test_execute_runs_all_steps_in_order(self):
        self.build_scene()
        linkage = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=self.dispatcher)
        linkage = self.act(linkage, "execute", actor=self.dispatcher)
        self.assertEqual(linkage["status"], "completed")

        steps = self.steps_of(linkage)
        for name in ("isolate_passages", "start_fans", "occupy_refuges", "dispatch_tasks"):
            self.assertEqual(steps[name]["status"], "done")
            self.assertEqual(steps[name]["executed_by"], "dispatcher-1")
            self.assertTrue(steps[name]["executed_at"])

        self.assertEqual(self.service.get("passage-1")["status"], "blocked")
        self.assertEqual(self.service.get("passage-2")["status"], "blocked")
        self.assertEqual(self.service.get("passage-3")["status"], "blocked")
        self.assertEqual(self.service.get("fan-m2")["status"], "running")
        self.assertEqual(self.service.get("refuge-1")["status"], "occupied")

        tasks = self.service.list("task")
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task["status"] == "assigned" for task in tasks))
        self.assertEqual({task["data"]["target"] for task in tasks}, {"worker-1", "worker-2"})

        audit = self.service.audit_log(linkage["id"])
        self.assertEqual([entry["action"] for entry in audit], ["create", "execute"])
        self.assertEqual(audit[-1]["actor_id"], "dispatcher-1")

    def test_failed_fan_keeps_isolation_and_retry_is_idempotent(self):
        self.build_scene(fan_data={"out_of_service": True})
        linkage = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=self.dispatcher)
        linkage = self.act(linkage, "execute", actor=self.dispatcher)
        self.assertEqual(linkage["status"], "active")

        steps = self.steps_of(linkage)
        self.assertEqual(steps["isolate_passages"]["status"], "done")
        self.assertEqual(steps["start_fans"]["status"], "pending")
        self.assertIn("fan-m2", steps["start_fans"]["detail"]["reason"])
        self.assertEqual(steps["occupy_refuges"]["status"], "pending")
        # 先完成的隔离不因后续风机故障回滚
        self.assertEqual(self.service.get("passage-1")["status"], "blocked")
        self.assertEqual(self.service.get("passage-2")["status"], "blocked")

        fan = self.service.get("fan-m2")
        repaired = dict(fan["data"])
        repaired["out_of_service"] = False
        self.service.repository.update_entity(fan["id"], None, fan["status"], repaired)

        linkage = self.act(linkage, "execute", actor=self.dispatcher)
        self.assertEqual(linkage["status"], "completed")
        self.assertEqual(self.service.get("fan-m2")["status"], "running")
        # 重试不重复占硐室、不重复派单，隔离保持
        self.assertEqual(len(self.service.list("task")), 2)
        occupies = [entry for entry in self.service.audit_log() if entry["action"] == "occupy"]
        self.assertEqual(len(occupies), 1)
        self.assertEqual(self.service.get("passage-1")["status"], "blocked")

    def test_insufficient_refuge_capacity_stays_pending(self):
        self.build_scene(refuges=(("refuge-1", "M-02", 2),), missing=("M-01", "M-02", "M-03"))
        linkage = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=self.dispatcher)
        linkage = self.act(linkage, "execute", actor=self.dispatcher)
        self.assertEqual(linkage["status"], "active")

        steps = self.steps_of(linkage)
        self.assertEqual(steps["start_fans"]["status"], "done")
        self.assertEqual(steps["occupy_refuges"]["status"], "pending")
        self.assertEqual(steps["occupy_refuges"]["detail"]["need"], 3)
        self.assertEqual(steps["occupy_refuges"]["detail"]["capacity"], 2)
        self.assertEqual(self.service.get("refuge-1")["status"], "occupied")
        self.assertEqual(len(self.service.list("task")), 0)

        self.create("refuge", {"id": "refuge-2", "location_code": "M-03", "capacity": 2})
        linkage = self.act(linkage, "execute", actor=self.dispatcher)
        self.assertEqual(linkage["status"], "completed")
        # 重试只补占新硐室，不重复占用已占的
        occupies = [entry for entry in self.service.audit_log() if entry["action"] == "occupy"]
        self.assertEqual(len(occupies), 2)
        self.assertEqual({entry["entity_id"] for entry in occupies}, {"refuge-1", "refuge-2"})
        self.assertEqual(len(self.service.list("task")), 3)

    def test_close_requires_linkage_completed_or_cancelled(self):
        incident = self.build_scene()
        linkage = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=self.dispatcher)
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        for worker_id in ("worker-1", "worker-2"):
            self.act(self.service.get(worker_id), "evacuate")
        self.act(self.service.get("fan-m2"), "restore", {"tested_at": "2026-09-29T08:00:00Z"})
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "done"})

        self.act(linkage, "cancel", {"reason": "false alarm"}, actor=self.dispatcher)
        incident = self.act(incident, "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")

    def test_completed_linkage_allows_close(self):
        incident = self.build_scene()
        linkage = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=self.dispatcher)
        linkage = self.act(linkage, "execute", actor=self.dispatcher)
        self.assertEqual(linkage["status"], "completed")
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        for worker_id in ("worker-1", "worker-2"):
            self.act(self.service.get(worker_id), "evacuate")
        for task in self.service.list("task"):
            task = self.act(task, "accept")
            self.act(task, "complete", {"result": "worker found"})
        incident = self.act(incident, "close", {"summary": "all clear"})
        self.assertEqual(incident["status"], "closed")

    def test_legacy_incident_without_linkage_closes_as_before(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "old event"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        incident = self.act(incident, "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")

    def test_upgrade_keeps_history(self):
        incident = self.create("incident", {"id": "inc-old", "area_code": "M-01", "severity": "high", "summary": "old event"})
        worker = self.create("worker", {"id": "worker-old", "name": "Old Worker", "location_code": "M-01", "team": "A"})
        self.act(worker, "mark_missing")
        incident = self.act(incident, "begin_evacuation")

        upgraded = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.assertEqual(upgraded.get("inc-old")["status"], "evacuating")
        self.assertEqual(upgraded.get("worker-old")["status"], "missing")
        self.assertTrue(upgraded.audit_log("inc-old"))

        for action in ("search", "stabilize", "recover"):
            incident = upgraded.transition(self.admin, "inc-old", action)
        upgraded.transition(self.admin, "worker-old", "evacuate")
        incident = upgraded.transition(self.admin, "inc-old", "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")

    def test_linkage_validation_and_permissions(self):
        self.build_scene()
        with self.assertRaises(ValidationError):
            self.create("linkage", {"incident_id": "inc-1"}, actor=self.dispatcher)
        with self.assertRaises(ValidationError):
            self.create("linkage", {"incident_id": "missing-incident", "area_code": "M-01"}, actor=self.dispatcher)
        with self.assertRaises(PermissionDenied):
            self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=Actor("viewer-1", "viewer"))

        linkage = self.create("linkage", {"incident_id": "inc-1", "area_code": "M-01"}, actor=self.dispatcher)
        with self.assertRaises(PermissionDenied):
            self.act(linkage, "execute", actor=Actor("viewer-1", "viewer"))
        with self.assertRaises(ValidationError):
            self.act(linkage, "cancel", actor=self.dispatcher)
        linkage = self.act(linkage, "cancel", {"reason": "false alarm"}, actor=self.dispatcher)
        self.assertEqual(linkage["status"], "cancelled")
        with self.assertRaises(InvalidTransition):
            self.act(linkage, "execute", actor=self.dispatcher)


if __name__ == "__main__":
    unittest.main()
