# 5. Incident Server

The Incident Server publishes the agent's conclusions as `ietf-incident` data (draft-ietf-nmop-network-incident-yang-20), over a RESTCONF-shaped HTTP API with Server-Sent Events (SSE). It runs inside the agent process.

- During a `replay`, notifications are written to the incident store.
- `serve` exposes the store over HTTP. It can also replay a scenario into the server, so clients can watch an incident live.

## 5.1 Start the server

```bash
# Serve whatever is in the store
.venv/bin/python -m diag_agent serve

# Serve, and replay the fibre-cut scenario into the server 1 s after start-up
.venv/bin/python -m diag_agent serve --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml --no-llm \
    --store /tmp/demo.sqlite --port 8040
```

| Option | Meaning |
|---|---|
| `--host`, `--port` | Bind address (default `127.0.0.1:8040`, from `incident_server.host/port`) |
| `--store FILE` | Incident store (default `data/diag_agent/incidents.sqlite`) |
| `--scenario`, `--db`, `--incident`, ... | Every `replay` option except `--step`. The replay runs in a background thread. |
| `--start-delay S` | Seconds to wait before the replay starts, so you can connect an SSE client first (default 1) |
| `--validate` | YANG validation policy for published payloads and RPC inputs |
| `--log-level` | uvicorn log level (default `warning`) |

The interactive API docs (FastAPI) are at `http://127.0.0.1:8040/docs`.

## 5.2 Endpoints

| Method and path | Description |
|---|---|
| `GET /restconf/data/ietf-incident:incidents` | The incident list. `?mode=strict\|extended` overrides the configured payload mode; `?status=raised,updated` filters by status. |
| `GET /restconf/data/ietf-incident:incidents/incident=<name>,<type>,<qualifier>` | One incident, by its URL-encoded key |
| `POST /restconf/operations/ietf-incident:incident-acknowledge` | Acknowledge. Returns 204, or 400 with RESTCONF `errors`. |
| `POST /restconf/operations/ietf-incident:incident-diagnose` | Request a diagnosis. Returns 200 with `task-id` in extended mode, 204 in strict mode. If a task is already running, the request joins it. |
| `POST /restconf/operations/ietf-incident:incident-resolve` | Request resolution. Returns 204, or 400 `probable-cause-unresolved` if nothing was committed. |
| `GET /restconf/streams/incident/json` | SSE stream of `incident-notification` and `diagnosis-task-notification`, each wrapped in `ietf-restconf:notification`. `?replay=true` first sends every stored notification. |
| `GET /diag/v1/incidents/{no}/tree?view=decision\|trace&format=json\|mermaid\|ascii` | The diagnosis tree; the target of the `tree-uri` leaf |
| `GET /diag/v1/incidents/{no}/ledger` | The ledger: prior, terms, posterior, NOTA, entropy and history |
| `GET /diag/v1/incidents/{no}/stream` | SSE of tree deltas, ledger snapshots and notifications for one incident |
| `GET /diag/v1/incidents/{no}/notifications` | The stored notification log of one incident |
| `GET /diag/v1/health` | Liveness check |

**Payload modes.** `strict` uses only `ietf-incident`, plus the `diag-agent-alarm-types` identities that alarm events need. `extended` (the default) adds the `diag-agent-incident-ext` augmentations: the tree URI, the probability, NOTA, the 90% set, and finer cause names. The default is set by `incident_server.payload_mode`.

**Authentication.** Leaving `incident_server.tokens` empty disables authentication. Otherwise, map bearer tokens to rights:

```yaml
incident_server:
  tokens: { "s3cret-token": "read ack diagnose resolve", "viewer-token": "read" }
```

## 5.3 curl examples

```bash
B=http://127.0.0.1:8040

curl -s "$B/restconf/data/ietf-incident:incidents?mode=strict" | python -m json.tool | head -40

curl -s "$B/diag/v1/incidents/1001/tree?format=ascii"
curl -s "$B/diag/v1/incidents/1001/tree?view=trace&format=mermaid" > tree.mmd

curl -s -X POST "$B/restconf/operations/ietf-incident:incident-acknowledge" \
     -H 'Content-Type: application/yang-data+json' \
     -d '{"ietf-incident:input": {"incident-no": ["1001"], "ticket-no": "TT-1"}}' -w '%{http_code}\n'

curl -s -X POST "$B/restconf/operations/ietf-incident:incident-diagnose" \
     -H 'Content-Type: application/yang-data+json' \
     -d '{"ietf-incident:input": {"incident-no": ["9999"]}}'
# 400 {"ietf-restconf:errors": {"error": [{ ... "reason": "ietf-incident:incident-not-found" ... }]}}
```

## 5.4 Watching notifications live

Open the stream in one shell **before** the replay starts:

```bash
curl -sN http://127.0.0.1:8040/restconf/streams/incident/json
```

and start `serve --scenario ...` in another (raise `--start-delay` if you need more time). Each event looks like this:

```text
event: notification
data: {"ietf-restconf:notification": {"eventTime": "2026-11-02T09:30:32.372Z",
       "ietf-incident:incident-notification": {"incident-no": "1001", "status": "raised", ...}}}
```

A replay runs in seconds of wall time, so all seven fibre-cut notifications arrive almost at once. Their `eventTime` values carry the replay times, from 09:30:32 to 10:52:10. To follow the reasoning of one incident, use `/diag/v1/incidents/1001/stream`, which sends `tree` events (new or updated nodes) and `ledger` events.

## 5.5 Command-line inspection

These commands read the store directly, without a running server:

```bash
.venv/bin/python -m diag_agent incidents                       # one line per incident
.venv/bin/python -m diag_agent tree 1001 --tree-view trace     # stored tree
.venv/bin/python -m diag_agent notifications 1001 --full       # full payloads (configured mode)
```

All three accept `--store FILE`.

## 5.6 Validation

Every notification is validated with yangson before it is stored or sent. The YANG modules are:

- `ietf-incident`, extracted from the draft in `references/`;
- `ietf-alarms`, `ietf-network`, `ietf-yang-types`, `ietf-inet-types` and `ietf-yang-structure-ext`, bundled with pyang;
- the generated `diag-agent-alarm-types`;
- `diag-agent-incident-ext`.

A payload that fails validation raises `ValidationFailed`, so a bug cannot publish invalid data silently. The `incident_server.validate` setting (or `--validate`) takes these values:

| Value | Behaviour |
|---|---|
| `always`, `debug` | Validate every payload |
| `sample` | Validate 1% of payloads |
| `off` | Do not validate |
