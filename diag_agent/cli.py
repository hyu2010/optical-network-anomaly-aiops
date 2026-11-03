"""diag-agent command line (doc 05 §4, doc 07 §2)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path

from .config.settings import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "diag_agent" / "fixtures"


# ====================================================================== helpers
def _settings(args):
    from .app import settings_from
    return settings_from(getattr(args, "config", None))


def _opts(args, settings):
    from .app import RunOptions, load_scenario
    sc = load_scenario(args.scenario) if getattr(args, "scenario", None) else {}
    db = args.db or sc.get("incident_db") or str(settings.path("substrate.incidents_db"))
    inc = args.incident if args.incident is not None else sc.get("raw_incident_id")
    if inc is None:
        sys.exit("error: give --scenario or --incident")
    return RunOptions(db=db, incident=int(inc), scenario=sc, view=args.view or sc.get("view", "raw"),
                      ppe_access=args.ppe, llm_profile=args.llm_profile, no_llm=args.no_llm,
                      cache_mode=args.cache_mode, cache_path=args.cache_path, episodic=args.episodic, cold=args.cold,
                      incidents_store=args.store, validate=args.validate, step=getattr(args, "step", False),
                      checkpoints=args.checkpoints, learn=not args.no_learn, field_seed=args.field_seed)


def _summary(state: dict) -> str:
    led = state.get("ledger") or {}
    info = state.get("decision_info") or {}
    hyps = state.get("hypotheses") or {}
    b = state.get("budget") or {}
    lines = [f"incident {state.get('incident_no')}  decision: {state.get('decision')}  "
             f"clock +{state.get('clock', 0):.1f} s  cycles {state.get('cycle')}"]
    c = info.get("committed")
    if c:
        fit = c.get("fit") or {}
        pos = f", {fit['position_km']} ± {fit['tol_km']} km from {fit['from_ne']}" if fit.get("position_km") is not None else ""
        lines.append(f"committed: {c['mode']} @ {c['location']}{pos}  P = {c['p']:.5f}  NOTA = {c['nota']:.5f}")
    else:
        acc = 0.0
        for h in led.get("ranked", []):
            lines.append(f"  {h} {hyps[h]['mode']} @ {hyps[h]['location']}  P = {led['post'][h]:.4f}")
            acc += led["post"][h]
            if acc >= 0.9:
                break
        lines.append(f"  none-of-these = {led.get('nota', 0):.4f}")
    lines.append(f"checks: {', '.join(b.get('checks_done', [])) or 'none'}  effort {b.get('effort_h', 0) * 60:.1f} min  "
                 f"LLM calls {b.get('llm_calls', 0)}  tokens {b.get('tokens', 0)}")
    inc = state.get("incident") or {}
    if inc.get("detail"):
        lines.append(f"detail: {inc['detail']}")
    cl = state.get("closure") or {}
    if "cleared" in cl:
        lines.append(f"closure: {'cleared at +%.0f s' % cl['cleared_at'] if cl.get('cleared') else 'not cleared'}  "
                     f"verification: {cl.get('verification', '-')}")
    return "\n".join(lines)


def _write_outputs(out_dir: str, state: dict, agent, score: dict | None = None) -> None:
    from .render.tree import render
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    tree = state.get("tree", [])
    (p / "tree.json").write_text(render(tree, "trace", "json"))
    (p / "tree-decision.txt").write_text(render(tree, "decision", "ascii") + "\n")
    (p / "tree-trace.txt").write_text(render(tree, "trace", "ascii") + "\n")
    (p / "tree-decision.mmd").write_text(render(tree, "decision", "mermaid"))
    led = dict(state.get("ledger") or {})
    led.pop("evidence", None)
    (p / "ledger.json").write_text(json.dumps(led, indent=1, sort_keys=True, default=str) + "\n")
    no = state.get("incident_no")
    notes = agent.incidents.store.notifications(no) if no else []
    (p / "notifications.json").write_text(json.dumps(notes, indent=1, ensure_ascii=False) + "\n")
    if score is not None:
        (p / "score.json").write_text(json.dumps(score, indent=1) + "\n")


def _node_printer(args):
    from .render.tree import render

    def w(ev: dict) -> None:
        if ev.get("type") == "tree.node":
            n = ev["node"]
            print(f"  + {n['id']} [{n['kind']}] {n['label'][:150]}", flush=True)
    return w if args.watch or getattr(args, "step", False) else None


# ====================================================================== commands
def cmd_kb_build(args) -> int:
    from .app import build_semantic
    s = _settings(args)
    sem = build_semantic(s, force=args.force)
    st = sem.stats()
    print(f"semantic memory: {s.path('memory.semantic.path')}")
    for k, v in st.items():
        print(f"  {k}: {v}")
    return 0


def cmd_kb_conformance(args) -> int:
    from .app import semantic_cached
    from .harness import conformance
    sem = semantic_cached(_settings(args))
    dbs = args.db or [str(FIXTURES / "fiber_cut_oms3.db"), str(FIXTURES / "variants.db")]
    rows = conformance.run(sem, dbs)
    n_bad = 0
    for r in rows:
        ok = r.get("ok", False)
        n_bad += 0 if ok else 1
        print(f"{'OK  ' if ok else 'DIFF'} {Path(r['db']).name}#{r['incident']} {r['fault_type']} -> "
              f"{r.get('mode')} @ {r.get('location')}")
        for f in r["findings"]:
            print(f"       {f['kind']}: {f.get('strength', '')} {f.get('condition', '')} @ {f.get('resource', '')} "
                  f"{f.get('detail', '')}".rstrip())
    print(f"{len(rows) - n_bad}/{len(rows)} incidents conform")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1) + "\n")
    return 0


def cmd_replay(args) -> int:
    from .app import Agent
    from .render.tree import render
    s = _settings(args)
    opts = _opts(args, s)
    agent = Agent(s, opts, writer=_node_printer(args))
    print(f"replay {Path(opts.db).name} incident {opts.incident} ({opts.view} view), LLM profile "
          f"{agent.settings.llm_profile_name}, thread {agent.thread_id}", flush=True)
    if opts.step:
        state = _step_loop(agent)
    else:
        state = agent.run()
    print()
    print(render(state.get("tree", []), args.tree_view, args.format))
    print()
    print(_summary(state))
    score = None
    if args.score:
        from .harness.scorer import score as do_score
        score = do_score(agent.sem, state, opts.db, opts.incident)
        score.pop("label_reads", None)
        print("score:", json.dumps({k: score[k] for k in ("correct", "committed", "truth_in_90_set", "p_truth")
                                    + (("position_error_km",) if "position_error_km" in score else ())}))
    if args.out:
        _write_outputs(args.out, state, agent, score)
        print(f"artefacts written to {args.out}/")
    agent.close()
    return 0


def _step_loop(agent) -> dict:
    print("step-through: Enter = next pause, c = run to the end, q = quit", flush=True)
    run_on = False
    for node, upd in agent.stream_steps():
        if node == "__pause__":
            if run_on:
                continue
            try:
                ans = input(f"-- paused before {', '.join(upd['next'])} > ").strip().lower()
            except EOFError:
                ans = "c"
            if ans == "q":
                break
            if ans == "c":
                run_on = True
        elif node != "__interrupt__":
            keys = ", ".join(sorted(k for k in (upd or {}).keys()))
            print(f"[{node}] wrote {keys}", flush=True)
    return agent.final_state()


def cmd_score(args) -> int:
    from .app import Agent, RunOptions
    from .environment.substrate.simulator_db import list_incidents
    from .harness.scorer import score as do_score
    s = _settings(args)
    db = args.db or str(FIXTURES / "variants.db")
    ids = [int(x) for x in args.incidents.split(",")] if args.incidents else \
        [int(r["id"]) for r in list_incidents(db, suppressed=False)]
    rows = []
    for i in ids:
        opts = RunOptions(db=db, incident=i, no_llm=args.no_llm, llm_profile=args.llm_profile, cold=not args.warm,
                          episodic=args.episodic, incidents_store=args.store or "/tmp/diag_agent_score.sqlite",
                          validate=args.validate or "off", learn=False, cache_mode=args.cache_mode,
                          cache_path=args.cache_path)
        agent = Agent(s, opts)
        st = agent.run()
        sc = do_score(agent.sem, st, db, i)
        agent.close()
        rows.append(sc)
        mark = "✔" if sc["correct"] else ("~" if sc["truth_in_90_set"] else "✘")
        print(f"{mark} #{i:<3} {sc['truth']['fault_type']:<22} -> {st['decision']:<9} {sc['leader']['mode']} @ "
              f"{sc['leader']['location']} P {sc['leader']['p']:.3f}  checks {len(sc['checks'])}", flush=True)
    n = len(rows)
    print(f"\ncorrect commits {sum(r['correct'] for r in rows)}/{n}, wrong commits "
          f"{sum(r['committed'] and not r['correct'] for r in rows)}/{n}, truth in the 90% set "
          f"{sum(r['truth_in_90_set'] or r['correct'] for r in rows)}/{n}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1) + "\n")
    return 0


def _service(args):
    from .app import build_incident_service
    return build_incident_service(_settings(args), args.store, getattr(args, "validate", None))


def cmd_incidents(args) -> int:
    svc = _service(args)
    views = svc.store.views()
    if not views:
        print("no incidents in", svc.store.path if hasattr(svc.store, "path") else "the store")
    for v in views:
        lead = v.get("leader") or {}
        print(f"{v['incident_no']}  {v.get('status', ''):<8} {v.get('decision', ''):<24} raw {v.get('raw_incident_id')}  "
              f"{v['name']}  ->  {lead.get('mode', '-')} @ {lead.get('location', '-')} P {lead.get('p', 0):.3f}")
    return 0


def cmd_tree(args) -> int:
    from .render.tree import render
    svc = _service(args)
    no = args.incident_no or (svc.store.views()[-1]["incident_no"] if svc.store.views() else None)
    nodes, _, _ = svc.store.reasoning(no) if no else (None, None, "")
    if not nodes:
        print(f"no tree for incident {no}")
        return 1
    print(render(nodes, args.tree_view, args.format))
    return 0


def cmd_notifications(args) -> int:
    svc = _service(args)
    for n in svc.store.notifications(args.incident_no):
        p = n["payload"].get(args.mode or svc.mode) or n["payload"].get("extended")
        if args.full:
            print(json.dumps(p, indent=1, ensure_ascii=False))
        else:
            body = next(iter(p.values()))
            print(f"#{n['seq']} {n['event_time']} {n['incident_no']} {n['kind']:<8} "
                  f"{body.get('status', body.get('task-state', ''))}  {str(body.get('detail', ''))[:110]}")
    return 0


def cmd_episodes(args) -> int:
    from .episodic_memory.store import EpisodeStore
    s = _settings(args)
    path = args.path or str(s.path("memory.episodic.path"))
    if args.action == "list":
        st = EpisodeStore(path)
        for e in st.all():
            print(f"{e['id']}  {e['verification']:<18} w {e['weight']:.1f}  {e['summary']}  ({e['source']})")
        print(f"{st.count()} episodes in {path}")
        return 0
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from make_episodes import build
    n = build(s, path, args.db or str(FIXTURES / "history.db"))
    print(f"wrote {n} episodes to {path}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from .app import Agent
    from .incident_server.api import create_app
    s = _settings(args)
    svc = _service(args)
    app = create_app(svc, tokens=s.get("incident_server.tokens") or {})
    host = args.host or s.get("incident_server.host", "127.0.0.1")
    port = args.port or int(s.get("incident_server.port", 8040))
    if args.scenario or args.incident is not None:
        opts = _opts(args, s)

        def run() -> None:
            import time
            time.sleep(args.start_delay)
            agent = Agent(s, opts, incidents=svc)
            st = agent.run()
            print("\n" + _summary(st), flush=True)
            agent.close()

        threading.Thread(target=run, daemon=True).start()
    print(f"Incident Server on http://{host}:{port}  (RESTCONF under /restconf, trees under /diag/v1)", flush=True)
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


def cmd_llm_check(args) -> int:
    from .app import build_llm
    s = _settings(args)
    if args.llm_profile:
        s = s.with_overrides(**{"llm.profile": args.llm_profile})
    prof = s.profile_for("proposer")
    print("profile:", s.llm_profile_name, json.dumps(prof.redacted()))
    llm = build_llm(s)
    if not llm.available("proposer"):
        print("LLM not configured")
        return 1
    llm.cache = None
    r = llm.chat("orchestrator", [{"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}], json_mode=True)
    print("reply:", r["content"][:200].strip(), f"({r['latency_s']:.1f} s, {r['usage']})")
    return 0


# ====================================================================== parser
def _run_flags(p: argparse.ArgumentParser, *, step: bool = True) -> None:
    p.add_argument("--scenario", help="scenario YAML (tests/diag_agent/scenarios/*.yaml)")
    p.add_argument("--db", help="frozen simulator incidents database")
    p.add_argument("--incident", type=int, help="raw incident id in --db")
    p.add_argument("--view", choices=["raw", "suppressed"], help="alarm view (default raw)")
    p.add_argument("--ppe", choices=["on_demand", "streamed"], help="PPE access mode")
    p.add_argument("--llm-profile", help="env | deepseek | local_vllm | ollama | none")
    p.add_argument("--no-llm", action="store_true", help="run without any LLM (deterministic lines only)")
    p.add_argument("--cache-mode", choices=["read_write", "read_only", "off"], help="LLM cassette mode")
    p.add_argument("--cache-path", help="LLM cassette file")
    p.add_argument("--episodic", help="episodic memory SQLite file")
    p.add_argument("--cold", action="store_true", help="empty episodic memory")
    p.add_argument("--store", help="incident store SQLite file")
    p.add_argument("--validate", choices=["always", "debug", "sample", "off"], help="YANG payload validation")
    p.add_argument("--checkpoints", help="LangGraph SQLite checkpoint file")
    p.add_argument("--no-learn", action="store_true", help="do not write the episode / incident graph")
    p.add_argument("--field-seed", type=int, default=0, help="seed of the simulated field technician")
    if step:
        p.add_argument("--step", action="store_true", help="interactive step-through")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="diag-agent", description="CoALA diagnosis agent for the optical test bed")
    ap.add_argument("--config", help="config YAML (default config/diag_agent.yaml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    kb = sub.add_parser("kb", help="semantic memory").add_subparsers(dest="kb_cmd", required=True)
    p = kb.add_parser("build", help="build data/diag_agent/semantic.trig")
    p.add_argument("--force", action="store_true", help="rebuild even if the file exists")
    p.set_defaults(fn=cmd_kb_build)
    p = kb.add_parser("conformance", help="check the KB against labelled fixtures")
    p.add_argument("--db", action="append", help="fixture database (repeatable)")
    p.add_argument("--json", help="write the findings to this file")
    p.set_defaults(fn=cmd_kb_conformance)

    p = sub.add_parser("replay", help="diagnose one frozen incident")
    _run_flags(p)
    p.add_argument("--tree-view", choices=["decision", "trace"], default="decision")
    p.add_argument("--format", choices=["ascii", "mermaid", "json"], default="ascii")
    p.add_argument("--watch", action="store_true", help="print tree nodes as they are created")
    p.add_argument("--score", action="store_true", help="score against the fixture labels")
    p.add_argument("--out", help="write tree, ledger and notifications to this directory")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("score", help="replay and score many labelled incidents")
    p.add_argument("--db", help="fixture database (default variants.db)")
    p.add_argument("--incidents", help="comma-separated raw ids (default all raw incidents)")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--llm-profile")
    p.add_argument("--cache-mode", choices=["read_write", "read_only", "off"])
    p.add_argument("--cache-path")
    p.add_argument("--warm", action="store_true", help="use episodic memory (default cold)")
    p.add_argument("--episodic")
    p.add_argument("--store")
    p.add_argument("--validate", choices=["always", "debug", "sample", "off"])
    p.add_argument("--json", help="write the per-incident scores here")
    p.set_defaults(fn=cmd_score)

    p = sub.add_parser("serve", help="run the Incident Server (optionally replaying a scenario into it)")
    _run_flags(p, step=False)
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--start-delay", type=float, default=1.0, help="seconds before the replay starts")
    p.add_argument("--log-level", default="warning")
    p.set_defaults(fn=cmd_serve)

    for name, fn, hlp in (("incidents", cmd_incidents, "list incidents in the store"),
                          ("tree", cmd_tree, "print a stored diagnosis tree"),
                          ("notifications", cmd_notifications, "print the notification log")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--store")
        if name != "incidents":
            p.add_argument("incident_no", nargs="?")
        if name == "tree":
            p.add_argument("--tree-view", choices=["decision", "trace"], default="decision")
            p.add_argument("--format", choices=["ascii", "mermaid", "json"], default="ascii")
        if name == "notifications":
            p.add_argument("--mode", choices=["strict", "extended"])
            p.add_argument("--full", action="store_true", help="print full payloads")
        p.set_defaults(fn=fn, validate="off")

    p = sub.add_parser("episodes", help="episodic memory")
    p.add_argument("action", choices=["list", "build"])
    p.add_argument("--path", help="episode store (default memory.episodic.path)")
    p.add_argument("--db", help="labelled fixture to learn from (build)")
    p.set_defaults(fn=cmd_episodes)

    p = sub.add_parser("llm-check", help="send one tiny request to the configured LLM (key never printed)")
    p.add_argument("--llm-profile")
    p.set_defaults(fn=cmd_llm_check)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if os.environ.get("FMA_NO_LLM") and hasattr(args, "no_llm"):
        args.no_llm = True
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
