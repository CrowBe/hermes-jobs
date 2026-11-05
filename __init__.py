"""hermes-jobs — Hermes cron (system of record) <-> ONE Google Calendar job board.

Automation plugin. `discover`/`status` are read-only. `sync` is a dry-run stub that refuses to
mutate without --confirm (and even then is not implemented in 0.1.0). Calendar event text is
display-only: it never grants tools, permissions, or prompts.

Credentials: declare oauth: in plugin.yaml; sign in with `hermes auth add hermes-jobs:google`;
call Calendar via credentialed_provider_request. Never read google_token.json, .env, browser
profiles, or google-workspace skill tokens (catalog rule 11).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Union

SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_NAME = "Hermes Jobs"
AUTH_HINT = "hermes auth add hermes-jobs:google"


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        import os
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _crons() -> list:
    """Read jobs.json directly — never call cron.jobs.load_jobs (it creates dirs)."""
    f = _home() / "cron" / "jobs.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    return [
        {
            "id": j.get("id"),
            "name": j.get("name"),
            "schedule": j.get("schedule_display") or j.get("schedule"),
            "enabled": j.get("enabled", True),
        }
        for j in jobs
        if isinstance(j, dict)
    ]


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


def _calendars() -> Union[List[Dict[str, Any]], Dict[str, Any], str]:
    """List calendars through Hermes credentialed_provider_request only."""
    try:
        from hermes_cli.auth_constants import AuthError
        from hermes_cli.plugin_provider_requests import (
            ProviderNotSignedIn,
            credentialed_provider_request,
        )
    except ImportError:
        return {
            "skipped": True,
            "reason": "credentialed_provider_request unavailable on this Hermes",
            "sign_in": AUTH_HINT,
        }

    try:
        resp = credentialed_provider_request(
            "google",
            "GET",
            "https://www.googleapis.com/calendar/v3/users/me/calendarList",
            timeout=20,
        )
    except ProviderNotSignedIn as exc:
        return {"signed_in": False, "error": str(exc), "sign_in": AUTH_HINT}
    except AuthError as exc:
        code = getattr(exc, "code", "auth_error")
        return {"signed_in": False, "error": f"{code}: {exc}", "sign_in": AUTH_HINT}
    except PermissionError as exc:
        return {"signed_in": False, "error": str(exc), "sign_in": AUTH_HINT}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "sign_in": AUTH_HINT}

    if getattr(resp, "status", 500) >= 300:
        body = getattr(resp, "text", "") or ""
        return {"ok": False, "status": resp.status, "body": body[:300], "sign_in": AUTH_HINT}

    try:
        items = resp.json().get("items", [])
    except Exception:
        items = []
    return [
        {
            "id": c.get("id"),
            "summary": c.get("summary"),
            "is_job_board": c.get("summary") == CALENDAR_NAME,
        }
        for c in items
        if isinstance(c, dict)
    ]


def discover() -> Dict[str, Any]:
    crons = _crons()
    oauth = _oauth_status()
    cals = _calendars()
    return {
        "dry_run": True,
        "writes": 0,
        "cron_count": len(crons),
        "crons": crons,
        "oauth": oauth,
        "calendars": cals,
        "target_calendar": CALENDAR_NAME,
        "note": "Never uses google_token.json; credentials only via Hermes oauth + hermes auth.",
    }


def status() -> Dict[str, Any]:
    d = discover()
    cals = d["calendars"]
    job_board = isinstance(cals, list) and any(c.get("is_job_board") for c in cals)
    return {
        "cron_count": d["cron_count"],
        "oauth_helpers_available": d["oauth"].get("helpers_available"),
        "job_board_found": job_board,
        "sign_in": AUTH_HINT,
        "sync": "not implemented (0.1.0 scaffold)",
    }


def sync(confirm: bool = False) -> Dict[str, Any]:
    if not confirm:
        return {"dry_run": True, "plan": "cron -> calendar diff (not yet implemented)", "writes": 0}
    return {"refused": True, "reason": "sync mutation not implemented in 0.1.0", "writes": 0}


def _cli(args) -> None:
    fn = {
        "discover": discover,
        "status": status,
        "sync": lambda: sync(getattr(args, "confirm", False)),
    }[args.jobs_cmd or "status"]
    print(json.dumps(fn(), indent=2, default=str))


def _setup(p) -> None:
    sub = p.add_subparsers(dest="jobs_cmd")
    sub.add_parser("discover", help="Read-only: crons, Hermes oauth status, calendars")
    sub.add_parser("status", help="One-line health summary")
    s = sub.add_parser("sync", help="Dry-run by default; --confirm still refused in 0.1.0")
    s.add_argument("--confirm", action="store_true")


def _tool(args: Dict[str, Any], **_: Any) -> str:
    action = (args or {}).get("action", "status")
    return json.dumps(discover() if action == "discover" else status(), default=str)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="hermes-jobs",
        help="Cron <-> Google Calendar job board (read-only; Hermes oauth)",
        setup_fn=_setup,
        handler_fn=_cli,
    )
    ctx.register_tool(
        name="hermes_jobs_discover",
        toolset="hermes-jobs",
        handler=_tool,
        description=(
            "Read-only: report Hermes crons and job-board calendar sync readiness "
            "via Hermes oauth (never google_token.json)."
        ),
        schema={
            "name": "hermes_jobs_discover",
            "description": (
                "Read-only report of Hermes crons and Google Calendar job-board readiness. "
                "Never mutates. Uses Hermes credentialed_provider_request when available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["discover", "status"]},
                },
            },
        },
    )
