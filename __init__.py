"""hermes-jobs — Hermes cron (system of record) <-> ONE Google Calendar job board.

Automation plugin. `discover`/`status` are read-only. `sync` plans both directions and is a
dry-run unless --confirm is passed on the CLI (the agent tool can only plan). Calendar event text is
display-only: it never grants tools, permissions, or prompts.

Credentials: declare oauth: in plugin.yaml; sign in with `hermes auth add hermes-jobs:google`;
call Calendar via credentialed_provider_request. Never read google_token.json, .env, browser
profiles, or google-workspace skill tokens (catalog rule 11).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import planner

SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_HINT = "hermes auth add hermes-jobs:google"


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        import os
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _raw_jobs() -> list:
    """Read jobs.json directly -- never call cron.jobs.load_jobs (it creates dirs)."""
    f = _home() / "cron" / "jobs.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("cannot read cron store safely; sync aborted") from exc
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict) and j.get("id")]


def _job_zone(raw: dict):
    """Only public config reader + stdlib; no private cron imports or credential reads."""
    import os
    from zoneinfo import ZoneInfo
    from hermes_cli.config import load_config_readonly
    name = raw.get("timezone") or os.environ.get("HERMES_TIMEZONE") or load_config_readonly().get("timezone")
    if not name:
        target = str(Path("/etc/localtime").resolve())
        if "/zoneinfo/" in target:
            name = target.split("/zoneinfo/", 1)[1]
    if not name:
        raise ValueError("no IANA timezone available; pin the source job timezone before moving")
    return ZoneInfo(name)


def _following_start(raw: dict, start: Optional[str] = None) -> Optional[str]:
    """Read-only recurrence calculation, following installed Hermes' wall-clock/DST rules."""
    from datetime import datetime, timedelta, timezone
    sched = raw.get("schedule") or {}
    zone = _job_zone(raw)
    base = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else datetime.now(zone)
    if sched.get("kind") == "interval":
        return (base.astimezone(timezone.utc) + timedelta(minutes=sched["minutes"])).astimezone(zone).isoformat()
    if sched.get("kind") != "cron":
        return None
    from croniter import croniter
    it = croniter(sched["expr"], base.astimezone(zone).replace(tzinfo=None))
    for _ in range(2):
        wall = it.get_next(datetime)
        for fold in (0, 1):
            candidate = wall.replace(tzinfo=zone, fold=fold)
            if candidate.timestamp() > base.timestamp():
                return candidate.isoformat()
    raise ValueError("cannot resolve next slot across DST")


def _next_start(j: dict) -> Optional[str]:
    """Stored next run; paused recurring jobs keep a read-only board preview."""
    if j.get("next_run_at"):
        return j["next_run_at"]
    if (j.get("schedule") or {}).get("kind") != "cron":
        return None
    try:
        return _following_start(j)
    except Exception:
        return None


# Verified against installed tools/cronjob_tools.py:_HANDLER_FORWARDED_ARGS.
COPY_FIELDS = {"prompt", "deliver", "failure_deliver", "skills", "script", "context_from",
               "enabled_toolsets", "workdir", "no_agent", "attach_to_session", "timezone", "catch_up"}


def _copy_args(raw: dict) -> dict:
    args = {k: raw[k] for k in COPY_FIELDS if raw.get(k) is not None}
    args.setdefault("deliver", "local")
    if raw.get("skill") and not args.get("skills"):
        args["skills"] = [raw["skill"]]
    refs = args.get("context_from")
    if refs:
        args["context_from"] = [raw["id"] if r == "self" else r for r in ([refs] if isinstance(refs, str) else refs)]
    for key in ("deliver", "failure_deliver"):
        if "origin" in str(args.get(key, "")).split(","):
            origin = raw.get("origin") or {}
            target = ":".join(str(origin[k]) for k in ("platform", "chat_id", "thread_id") if origin.get(k))
            if not target or not origin.get("chat_id"):
                raise ValueError("origin delivery cannot be safely copied; set an explicit delivery target first")
            args[key] = ",".join(target if t == "origin" else t for t in args[key].split(","))
    for key in ("monitor_script", "monitor_url"):
        if raw.get(key):
            args[key] = raw[key]  # explicit legacy args accepted by _cronjob_handler
    # Dispatch deliberately drops these fields. Refuse rather than silently changing execution.
    if any(raw.get(k) for k in ("model", "provider", "base_url", "reasoning_effort", "interpreter")):
        raise ValueError("source has inference/interpreter settings cronjob_manage cannot copy; move refused")
    return args


def _crons() -> list:
    """Read-only view. No scheduler writes; prompts never flow to Calendar."""
    out = []
    for raw in _raw_jobs():
        sched = raw.get("schedule") or {}
        start = _next_start(raw)
        j = {"id": raw["id"], "name": raw.get("name"), "prompt": raw.get("prompt") or "",
             "schedule": raw.get("schedule_display") or sched.get("display") or sched,
             "schedule_kind": sched.get("kind"), "enabled": raw.get("enabled", True) and raw.get("state") != "paused",
             "terminal": raw.get("state") in {"completed", "error"}, "start": start}
        try:
            j["copy_args"] = _copy_args(raw)
            if start and sched.get("kind") in {"cron", "interval"}:
                # Mirror Hermes wall-clock/DST calculation without importing private cron internals.
                j["next_after"] = _following_start(raw, start)
        except (ValueError, KeyError, TypeError) as exc:
            j["copy_error"] = str(exc)
        out.append(j)
    return out


def _state_file() -> Path:
    try:
        from hermes_cli.plugins_state import PluginState
        d = PluginState("hermes-jobs").data_dir
    except Exception:
        d = _home() / "plugin-data" / "hermes-jobs"
    return d / "sync-state.json"


def _load_state() -> Dict[str, Any]:
    f = _state_file()
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"calendar_id": None, "jobs": {}}


def _save_state(state: Dict[str, Any]) -> None:
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(f)


def _oauth_status() -> Dict[str, Any]:
    """Report whether Hermes plugin-oauth helpers exist (no secrets, no token files)."""
    try:
        from hermes_cli.plugin_provider_requests import credentialed_provider_request  # noqa: F401
    except ImportError:
        return {
            "helpers_available": False,
            "provider": "google",
            "scope": SCOPE,
            "sign_in": AUTH_HINT,
            "note": (
                "This Hermes build lacks credentialed_provider_request / plugin oauth. "
                "Declare oauth: in plugin.yaml for catalog; run `hermes update`, then "
                f"`{AUTH_HINT}` (CrowClaws: crowsclaw001@gmail.com)."
            ),
        }
    return {
        "helpers_available": True,
        "provider": "google",
        "scope": SCOPE,
        "sign_in": AUTH_HINT,
    }


CRON_FIELDS = {"schedule", "name"}  # the only cron fields calendar edits may touch
_CTX: Dict[str, Any] = {}


def _events(calendar_id: Optional[str], offline: bool) -> List[Dict[str, Any]]:
    if offline or not calendar_id:
        return []
    from .gcal import Calendar
    return Calendar(calendar_id).list_tagged()


def discover() -> Dict[str, Any]:
    st = _load_state()
    return {
        "dry_run": True,
        "writes": 0,
        "crons": [{k: c[k] for k in ("id", "name", "schedule", "enabled", "start")} for c in _crons()],
        "oauth": _oauth_status(),
        "calendar_id": st.get("calendar_id"),
        "state_file": str(_state_file()),
        "note": "Never uses google_token.json; credentials only via Hermes oauth + hermes auth.",
    }


def status() -> Dict[str, Any]:
    st = _load_state()
    return {
        "cron_count": len(_raw_jobs()),
        "oauth_helpers_available": _oauth_status().get("helpers_available"),
        "calendar_id": st.get("calendar_id"),
        "synced_jobs": len(st.get("jobs", {})),
        "sign_in": AUTH_HINT,
        "messages": [planner.pause_message(jid) for jid, last in st.get("jobs", {}).items() if last.get("paused_by_delete")],
    }


def bind(calendar_id: str) -> Dict[str, Any]:
    """Choose the ONE calendar to sync (local state only). Rebinding forgets prior event links."""
    st = _load_state()
    if st.get("calendar_id") != calendar_id:
        st = {"calendar_id": calendar_id, "jobs": {}}
        _save_state(st)
    return {"calendar_id": calendar_id, "state_file": str(_state_file())}


def sync(confirm: bool = False, offline: bool = False, allow_additional_run: bool = False) -> Dict[str, Any]:
    st = _load_state()
    cal = st.get("calendar_id")
    if confirm and (offline or not cal):
        return {"refused": True, "reason": "--confirm needs a bound calendar and no --offline", "writes": 0}
    try:
        jobs, events = _crons(), _events(cal, offline)
    except Exception as exc:  # noqa: BLE001 -- not signed in, HTTP error, ...
        return {"error": f"{type(exc).__name__}: {exc}", "sign_in": AUTH_HINT, "writes": 0}
    ops = planner.plan(jobs, events, {} if offline else st.get("jobs", {}))
    out: Dict[str, Any] = {"dry_run": not confirm, "calendar_id": cal, "offline": offline or not cal,
                           "events_seen": len(events), "ops": ops, "writes": 0,
                           "messages": [o["reason"] for o in ops if o["action"] in {"pause_job", "notice"}],
                           "warnings": [o["warning"] for o in ops if o.get("warning")]}
    if confirm and any(o["action"] == "create_once" for o in ops) and not allow_additional_run:
        out.update(refused=True, reason="Exact occurrence suppression is unavailable. Review the duplicate-run warning, then explicitly use --confirm --allow-additional-run to create an additional run.")
        return out
    if confirm:
        out["writes"], out["errors"] = _apply(ops, cal)
        # Do not bless failed operations as a successful baseline.
        if not out["errors"]:
            _snapshot(cal)
    return out


CRON_TOOL = "cronjob_manage"  # registered name in tools/cronjob_tools.py


def _cron_dispatcher():
    """Return a caller for Hermes' own cron tool via the documented ctx.dispatch_tool seam.

    A `hermes <plugin-command>` CLI process never imports model_tools, so the built-in tool
    registry is empty there and dispatch_tool answered "Unknown tool: cronjob_manage". Populate
    it with the public tools.registry discovery entry point (the same call model_tools makes)
    before dispatching; agent/gateway processes already have it and skip discovery.
    """
    from tools.registry import discover_builtin_tools, registry
    if registry.get_entry(CRON_TOOL) is None:
        discover_builtin_tools()
    if registry.get_entry(CRON_TOOL) is None:
        raise RuntimeError(f"Hermes tool {CRON_TOOL!r} is unavailable; refusing to apply cron ops")
    ctx = _CTX["ctx"]
    return lambda args: ctx.dispatch_tool(CRON_TOOL, args)


def _apply(ops: List[Dict[str, Any]], calendar_id: str):
    from .gcal import Calendar
    needs_cron = any(o["direction"] == "calendar->cron" for o in ops)
    # Preflight before ANY write so a missing cron tool cannot leave calendar-only partial syncs.
    cron = _cron_dispatcher() if needs_cron else None
    gc, writes, errors = Calendar(calendar_id), 0, []
    st = _load_state()
    links = st.setdefault("jobs", {})
    for op in ops:
        a, jid = op["action"], op["job_id"]
        try:
            if a == "insert_event":
                ev = gc.insert(op["event"])
                links.setdefault(jid, {}).pop("paused_by_delete", None)
                links[jid].update(event_id=ev["id"], job_hash=planner.job_hash(next(j for j in _crons() if j["id"] == jid)),
                                  start=ev["start"], summary=ev["summary"])
                _save_state(st)
            elif a == "patch_event":
                gc.patch(op["event_id"], op["event"])
            elif a == "delete_event":
                gc.delete(op["event_id"])
                if jid in links:
                    links[jid]["event_id"] = None
                    _save_state(st)
            elif a == "update_job":
                upd = {k: v for k, v in op["updates"].items() if k in CRON_FIELDS}
                _check(cron({"action": "update", "job_id": jid, **upd}))
            elif a == "pause_job":
                _check(cron({"action": "pause", "job_id": jid,
                                               "reason": "hermes-jobs: calendar event deleted"}))
                links.setdefault(jid, {}).update(paused_by_delete=True, event_id=None,
                                                 pause_reason=planner.pause_message(jid))
                _save_state(st)
            elif a in {"create_once", "finish_move"}:
                if a == "create_once":
                    # Journal intent before creating: a crash with unknown result must not duplicate.
                    links.setdefault(jid, {})["move_intent"] = op["event_id"]
                    _save_state(st)
                    result = _check(cron({"action": "create", **op["create_args"]}))
                    writes += 1  # cron creation and event ownership patch are separate writes
                    child = result.get("job_id")
                    if not child:
                        raise RuntimeError("create returned no job_id; reconcile move_intent manually before retry")
                    links[child] = {"source_job_id": jid, "event_id": op["event_id"],
                                    "original_slot": op["original_slot"]}
                    links[jid].update(pending_move={"child_id": child, "event_id": op["event_id"],
                                                   "moved_start": op["moved_start"],
                                                   "original_slot": op["original_slot"], "board_start": op["board_start"]})
                    links[jid].pop("move_intent", None)
                    _save_state(st)
                pending = links[jid]["pending_move"]
                child = pending["child_id"]
                child_job = next(j for j in _crons() if j["id"] == child)
                body = planner.event_body(child_job)
                body["start"] = pending["moved_start"]
                gc.patch(pending["event_id"], body)
                links[child].update(job_hash=planner.job_hash(child_job), start=body["start"], summary=body["summary"])
                links[jid].update(original_slot=pending["original_slot"], board_start=pending["board_start"], event_id=None)
                links[jid].pop("pending_move", None)
                _save_state(st)
            elif a == "forget":
                links.pop(jid, None)
                _save_state(st)
            else:
                continue
            writes += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"op": op, "error": f"{type(exc).__name__}: {exc}"})
    return writes, errors


def _check(result) -> dict:
    data = json.loads(result) if isinstance(result, str) else result
    if not isinstance(data, dict):
        raise RuntimeError("cronjob_manage returned no structured result")
    if data.get("error") or data.get("success") is False:
        raise RuntimeError(str(data.get("error") or data)[:200])
    return data


def _snapshot(calendar_id: str) -> None:
    """Record what is now on both sides so the next run can tell who moved what."""
    jobs = {j["id"]: j for j in _crons()}
    old = _load_state().get("jobs", {})
    st = {"calendar_id": calendar_id, "jobs": {jid: dict(last) for jid, last in old.items() if jid in jobs}}
    for jid, last in st["jobs"].items():
        if last.get("original_slot") and jobs[jid].get("start") != last["original_slot"]:
            last.pop("original_slot", None)
            last.pop("board_start", None)
    for ev in _events(calendar_id, False):
        j = jobs.get(ev["hermes_job_id"])
        if j:
            st["jobs"].setdefault(j["id"], {}).update(event_id=ev["id"], job_hash=planner.job_hash(j),
                                                        start=ev["start"], summary=ev["summary"])
    _save_state(st)


def _cli(args) -> None:
    cmd = args.jobs_cmd or "status"
    if cmd == "bind":
        res = bind(args.calendar)
    elif cmd == "sync":
        res = sync(args.confirm, args.offline, args.allow_additional_run)
    else:
        res = {"discover": discover, "status": status}[cmd]()
    print(json.dumps(res, indent=2, default=str))


def _setup(p) -> None:
    sub = p.add_subparsers(dest="jobs_cmd")
    sub.add_parser("discover", help="Read-only: crons, Hermes oauth status, bound calendar")
    sub.add_parser("status", help="One-line health summary")
    b = sub.add_parser("bind", help="Choose the ONE calendar to sync (e.g. primary)")
    b.add_argument("calendar")
    s = sub.add_parser("sync", help="Plan both directions; dry-run unless --confirm")
    s.add_argument("--confirm", action="store_true", help="Apply the plan (cron + calendar writes)")
    s.add_argument("--allow-additional-run", action="store_true", help="Accept that a moved recurring occurrence ALSO runs at its original slot (requires --confirm)")
    s.add_argument("--offline", action="store_true", help="Plan against an empty calendar; no Google calls")


def _tool(args: Dict[str, Any], **_: Any) -> str:
    action = (args or {}).get("action", "status")
    fn = {"discover": discover, "plan": lambda: sync(confirm=False)}.get(action, status)
    return json.dumps(fn(), default=str)


def register(ctx) -> None:
    _CTX["ctx"] = ctx
    ctx.register_cli_command(
        name="hermes-jobs",
        help="Cron <-> Google Calendar job board (dry-run by default; Hermes oauth)",
        setup_fn=_setup,
        handler_fn=_cli,
    )
    ctx.register_tool(
        name="hermes_jobs_discover",
        toolset="hermes-jobs",
        handler=_tool,
        description=(
            "Read-only: report Hermes crons, sync readiness, or the dry-run sync plan "
            "via Hermes oauth (never google_token.json). Never writes."
        ),
        schema={
            "name": "hermes_jobs_discover",
            "description": (
                "Read-only report of Hermes crons and the Google Calendar job board, or the dry-run "
                "sync plan. Never mutates; applying a plan is a human CLI step (--confirm)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["discover", "status", "plan"]},
                },
            },
        },
    )
