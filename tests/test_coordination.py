import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from src.domain import Actor, ConflictError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None):
        return self.service.transition(self.actor, entity["id"], action, data or {})

    def _topology(self):
        """M-01 <-> M-02 <-> M-03 (open); M-01 <-> M-04 blocked."""
        p1 = self.create("passage", {"from_location": "M-01", "to_location": "M-02", "width_m": 3})
        p2 = self.create("passage", {"from_location": "M-02", "to_location": "M-03", "width_m": 3})
        p3 = self.create("passage", {"from_location": "M-01", "to_location": "M-04", "width_m": 3})
        self.act(p3, "block")
        sensor = self.create("sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        fan1 = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        fan2 = self.create("ventilation", {"name": "fan-2", "area_code": "M-02", "capacity": 100})
        fan3 = self.create("ventilation", {"name": "fan-3", "area_code": "M-04", "capacity": 100})
        r1 = self.create("refuge", {"location_code": "M-01", "capacity": 10})
        r2 = self.create("refuge", {"location_code": "M-02", "capacity": 10})
        w1 = self.create("worker", {"name": "Li Wei", "location_code": "M-01", "team": "A"})
        w2 = self.create("worker", {"name": "Wang Fang", "location_code": "M-02", "team": "B"})
        w3 = self.create("worker", {"name": "Zhao Min", "location_code": "M-04", "team": "C"})
        self.act(w1, "mark_missing")
        self.act(w2, "mark_missing")
        return dict(p1=p1, p2=p2, p3=p3, sensor=sensor, fan1=fan1, fan2=fan2, fan3=fan3,
                    r1=r1, r2=r2, w1=w1, w2=w2, w3=w3)

    def _steps_by_type(self, order):
        result = {}
        for step in order["data"]["steps"]:
            result.setdefault(step["type"], []).append(step)
        return result

    def test_alarm_creates_one_order_and_duplicate_returns_original(self):
        topo = self._topology()
        first = self.service.activate_alarm(self.actor, {"sensor_id": topo["sensor"]["id"]})
        self.assertFalse(first["duplicate"])
        second = self.service.activate_alarm(self.actor, {"sensor_id": topo["sensor"]["id"]})
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["order"]["id"], second["order"]["id"])
        orders = self.service.list("coordination_order")
        self.assertEqual(len(orders), 1)

    def test_affected_areas_follow_open_passages_only(self):
        topo = self._topology()
        result = self.service.activate_alarm(self.actor, {"sensor_id": topo["sensor"]["id"]})
        affected = result["order"]["data"]["affected_areas"]
        self.assertIn("M-01", affected)
        self.assertIn("M-02", affected)
        self.assertIn("M-03", affected)
        self.assertNotIn("M-04", affected)

    def test_steps_execute_in_order_and_complete(self):
        topo = self._topology()
        result = self.service.activate_alarm(self.actor, {"sensor_id": topo["sensor"]["id"]})
        order = result["order"]
        self.assertEqual(order["status"], "completed")
        steps = self._steps_by_type(order)
        # isolate passages touching affected areas
        self.assertEqual(len(steps["isolate"]), 2)
        self.assertTrue(all(s["status"] == "done" for s in steps["isolate"]))
        # fans in affected areas started
        self.assertEqual(len(steps["fan"]), 2)
        self.assertTrue(all(s["status"] == "done" for s in steps["fan"]))
        # refuges occupied, tasks assigned
        self.assertTrue(all(s["status"] == "done" for s in steps["refuge"]))
        self.assertTrue(all(s["status"] == "done" for s in steps["task"]))
        # side effects actually applied
        self.assertEqual(self.service.get(topo["p1"]["id"])["status"], "blocked")
        self.assertEqual(self.service.get(topo["p2"]["id"])["status"], "blocked")
        self.assertEqual(self.service.get(topo["fan1"]["id"])["status"], "running")
        self.assertEqual(self.service.get(topo["fan2"]["id"])["status"], "running")
        self.assertEqual(self.service.get(topo["r1"]["id"])["status"], "occupied")
        self.assertEqual(self.service.get(topo["r2"]["id"])["status"], "occupied")
        # every done step records executor and time
        for step in order["data"]["steps"]:
            self.assertEqual(step["status"], "done")
            self.assertEqual(step["executor"], self.actor.user_id)
            self.assertTrue(step["executed_at"])

    def test_refuge_capacity_shortage_pending_then_retry_completes(self):
        sensor = self.create("sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        self.create("refuge", {"location_code": "M-01", "capacity": 2})
        for i in range(3):
            self.create("worker", {"name": "worker-%d" % i, "location_code": "M-01", "team": "A"})
        result = self.service.activate_alarm(self.actor, {"sensor_id": sensor["id"]})
        order = result["order"]
        refuge_steps = [s for s in order["data"]["steps"] if s["type"] == "refuge"]
        self.assertEqual(len(refuge_steps), 1)
        self.assertEqual(refuge_steps[0]["status"], "pending")
        self.assertEqual(order["status"], "active")
        # a larger refuge becomes available
        self.create("refuge", {"location_code": "M-01", "capacity": 8})
        order = self.service.transition(self.actor, order["id"], "process")
        refuge_steps = [s for s in order["data"]["steps"] if s["type"] == "refuge"]
        self.assertEqual(refuge_steps[0]["status"], "done")
        self.assertEqual(order["status"], "completed")
        # retry must not occupy again: only one refuge occupied
        occupied = [r for r in self.service.list("refuge") if r["status"] == "occupied"]
        self.assertEqual(len(occupied), 1)
        order = self.service.transition(self.actor, order["id"], "process")
        self.assertEqual(order["status"], "completed")
        occupied = [r for r in self.service.list("refuge") if r["status"] == "occupied"]
        self.assertEqual(len(occupied), 1)

    def test_fan_fault_pending_no_rollback_then_retry_starts(self):
        sensor = self.create("sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        fan = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        self.act(fan, "report_fault")
        passage = self.create("passage", {"from_location": "M-01", "to_location": "M-02", "width_m": 3})
        result = self.service.activate_alarm(self.actor, {"sensor_id": sensor["id"]})
        order = result["order"]
        steps = self._steps_by_type(order)
        self.assertEqual(steps["fan"][0]["status"], "pending")
        self.assertEqual(steps["isolate"][0]["status"], "done")
        self.assertEqual(self.service.get(passage["id"])["status"], "blocked")
        self.assertEqual(order["status"], "active")
        # repair then retry
        self.act(fan, "repair")
        order = self.service.transition(self.actor, order["id"], "process")
        steps = self._steps_by_type(order)
        self.assertEqual(steps["fan"][0]["status"], "done")
        self.assertEqual(self.service.get(fan["id"])["status"], "running")
        # isolation must not be rolled back by the earlier fan failure
        self.assertEqual(self.service.get(passage["id"])["status"], "blocked")
        self.assertEqual(order["status"], "completed")

    def test_retry_does_not_duplicate_tasks(self):
        sensor = self.create("sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        self.create("refuge", {"location_code": "M-01", "capacity": 10})
        worker = self.create("worker", {"name": "Li Wei", "location_code": "M-01", "team": "A"})
        self.act(worker, "mark_missing")
        result = self.service.activate_alarm(self.actor, {"sensor_id": sensor["id"]})
        order = result["order"]
        self.assertEqual(order["status"], "completed")
        tasks = self.service.list("task")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["data"]["dedupe_key"], "rescue:" + worker["id"])
        # retry: no duplicate task
        order = self.service.transition(self.actor, order["id"], "process")
        self.assertEqual(len(self.service.list("task")), 1)
        self.assertEqual(order["status"], "completed")

    def test_close_requires_order_completed_or_cancelled(self):
        sensor = self.create("sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        fan = self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        self.act(fan, "report_fault")
        result = self.service.activate_alarm(self.actor, {"sensor_id": sensor["id"]})
        incident = result["incident"]
        order = result["order"]
        self.assertEqual(order["status"], "active")
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "done"})
        # complete the order then close
        self.act(fan, "repair")
        order = self.service.transition(self.actor, order["id"], "process")
        self.assertEqual(order["status"], "completed")
        incident = self.act(incident, "close", {"summary": "all clear"})
        self.assertEqual(incident["status"], "closed")

    def test_cancelled_order_allows_close(self):
        sensor = self.create("sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        self.create("refuge", {"location_code": "M-01", "capacity": 2})
        for i in range(3):
            self.create("worker", {"name": "worker-%d" % i, "location_code": "M-01", "team": "A"})
        result = self.service.activate_alarm(self.actor, {"sensor_id": sensor["id"]})
        incident = result["incident"]
        order = result["order"]
        self.assertEqual(order["status"], "active")
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        order = self.service.transition(self.actor, order["id"], "cancel", {"reason": "false alarm"})
        self.assertEqual(order["status"], "cancelled")
        incident = self.act(incident, "close", {"summary": "all clear"})
        self.assertEqual(incident["status"], "closed")

    def test_old_incident_without_order_closes_normally(self):
        incident = self.create("incident", {"area_code": "M-09", "severity": "high", "summary": "x"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        incident = self.act(incident, "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")

    def test_activate_by_area_code_without_sensor(self):
        result = self.service.activate_alarm(self.actor, {"area_code": "M-01"})
        self.assertEqual(result["order"]["data"]["area_code"], "M-01")
        self.assertEqual(result["duplicate"], False)
        again = self.service.activate_alarm(self.actor, {"area_code": "M-01"})
        self.assertTrue(again["duplicate"])


class CoordinationHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("admin", "admin")
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), str(Path(__file__).resolve().parent.parent / "static"))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, path, body):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-User-Id": "admin", "X-Role": "admin"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_http_activate_returns_order_with_steps(self):
        sensor = self.service.create(self.actor, "sensor", {"location_code": "M-01", "gas_ppm": 120, "threshold_ppm": 80})
        self.service.create(self.actor, "ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})
        self.service.create(self.actor, "refuge", {"location_code": "M-01", "capacity": 10})
        result = self._post("/api/coordination-orders", {"sensor_id": sensor["id"]})
        self.assertIn("order", result)
        self.assertEqual(result["order"]["data"]["area_code"], "M-01")
        types = {s["type"] for s in result["order"]["data"]["steps"]}
        self.assertIn("fan", types)
        self.assertIn("refuge", types)


if __name__ == "__main__":
    unittest.main()
