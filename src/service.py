import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if self.rules.normalize_kind(entity["kind"]) == "coordination_order" and action == "process":
            return self._process_order(actor, entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # 报警联动单（coordination order）
    # ------------------------------------------------------------------

    def activate_alarm(self, actor, payload):
        """气体报警响应：按事件+污染区域找/建一张联动单，并顺序处置。

        重复报警（同一事件+同一污染区域）返回原单，不重复建单。
        """
        payload = payload or {}
        sensor = None
        area_code = payload.get("area_code")
        sensor_id = payload.get("sensor_id")
        if sensor_id:
            sensor = self.get(sensor_id)
            if self.rules.normalize_kind(sensor["kind"]) != "sensor":
                raise ValidationError("sensor_id must reference a sensor")
            area_code = sensor["data"].get("location_code")
            if sensor["status"] != "alarm":
                sensor = self.transition(actor, sensor["id"], "raise_alarm", {})
        if not area_code:
            raise ValidationError("sensor_id or area_code is required")

        incident = self._find_open_incident(area_code)
        if not incident:
            severity = "critical" if (sensor and sensor["data"].get("severity") == "alarm") else "high"
            incident = self.create(actor, "incident", {
                "area_code": area_code,
                "severity": severity,
                "summary": "gas alarm response at " + area_code,
            })

        existing = self._find_order(incident["id"], area_code)
        if existing:
            return {"sensor": sensor, "incident": incident, "order": existing, "duplicate": True}

        affected = self._find_affected_areas(area_code)
        steps = self._build_steps(area_code, affected)
        order = self.create(actor, "coordination_order", {
            "incident_id": incident["id"],
            "area_code": area_code,
            "affected_areas": affected,
            "steps": steps,
        })
        order = self._run_steps(actor, order)
        return {"sensor": sensor, "incident": incident, "order": order, "duplicate": False}

    def _find_open_incident(self, area_code):
        for incident in self.repository.list_entities(kind="incident"):
            if incident["status"] != "closed" and incident["data"].get("area_code") == area_code:
                return incident
        return None

    def _find_order(self, incident_id, area_code):
        for order in self.repository.list_entities(kind="coordination_order"):
            if order["data"].get("incident_id") == incident_id and order["data"].get("area_code") == area_code:
                return order
        return None

    def _find_affected_areas(self, start):
        """沿可用通道（open 状态）找出污染可能波及的全部区域。"""
        adjacency = {}
        for passage in self.repository.list_entities(kind="passage"):
            if passage["status"] != "open":
                continue
            source = passage["data"].get("from_location")
            target = passage["data"].get("to_location")
            adjacency.setdefault(source, []).append(target)
            adjacency.setdefault(target, []).append(source)
        seen = {start}
        queue = [start]
        while queue:
            node = queue.pop(0)
            for neighbour in adjacency.get(node, []):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        return sorted(seen)

    def _build_steps(self, area_code, affected):
        steps = []
        for passage in self.repository.list_entities(kind="passage"):
            if passage["status"] not in ("open", "restricted"):
                continue
            source = passage["data"].get("from_location")
            target = passage["data"].get("to_location")
            if source in affected or target in affected:
                steps.append(self._step(
                    "isolate", passage["id"], area_code,
                ))
        for fan in self.repository.list_entities(kind="ventilation"):
            if fan["data"].get("area_code") in affected:
                steps.append(self._step("fan", fan["id"], area_code))
        for area in affected:
            steps.append(self._step("refuge", None, area))
        for worker in self.repository.list_entities(kind="worker"):
            if worker["status"] == "missing" and worker["data"].get("location_code") in affected:
                steps.append(self._step("task", worker["id"], area_code))
        order = {"isolate": 0, "fan": 1, "refuge": 2, "task": 3}
        steps.sort(key=lambda s: (order[s["type"]], s["key"]))
        return steps

    @staticmethod
    def _step(kind, target, area):
        return {
            "key": kind + ":" + str(target if target is not None else area),
            "type": kind,
            "target": target,
            "area": area,
            "status": "pending",
            "executor": None,
            "executed_at": None,
            "message": "",
        }

    def _process_order(self, actor, order_id):
        order = self.repository.get_entity(order_id)
        if not order:
            raise NotFoundError("entity not found: " + order_id)
        if self.rules.normalize_kind(order["kind"]) != "coordination_order":
            raise ValidationError("not a coordination order")
        if order["status"] == "cancelled":
            raise InvalidTransition("cannot process a cancelled coordination order")
        if order["status"] == "completed":
            return order
        return self._run_steps(actor, order)

    def _run_steps(self, actor, order):
        data = dict(order["data"])
        steps = []
        for raw in data.get("steps", []):
            step = dict(raw)
            if step["status"] != "done":
                try:
                    ok, message = self._execute_step(actor, step, data)
                except Exception as exc:
                    ok, message = False, str(exc)
                if ok:
                    step["status"] = "done"
                    step["executor"] = actor.user_id
                    step["executed_at"] = utcnow()
                    step["message"] = message
                else:
                    step["status"] = "pending"
                    step["message"] = message
            steps.append(step)
        data["steps"] = steps
        new_status = "completed" if all(s["status"] == "done" for s in steps) else "active"
        updated = self.repository.update_entity(order["id"], order["version"], new_status, data)
        self.audit.record(
            order["id"], actor, "process", order["status"], new_status,
            {"steps": [{"key": s["key"], "status": s["status"], "message": s["message"]} for s in steps]},
        )
        return updated

    def _execute_step(self, actor, step, order_data):
        kind = step["type"]
        if kind == "isolate":
            return self._step_isolate(actor, step)
        if kind == "fan":
            return self._step_fan(actor, step)
        if kind == "refuge":
            return self._step_refuge(actor, step)
        if kind == "task":
            return self._step_task(actor, step, order_data)
        return False, "unknown step type: " + str(kind)

    def _step_isolate(self, actor, step):
        passage = self.repository.get_entity(step["target"])
        if not passage:
            return False, "passage not found"
        if passage["status"] == "blocked":
            return True, "passage already isolated"
        if passage["status"] in ("open", "restricted"):
            self.transition(actor, passage["id"], "block", {})
            return True, "passage blocked"
        return False, "passage status %s cannot be blocked" % passage["status"]

    def _step_fan(self, actor, step):
        fan = self.repository.get_entity(step["target"])
        if not fan:
            return False, "fan not found"
        if fan["status"] == "running":
            return True, "fan already running"
        if fan["status"] == "faulty":
            return False, "fan faulty; repair required"
        if fan["status"] in ("stopped", "degraded"):
            self.transition(actor, fan["id"], "restore", {"tested_at": utcnow()})
            return True, "fan started"
        return False, "fan status %s cannot be started" % fan["status"]

    def _step_refuge(self, actor, step):
        area = step["area"]
        needed = len([
            w for w in self.repository.list_entities(kind="worker")
            if w["status"] in ("active", "missing") and w["data"].get("location_code") == area
        ])
        if needed == 0:
            return True, "no personnel in area"
        candidates = [
            r for r in self.repository.list_entities(kind="refuge")
            if r["status"] == "available"
            and r["data"].get("location_code") == area
            and float(r["data"].get("capacity", 0)) >= needed
        ]
        if not candidates:
            return False, "no available refuge with capacity for %d people" % needed
        chosen = sorted(candidates, key=lambda r: (-float(r["data"]["capacity"]), r["id"]))[0]
        self.transition(actor, chosen["id"], "occupy", {})
        step["target"] = chosen["id"]
        return True, "refuge occupied for %d people" % needed

    def _step_task(self, actor, step, order_data):
        worker = self.repository.get_entity(step["target"])
        if not worker:
            return False, "worker not found"
        if worker["status"] != "missing":
            return True, "worker no longer missing"
        dedupe = "rescue:" + worker["id"]
        existing = [
            t for t in self.repository.list_entities(kind="task")
            if t["data"].get("dedupe_key") == dedupe and t["status"] not in ("completed", "cancelled")
        ]
        if existing:
            return True, "task already dispatched"
        task = self.create(actor, "task", {
            "incident_id": order_data["incident_id"],
            "task_type": "rescue",
            "target": worker["id"],
            "dedupe_key": dedupe,
        })
        self.transition(actor, task["id"], "assign", {"team": worker["data"].get("team", "")})
        return True, "rescue task assigned"
