import copy
import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import RuleEngine

# 联动单步骤按固定顺序执行：隔离通道 -> 启动应急风机 -> 占用避险硐室 -> 失联人员派单
LINKAGE_STEPS = ("isolate_passages", "start_fans", "occupy_refuges", "dispatch_tasks")

# 联动子操作（封通道、恢复风机等）以系统身份执行，触发人记录在步骤的 executed_by 上
LINKAGE_ACTOR = Actor("linkage-engine", "admin")


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
        if kind == "linkage":
            # 同一事件和污染区域只保留一张活跃联动单，重复报警返回原单
            duplicate = self._find_active_linkage(payload["incident_id"], payload["area_code"])
            if duplicate:
                return duplicate
            payload["affected_areas"] = sorted(self._affected_areas(payload["area_code"]))
            payload["steps"] = [
                {"name": name, "status": "pending", "executed_by": None, "executed_at": None, "detail": {}}
                for name in LINKAGE_STEPS
            ]
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _find_active_linkage(self, incident_id, area_code):
        for linkage in self._lookup("linkage", "incident_id", incident_id):
            if linkage["status"] == "active" and linkage["data"].get("area_code") == area_code:
                return linkage
        return None

    def _affected_areas(self, area_code):
        """沿可用通道（未封锁）从污染区域向外搜索所有受影响区域。"""
        adjacency = {}
        for passage in self.repository.list_entities(kind="passage"):
            if passage["status"] not in ("open", "restricted"):
                continue
            frm = passage["data"].get("from_location")
            to = passage["data"].get("to_location")
            adjacency.setdefault(frm, set()).add(to)
            adjacency.setdefault(to, set()).add(frm)
        seen = {area_code}
        queue = [area_code]
        while queue:
            current = queue.pop()
            for nxt in adjacency.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if entity["kind"] == "linkage" and action == "execute":
            return self._execute_linkage(actor, entity, dict(data or {}), expected)
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

    def _apply_subaction(self, actor, entity, action, data):
        """以统一状态机和审计执行联动子操作（封通道、恢复风机等）。"""
        next_status, patch = self.rules.validate_transition(actor, entity, action, data, self._lookup)
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], entity["version"], next_status, merged)
        self.audit.record(entity["id"], actor, action, entity["status"], updated["status"], {"patch": patch})
        return updated

    def _execute_linkage(self, actor, entity, data, expected):
        self.rules.validate_transition(actor, entity, "execute", data, self._lookup)
        steps = copy.deepcopy(entity["data"].get("steps") or [])
        affected = set(entity["data"].get("affected_areas") or [])
        results = []
        for step in steps:
            if step.get("status") == "done":
                continue  # 已完成步骤跳过，重试不会重复执行副作用
            ok, detail = self._run_linkage_step(actor, entity, step, affected)
            step["detail"] = detail
            if ok:
                step["status"] = "done"
                step["executed_by"] = actor.user_id
                step["executed_at"] = utcnow()
                results.append({"step": step["name"], "status": "done"})
            else:
                # 条件不满足的步骤留在待处理，后续步骤本轮不再执行
                results.append({"step": step["name"], "status": "pending", "reason": detail.get("reason")})
                break
        final_status = "completed" if all(step.get("status") == "done" for step in steps) else "active"
        merged = dict(entity["data"])
        merged["steps"] = steps
        updated = self.repository.update_entity(entity["id"], expected, final_status, merged)
        self.audit.record(
            entity["id"], actor, "execute", entity["status"], updated["status"], {"results": results}
        )
        return updated

    def _run_linkage_step(self, actor, linkage, step, affected):
        name = step.get("name")
        if name == "isolate_passages":
            return self._step_isolate_passages(linkage, step, affected)
        if name == "start_fans":
            return self._step_start_fans(linkage, step, affected)
        if name == "occupy_refuges":
            return self._step_occupy_refuges(linkage, step, affected)
        if name == "dispatch_tasks":
            return self._step_dispatch_tasks(linkage, step, affected)
        raise ValidationError("unknown linkage step: " + str(name))

    def _step_isolate_passages(self, linkage, step, affected):
        detail = dict(step.get("detail") or {})
        blocked_ids = list(detail.get("passage_ids", []))
        for passage in self.repository.list_entities(kind="passage"):
            frm = passage["data"].get("from_location")
            to = passage["data"].get("to_location")
            if frm not in affected and to not in affected:
                continue
            if passage["status"] == "blocked":
                if passage["id"] not in blocked_ids:
                    blocked_ids.append(passage["id"])
                continue
            updated = self._apply_subaction(
                LINKAGE_ACTOR, passage, "block",
                {"reason": "linkage isolation", "linkage_id": linkage["id"]},
            )
            blocked_ids.append(updated["id"])
        detail["passage_ids"] = blocked_ids
        return True, detail

    def _step_start_fans(self, linkage, step, affected):
        detail = dict(step.get("detail") or {})
        restored = list(detail.get("restored_fan_ids", []))
        failed = []
        fans = [
            fan for fan in self.repository.list_entities(kind="ventilation")
            if fan["data"].get("area_code") in affected
        ]
        for fan in fans:
            if fan["status"] == "running":
                continue
            if fan["data"].get("out_of_service"):
                failed.append(fan["id"])  # 设备状态不满足，留待处理且不回滚已完成的隔离
                continue
            updated = self._apply_subaction(
                LINKAGE_ACTOR, fan, "restore",
                {"tested_at": utcnow(), "linkage_id": linkage["id"]},
            )
            if updated["id"] not in restored:
                restored.append(updated["id"])
        detail["fan_ids"] = [fan["id"] for fan in fans]
        detail["restored_fan_ids"] = restored
        if failed:
            detail["failed_fan_ids"] = failed
            detail["reason"] = "ventilation unavailable: " + ", ".join(failed)
            return False, detail
        detail.pop("failed_fan_ids", None)
        detail.pop("reason", None)
        return True, detail

    def _step_occupy_refuges(self, linkage, step, affected):
        detail = dict(step.get("detail") or {})
        refuge_ids = list(detail.get("refuge_ids", []))
        need = len([
            worker for worker in self.repository.list_entities(kind="worker")
            if worker["status"] in ("missing", "located")
            and worker["data"].get("location_code") in affected
        ])
        refuges = {
            refuge["id"]: refuge
            for refuge in self.repository.list_entities(kind="refuge")
            if refuge["data"].get("location_code") in affected
        }

        def current_capacity():
            # 只累计本步骤已占用且仍被占用的硐室，重试不会重复占用
            return sum(
                int(refuges[refuge_id]["data"].get("capacity", 0))
                for refuge_id in refuge_ids
                if refuge_id in refuges and refuges[refuge_id]["status"] == "occupied"
            )

        capacity = current_capacity()
        if need and capacity < need:
            for refuge in refuges.values():
                if capacity >= need:
                    break
                if refuge["status"] != "available":
                    continue
                self._apply_subaction(
                    LINKAGE_ACTOR, refuge, "occupy", {"linkage_id": linkage["id"]}
                )
                if refuge["id"] not in refuge_ids:
                    refuge_ids.append(refuge["id"])
                capacity += int(refuge["data"].get("capacity", 0))
        detail["refuge_ids"] = refuge_ids
        detail["need"] = need
        detail["capacity"] = capacity
        if capacity < need:
            detail["reason"] = "insufficient refuge capacity: need %d, have %d" % (need, capacity)
            return False, detail
        detail.pop("reason", None)
        return True, detail

    def _step_dispatch_tasks(self, linkage, step, affected):
        detail = dict(step.get("detail") or {})
        task_ids = list(detail.get("task_ids", []))
        workers = [
            worker for worker in self.repository.list_entities(kind="worker")
            if worker["status"] == "missing"
            and worker["data"].get("location_code") in affected
        ]
        existing_tasks = self.repository.list_entities(kind="task")
        for worker in workers:
            dedupe_key = "linkage:%s:worker:%s" % (linkage["id"], worker["id"])
            active = [
                task for task in existing_tasks
                if task["data"].get("dedupe_key") == dedupe_key
                and task["status"] not in ("completed", "cancelled")
            ]
            if active:
                # 已派单的人员跳过，重试不会重复派单
                if active[0]["id"] not in task_ids:
                    task_ids.append(active[0]["id"])
                continue
            task = self.create(LINKAGE_ACTOR, "task", {
                "incident_id": linkage["data"].get("incident_id"),
                "task_type": "search",
                "target": worker["id"],
                "dedupe_key": dedupe_key,
                "worker_name": worker["data"].get("name"),
                "location_code": worker["data"].get("location_code"),
            })
            self._apply_subaction(
                LINKAGE_ACTOR, task, "assign", {"team": "rescue", "linkage_id": linkage["id"]}
            )
            task_ids.append(task["id"])
            existing_tasks.append(task)
        detail["task_ids"] = task_ids
        detail["worker_ids"] = [worker["id"] for worker in workers]
        return True, detail

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
