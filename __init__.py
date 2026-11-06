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
    except Exception:
        return []
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict) and j.get("id")]


def _next_start(j: dict) -> Optional[str]:
    """Stored next_run_at, else the next slot of the schedule (paused jobs keep a slot on the board)."""
    if j.get("next_run_at"):
        return j["next_run_at"]
    sched = j.get("schedule") or {}
    if sched.get("kind") != "cron":
        return None
    try:
        from croniter import croniter
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(j["timezone"])) if j.get("timezone") else datetime.now().astimezone()
        return croniter(sched["expr"], now).get_next(datetime).isoformat()
    except Exception:
        return None


def _crons() -> list:
    """Read-only job view for the planner. Prompt stays local; only its digest reaches the calendar."""
    return [
        {
            "id": j["id"],
            "name": j.get("name"),
            "prompt": j.get("prompt") or "",
            "schedule": j.get("schedule_display") or (j.get("schedule") or {}).get("display") or j.get("schedule"),
            "enabled": j.get("enabled", True) and j.get("state") != "paused",
            "start": _next_start(j),
        }
        for j in _raw_jobs()
    ]


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
    }


def bind(calendar_id: str) -> Dict[str, Any]:
    """Choose the ONE calendar to sync (local state only). Rebinding forgets prior event links."""
    st = _load_state()
    if st.get("calendar_id") != calendar_id:
        st = {"calendar_id": calendar_id, "jobs": {}}
        _save_state(st)
    return {"calendar_id": calendar_id, "state_file": str(_state_file())}


def sync(confirm: bool = False, offline: bool = False) -> Dict[str, Any]:
    st = _load_state()
    cal = st.get("calendar_id")
    if confirm and (offline or not cal):
        return {"refused": True, "reason": "--confirm needs a bound calendar and no --offline", "writes": 0}
    try:
        jobs, events = _crons(), _events(cal, offline)
    except Exception as exc:  # noqa: BLE001 -- not signed in, HTTP error, ...
        return {"error": f"{type(exc).__name__}: {exc}", "sign_in": AUTH_HINT, "writes": 0}
    ops = planner.plan(jobs, events, st.get("jobs", {}))
    out: Dict[str, Any] = {"dry_run": not confirm, "calendar_id": cal, "offline": offline or not cal,
                           "events_seen": len(events), "ops": ops, "writes": 0}
    if confirm:
        out["writes"], out["errors"] = _apply(ops, cal)
        _snapshot(cal)
    return out


def _apply(ops: List[Dict[str, Any]], calendar_id: str):
    from .gcal import Calendar
    cron = _CTX["ctx"].dispatch_tool  # public seam: Hermes' own cronjob_manage tool
    gc, writes, errors = Calendar(calendar_id), 0, []
    for op in ops:
        a, jid = op["action"], op["job_id"]
        try:
            if a == "insert_event":
                gc.insert(op["event"])
            elif a == "patch_event":
                gc.patch(op["event_id"], op["event"])
            elif a == "delete_event":
                gc.delete(op["event_id"])
            elif a == "update_job":
                upd = {k: v for k, v in op["updates"].items() if k in CRON_FIELDS}
                _check(cron("cronjob_manage", {"action": "update", "job_id": jid, **upd}))
            elif a == "pause_job":
                _check(cron("cronjob_manage", {"action": "pause", "job_id": jid,
                                               "reason": "hermes-jobs: calendar event deleted"}))
            else:
                continue
            writes += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"op": op, "error": f"{type(exc).__name__}: {exc}"})
    return writes, errors


def _check(result: str) -> None:
    try:
        data = json.loads(result)
    except Exception:
        return
    if isinstance(data, dict) and (data.get("error") or data.get("success") is False):
        raise RuntimeError(str(data.get("error") or data)[:200])


def _snapshot(calendar_id: str) -> None:
    """Record what is now on both sides so the next run can tell who moved what."""
    jobs = {j["id"]: j for j in _crons()}
    st = {"calendar_id": calendar_id, "jobs": {}}
    for ev in _events(calendar_id, False):
        j = jobs.get(ev["hermes_job_id"])
        if j:
            st["jobs"][j["id"]] = {"event_id": ev["id"], "job_hash": planner.job_hash(j),
                                   "start": ev["start"], "summary": ev["summary"]}
    _save_state(st)


def _cli(args) -> None:
    cmd = args.jobs_cmd or "status"
    if cmd == "bind":
        res = bind(args.calendar)
    elif cmd == "sync":
        res = sync(args.confirm, args.offline)
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
