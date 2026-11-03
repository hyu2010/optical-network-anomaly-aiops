"""RESTCONF-shaped HTTP API of the Incident Server (doc 06 §7)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sse_starlette.sse import EventSourceResponse

from ..render.tree import render
from .service import IncidentService, RpcError

YANG_JSON = "application/yang-data+json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _yang(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, media_type=YANG_JSON)


def _authorised(svc_tokens: dict, request: Request, right: str) -> bool:
    if not svc_tokens:
        return True
    auth = request.headers.get("authorization", "")
    tok = auth[7:] if auth.lower().startswith("bearer ") else ""
    return right in str(svc_tokens.get(tok, "")).split()


def create_app(service: IncidentService, *, tokens: dict | None = None) -> FastAPI:
    app = FastAPI(title="diag_agent Incident Server", version="1.0")
    tokens = tokens or {}

    def deny() -> JSONResponse:
        return _yang({"ietf-restconf:errors": {"error": [{"error-type": "protocol", "error-tag": "access-denied",
                                                          "error-message": "access denied"}]}}, 403)

    # ------------------------------------------------------------------ data
    @app.get("/restconf/data/ietf-incident:incidents")
    def incidents(request: Request, mode: str | None = None, status: str | None = None):
        if not _authorised(tokens, request, "read"):
            return deny()
        statuses = [s for s in (status or "").split(",") if s] or None
        return _yang(service.incidents(mode, statuses))

    @app.get("/restconf/data/ietf-incident:incidents/incident={key:path}")
    def incident(key: str, request: Request, mode: str | None = None):
        if not _authorised(tokens, request, "read"):
            return deny()
        parts = [unquote(p) for p in key.split(",")]
        while len(parts) < 3:
            parts.append("")
        out = service.incident(parts[0], parts[1], parts[2], mode)
        if out is None:
            return _yang({"ietf-restconf:errors": {"error": [{"error-type": "application", "error-tag": "invalid-value",
                                                              "error-message": "no such incident"}]}}, 404)
        return _yang(out)

    # ------------------------------------------------------------------ operations
    async def _input(request: Request, rpc: str) -> dict:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        inp = body.get("ietf-incident:input", body.get("input", {})) or {}
        if service.validator is not None and inp:
            service.validator.node(f"{rpc}-input", {"ietf-incident:input": inp})
        return inp

    def _nos(inp: dict) -> list[str]:
        v = inp.get("incident-no", [])
        return [str(x) for x in (v if isinstance(v, list) else [v])]

    @app.post("/restconf/operations/ietf-incident:incident-acknowledge")
    async def acknowledge(request: Request):
        if not _authorised(tokens, request, "ack"):
            return deny()
        inp = await _input(request, "acknowledge")
        try:
            service.acknowledge(_nos(inp), inp.get("ticket-no"), _now_iso())
        except RpcError as e:
            return _yang(e.payload, e.status)
        return Response(status_code=204)

    @app.post("/restconf/operations/ietf-incident:incident-diagnose")
    async def diagnose(request: Request):
        if not _authorised(tokens, request, "diagnose"):
            return deny()
        inp = await _input(request, "diagnose")
        try:
            ids = service.diagnose(_nos(inp), inp.get("ticket-no"), _now_iso())
        except RpcError as e:
            return _yang(e.payload, e.status)
        if service.mode == "strict":
            return Response(status_code=204)
        return _yang({"ietf-incident:output": {"task-id": ids}})

    @app.post("/restconf/operations/ietf-incident:incident-resolve")
    async def resolve(request: Request):
        if not _authorised(tokens, request, "resolve"):
            return deny()
        inp = await _input(request, "resolve")
        try:
            service.resolve(_nos(inp), inp.get("ticket-no"), _now_iso())
        except RpcError as e:
            return _yang(e.payload, e.status)
        return Response(status_code=204)

    # ------------------------------------------------------------------ streams
    def _sse(filter_fn, *, replay: list[dict] | None = None):
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def cb(ev: dict) -> None:
            if filter_fn(ev):
                loop.call_soon_threadsafe(q.put_nowait, ev)

        service.subscribe(cb)

        async def gen():
            try:
                for ev in replay or []:
                    yield ev
                while True:
                    ev = await q.get()
                    yield {"event": ev["type"], "data": json.dumps(ev.get("payload", ev), ensure_ascii=False)}
            finally:
                service.unsubscribe(cb)
        return EventSourceResponse(gen(), ping=15)

    @app.get("/restconf/streams/incident/json")
    async def stream(request: Request, replay: bool = False):
        if not _authorised(tokens, request, "read"):
            return deny()
        past = []
        if replay:
            for n in service.store.notifications():
                p = n["payload"].get(service.mode) or n["payload"].get("extended")
                from .mapper import restconf_wrap
                past.append({"event": "notification", "data": json.dumps(restconf_wrap(p, n["event_time"]))})
        return _sse(lambda ev: ev["type"] == "notification", replay=past)

    @app.get("/diag/v1/incidents/{no}/stream")
    async def incident_stream(no: str, request: Request):
        if not _authorised(tokens, request, "read"):
            return deny()
        return _sse(lambda ev: ev.get("incident_no") == no and ev["type"] in ("tree", "ledger", "notification"))

    # ------------------------------------------------------------------ reasoning
    @app.get("/diag/v1/incidents/{no}/tree")
    def tree(no: str, request: Request, view: str = "decision", format: str = "json"):
        if not _authorised(tokens, request, "read"):
            return deny()
        nodes, _, _ = service.store.reasoning(no)
        if nodes is None:
            return JSONResponse({"error": f"no tree for incident {no}"}, status_code=404)
        text = render(nodes, view, format)
        if format == "json":
            return Response(text, media_type="application/json")
        return PlainTextResponse(text)

    @app.get("/diag/v1/incidents/{no}/ledger")
    def ledger(no: str, request: Request):
        if not _authorised(tokens, request, "read"):
            return deny()
        _, led, _ = service.store.reasoning(no)
        if led is None:
            return JSONResponse({"error": f"no ledger for incident {no}"}, status_code=404)
        return JSONResponse(led)

    @app.get("/diag/v1/incidents/{no}/notifications")
    def notifications(no: str, request: Request):
        if not _authorised(tokens, request, "read"):
            return deny()
        return JSONResponse(service.store.notifications(no))

    @app.get("/diag/v1/health")
    def health():
        return {"status": "ok", "mode": service.mode, "incidents": len(service.store.views())}

    return app
