"""Incident Server lifecycle, notification policy and RPCs (doc 06 §2-§6).

`IncidentService` is the concrete `Notifier` used by the decision procedure
and the backend of the RESTCONF API.  It never writes to the network:
resolution is advised and verified.
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import Any, Callable

from . import mapper
from .store import IncidentStore
from .validate import PayloadValidator

log = logging.getLogger("diag_agent.incident")


class RpcError(Exception):
    def __init__(self, payload: dict, status: int = 400):
        super().__init__(payload["ietf-restconf:errors"]["error"][0]["error-message"])
        self.payload = payload
        self.status = status


def _summary(view: dict) -> dict:
    return {"decision": view.get("decision"), "p": view.get("leader", {}).get("p") if view.get("leader") else None,
            "nota": view.get("nota"), "causes": [(c["mode"], c["location"]) for c in
                                                 ([view["leader"]] if view.get("decision") == "committed" and view.get("leader")
                                                  else view.get("set", []))],
            "ack": view.get("ack_status"), "ticket": view.get("ticket_no"), "priority": mapper.priority(view),
            "category": mapper.category(view), "status": view.get("status")}


class IncidentService:
    def __init__(self, store: IncidentStore, *, mode: str = "extended", validator: PayloadValidator | None = None,
                 base_url: str = "http://127.0.0.1:8040"):
        self.store = store
        self.mode = mode
        self.validator = validator
        self.base_url = base_url.rstrip("/")
        self._subs: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self.on_rediagnose: Callable[[str, str], None] | None = None
        self.on_resolve: Callable[[str], None] | None = None

    # ------------------------------------------------------------------ streaming
    def subscribe(self, cb: Callable[[dict], None]) -> None:
        self._subs.append(cb)

    def unsubscribe(self, cb: Callable[[dict], None]) -> None:
        if cb in self._subs:
            self._subs.remove(cb)

    def _publish(self, event: dict) -> None:
        for cb in list(self._subs):
            try:
                cb(event)
            except Exception:  # noqa: BLE001 - a broken subscriber must not stop the agent
                log.exception("subscriber failed")

    def _emit(self, no: str, kind: str, event_time: str, payloads: dict[str, dict]) -> None:
        if self.validator is not None:
            for mode, p in payloads.items():
                self.validator.node("incident-notification" if kind != "task" else "diagnosis-task-notification", p)
        self.store.log_notification(no, kind, event_time, payloads)
        self._publish({"type": "notification", "kind": kind, "incident_no": no, "event_time": event_time,
                       "payload": mapper.restconf_wrap(payloads.get(self.mode) or payloads["extended"], event_time)})

    def tree_delta(self, incident_no: str, nodes) -> None:
        self._publish({"type": "tree", "incident_no": incident_no, "nodes": list(nodes)})

    def ledger_snapshot(self, incident_no: str, snap: dict) -> None:
        self._publish({"type": "ledger", "incident_no": incident_no, **snap})

    # ------------------------------------------------------------------ lifecycle
    def tree_uri(self, no: str) -> str:
        return f"{self.base_url}/diag/v1/incidents/{no}/tree"

    def on_raise(self, view: dict) -> dict:
        with self._lock:
            view = copy.deepcopy(view)
            no = self.store.next_incident_no()
            view["incident_no"] = no
            view.setdefault("type", "ietf-incident:problem")
            view["qualifier"] = ""
            if self.store.key_in_use(view["name"], view["type"], ""):
                view["qualifier"] = view.get("onset_iso", [view["times"]["occur"]])[0]
            view["status"] = "raised"
            view["ack_status"] = "unacknowledged"
            view["decision"] = "in-progress"
            view["tree_uri"] = self.tree_uri(no)
            view["times"]["last_updated"] = view["times"]["raise"]
            task = {"task_id": f"dx-{no}-1", "incident_no": no, "start_time": view["times"]["raise"],
                    "state": "diagnosing", "ticket_no": view.get("ticket_no")}
            view["tasks"] = [task]
            view["_last"] = _summary(view)
            self.store.save_view(view, view.get("thread_id", ""))
            self.store.save_task(task)
            self._emit(no, "raised", view["times"]["raise"], self._payloads(view))
            return view

    def _payloads(self, view: dict) -> dict[str, dict]:
        return {m: mapper.notification(view, m) for m in ("strict", "extended")}

    def on_update(self, view: dict, *, force: bool = False, event_time: str | None = None) -> bool:
        """Send `updated` if the notification policy (doc 06 §4) says so."""
        with self._lock:
            cur = self.store.view(view["incident_no"]) or {}
            merged = copy.deepcopy(view)
            for k in ("ack_status", "ticket_no", "tasks", "resolve_requested", "status", "qualifier", "tree_uri"):
                if k in cur and k not in ("status",):
                    merged[k] = cur[k]
            if cur.get("status") == "cleared":
                return False
            merged["status"] = "updated"
            merged["times"] = dict(cur.get("times", {}), **{k: v for k, v in view["times"].items() if v})
            last, now = cur.get("_last") or {}, _summary(merged)
            changed = force or any(last.get(k) != now.get(k) for k in
                                   ("decision", "causes", "ack", "ticket", "priority", "category"))
            if not changed and last.get("p") is not None and now.get("p") is not None:
                changed = abs(now["p"] - last["p"]) >= 0.01 or abs((now["nota"] or 0) - (last["nota"] or 0)) >= 0.01
            if not changed:
                merged["status"] = cur.get("status", "raised")
                merged["_last"] = last
                self.store.save_view(merged)
                return False
            t = event_time or view["times"].get("last_updated") or view["times"]["raise"]
            merged["times"]["last_updated"] = t
            merged["_last"] = now
            self.store.save_view(merged)
            self._emit(merged["incident_no"], "updated", t, self._payloads(merged))
            return True

    def on_task(self, incident_no: str, task: dict) -> None:
        with self._lock:
            view = self.store.view(incident_no)
            if view is None:
                return
            tasks = view.get("tasks", [])
            old = next((t for t in tasks if t["task_id"] == task["task_id"]), None)
            if old is not None:
                old_state = old.get("state")
                old.update(task)
                task = old
            else:
                old_state = None
                tasks.append(task)
            view["tasks"] = tasks
            self.store.save_view(view)
            self.store.save_task(task)
            if task["state"] in ("diagnosed", "finished") and old_state != task["state"]:
                t = task.get("end_time") or view["times"].get("last_updated")
                p = mapper.task_notification(task)
                self._emit(incident_no, "task", t, {"extended": p})

    def on_clear(self, incident_no: str, t_iso: str, detail: str | None = None) -> None:
        with self._lock:
            view = self.store.view(incident_no)
            if view is None or view.get("status") == "cleared":
                return
            view["status"] = "cleared"
            view["times"]["clear"] = t_iso
            view["times"]["last_updated"] = t_iso
            if detail:
                view["detail"] = detail
            view["_last"] = _summary(view)
            self.store.save_view(view)
            self._emit(incident_no, "cleared", t_iso, self._payloads(view))

    # ------------------------------------------------------------------ RPCs
    def _require(self, rpc: str, no: str) -> dict:
        view = self.store.view(str(no))
        if view is None:
            raise RpcError(mapper.rpc_error(rpc, no, "incident-not-found", f"Incident {no} does not exist.",
                                            f"No incident with incident-no {no} is known to this Incident Server."))
        return view

    def acknowledge(self, nos: list[str], ticket_no: str | None, t_iso: str) -> None:
        if not nos:
            raise RpcError(_invalid("incident-acknowledge", "incident-no is required"))
        for no in nos:
            view = self._require("incident-acknowledge", no)
            if view.get("ack_status") == "acknowledged":
                raise RpcError(mapper.rpc_error("incident-acknowledge", no, "repeated-acknowledge",
                                                f"Incident {no} is already acknowledged.",
                                                f"Incident {no} was acknowledged at {view['times'].get('ack')}."))
            with self._lock:
                view["ack_status"] = "acknowledged"
                view["times"]["ack"] = t_iso
                if ticket_no:
                    view["ticket_no"] = ticket_no
                self.store.save_view(view)
            self.on_update(dict(view, times=dict(view["times"], last_updated=t_iso)), force=True, event_time=t_iso)

    def diagnose(self, nos: list[str], ticket_no: str | None, t_iso: str) -> list[str]:
        if not nos:
            raise RpcError(_invalid("incident-diagnose", "incident-no is required"))
        out = []
        for no in nos:
            view = self._require("incident-diagnose", no)
            tasks = view.get("tasks", [])
            running = next((t for t in tasks if t["state"] in ("created", "diagnosing")), None)
            with self._lock:
                if ticket_no:
                    view["ticket_no"] = ticket_no
                if running is not None:
                    if ticket_no:
                        running["ticket_no"] = ticket_no
                    self.store.save_view(view)
                    self.store.save_task(running)
                    out.append(running["task_id"])
                    if ticket_no:
                        self.on_update(dict(view, times=dict(view["times"], last_updated=t_iso)), event_time=t_iso)
                    continue
                last = tasks[-1] if tasks else None
                if last and last.get("result") == "failed" and last.get("exhausted"):
                    raise RpcError(mapper.rpc_error("incident-diagnose", no, "probable-cause-unlocated",
                                                    f"Incident {no}: the probable cause cannot be located.",
                                                    "The previous diagnosis exhausted its return passes and check "
                                                    "budget and nothing new is observable."))
                task = {"task_id": f"dx-{no}-{len(tasks) + 1}", "incident_no": no, "start_time": t_iso,
                        "state": "diagnosing", "ticket_no": ticket_no or view.get("ticket_no")}
                tasks.append(task)
                view["tasks"] = tasks
                self.store.save_view(view)
                self.store.save_task(task)
            out.append(task["task_id"])
            if self.on_rediagnose:
                self.on_rediagnose(no, task["task_id"])
        return out

    def resolve(self, nos: list[str], ticket_no: str | None, t_iso: str) -> None:
        if not nos:
            raise RpcError(_invalid("incident-resolve", "incident-no is required"))
        for no in nos:
            view = self._require("incident-resolve", no)
            if view.get("decision") != "committed":
                desc = "Diagnosis ended without commitment"
                if view.get("set"):
                    desc += " (90% set: " + ", ".join(f"{m['mode']}@{m['location']} {m['p']:.2f}" for m in view["set"]) + \
                            f"; none-of-these {view.get('nota', 0):.2f})"
                raise RpcError(mapper.rpc_error("incident-resolve", no, "probable-cause-unresolved",
                                                f"Incident {no} cannot be resolved: no probable cause has been committed.",
                                                desc + ".", path=False))
            with self._lock:
                view["resolve_requested"] = t_iso
                if ticket_no:
                    view["ticket_no"] = ticket_no
                self.store.save_view(view)
            if self.on_resolve:
                self.on_resolve(no)

    # ------------------------------------------------------------------ queries
    def incidents(self, mode: str | None = None, statuses: list[str] | None = None) -> dict:
        views = self.store.views(statuses)
        tree = mapper.incidents_tree(views, mode or self.mode)
        if self.validator is not None and views:
            self.validator.data_tree(tree)
        return tree

    def incident(self, name: str, type_: str, qualifier: str, mode: str | None = None) -> dict | None:
        for v in self.store.views():
            if v["name"] == name and v.get("type") == type_ and v.get("qualifier", "") == qualifier:
                return {"ietf-incident:incident": [mapper.data_entry(v, mode or self.mode)]}
        return None


def _invalid(rpc: str, msg: str) -> dict:
    return {"ietf-restconf:errors": {"error": [{"error-type": "protocol", "error-tag": "missing-element",
                                                "error-path": f"/ietf-incident:{rpc}/input/incident-no",
                                                "error-message": msg}]}}


def view_time(view: dict, key: str) -> Any:
    return view.get("times", {}).get(key)
