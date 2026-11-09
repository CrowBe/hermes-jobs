"""EXPERIMENTAL true skip of ONE occurrence of a recurring cron job (opt-in only).

Installed Hermes has no skip-occurrence API (see README). This composes only public
cronjob_manage actions:

  arm (at --confirm, before the slot S):  pause(reason=marker) -> grace_minutes=0
                                          (+catch_up=false if it was explicitly true)
                                          -> two one-shot no_agent wake-up jobs
  S + RESUME_AFTER:                        resume. Hermes keeps the elapsed S due; because it is
                                          later than the (120 s floored) grace and catch-up is
                                          off, the next due scan SKIPS it and books one
                                          "missed x1" row in the execution ledger.
  S + RESTORE_AFTER (and every sync):      once next_run_at is past S, restore grace/catch_up.

Every step re-verifies the job (fingerprint unchanged, still paused BY US, slot unchanged) and
otherwise aborts with a warning. decide() is pure (dry-run shows it); execute() performs it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import planner

SCRIPT_NAME = "hermes-jobs-skip-tick.py"
WAKE_PREFIX = "hermes-jobs skip-tick "
RESUME_AFTER = timedelta(minutes=10)    # > Hermes' 120 s grace floor with grace_minutes=0
RESTORE_AFTER = timedelta(minutes=25)   # ticker scans ~every 60 s; leaves room for a slow tick
MIN_LEAD = timedelta(minutes=5)          # refuse to arm a slot this close (the ticker may win)
STALE_AFTER = timedelta(hours=2)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def marker(key: str, slot: str, orig_grace: Any, orig_catch_up: Any) -> str:
    """paused_reason carrying everything needed to finish/undo if sync state is lost."""
    return planner.SKIP_MARKER + json.dumps({"k": key, "slot": planner.utc_instant(slot),
                                             "grace": orig_grace, "catch_up": orig_catch_up},
                                            separators=(",", ":"))


def parse_marker(reason: Optional[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(reason, str) or not reason.startswith(planner.SKIP_MARKER):
        return None
    try:
        data = json.loads(reason[len(planner.SKIP_MARKER):])
        return data if isinstance(data, dict) and data.get("k") else None
    except ValueError:
        return None


def eligibility(raw: Dict[str, Any], slot: str, now: datetime) -> Optional[str]:
    """None when the original slot can be armed for an experimental skip, else why not."""
    if (raw.get("schedule") or {}).get("kind") != "cron":
        return "experimental skip supports cron-expression schedules only (grace_minutes is cron-only)"
    if not raw.get("enabled", True) or raw.get("state") == "paused":
        return "source job is paused; nothing to skip"
    if not raw.get("next_run_at") or planner.instant(raw["next_run_at"]) != planner.instant(slot):
        return "the original slot is not the job's next run (experimental skip handles only the next occurrence); re-run the dry-run"
    if _dt(slot) - now < MIN_LEAD:
        return f"original slot is less than {int(MIN_LEAD.total_seconds() // 60)} min away; too late to arm safely"
    return None


def steps(slot: str) -> List[Dict[str, str]]:
    s = _dt(slot)
    return [
        {"when": "at --confirm (before slot)", "do": "pause source (reason=skip marker); grace_minutes=0; "
         "catch_up=false only if it was explicitly true; create 2 one-shot no_agent wake-up jobs"},
        {"when": (s + RESUME_AFTER).isoformat(), "do": "wake-up 1: verify, resume source; Hermes skips the elapsed slot "
         "(books one 'missed' ledger row)"},
        {"when": (s + RESTORE_AFTER).isoformat(), "do": "wake-up 2: once next_run_at > slot, restore original "
         "grace_minutes/catch_up"},
    ]


def _restore_args(rec: Dict[str, Any]) -> Dict[str, Any]:
    g = rec.get("orig_grace")
    args: Dict[str, Any] = {"grace_minutes": "" if g is None else g}
    if rec.get("orig_catch_up") is True:
        args["catch_up"] = True
    return args


def _ours(raw: Dict[str, Any], key: str) -> bool:
    m = parse_marker(raw.get("paused_reason"))
    return bool(m and m.get("k") == key and (raw.get("state") == "paused" or not raw.get("enabled", True)))


def decide(rec: Dict[str, Any], raw: Optional[Dict[str, Any]], fp_now: Optional[str],
           now: datetime) -> Tuple[List[str], List[str]]:
    """(actions, warnings) for one skip record. Pure. Actions: resume, restore, cleanup, done, abort."""
    key, slot, phase = rec["key"], rec["original_slot"], rec.get("phase")
    if phase in {"done", "aborted"}:
        return [], []
    if raw is None:
        return ["abort"], [f"skip {key[:8]}: source job is gone; aborted"]
    tag = f"skip {key[:8]} for job {raw['id']}"
    if fp_now != rec.get("fingerprint"):
        acts = ["restore"] + (["resume"] if _ours(raw, key) else []) + ["cleanup", "abort"]
        return acts, [f"{tag}: source edited (schedule/prompt/config); aborted, settings restored"]
    if phase == "arming":  # interrupted mid-arm: undo whatever landed
        return (["restore"] + (["resume"] if _ours(raw, key) else []) + ["cleanup", "abort"],
                [f"{tag}: arming was interrupted; undone (the original slot will run)"])
    if phase == "paused":
        if not _ours(raw, key):
            if raw.get("state") == "paused" or not raw.get("enabled", True):
                return ["restore", "cleanup", "abort"], [f"{tag}: job was paused manually; aborted, left paused"]
            return ["restore", "cleanup", "abort"], [f"{tag}: job was resumed manually before the slot "
                                                    "passed; aborted (the original slot may run)"]
        if now < _dt(slot) + RESUME_AFTER:
            return [], []
        return ["resume"], []
    if phase == "resumed":
        if raw.get("state") == "paused" or not raw.get("enabled", True):
            return ["restore", "cleanup", "abort"], [f"{tag}: job paused manually after resume; aborted"]
        if raw.get("last_run_at") and planner.instant(raw["last_run_at"]) >= planner.instant(slot):
            return ["restore", "cleanup", "done"], [f"{tag}: the original slot RAN; skip failed (copy still runs)"]
        nxt = raw.get("next_run_at")
        if nxt and planner.instant(nxt) > planner.instant(slot):
            return ["restore", "cleanup", "done"], []
        warn = [f"{tag}: Hermes has not consumed the elapsed slot yet; waiting"] if now > _dt(slot) + STALE_AFTER else []
        return [], warn
    return [], [f"{tag}: unknown phase {phase!r}; untouched"]


Cron = Callable[[Dict[str, Any]], Any]


def arm(cron: Cron, check: Callable, rec: Dict[str, Any], raw: Dict[str, Any], save: Callable[[], None],
        install_script: Callable[[], str]) -> None:
    """Journal first, then pause -> grace/catch_up -> wake-ups. Each step saved."""
    rec.update(phase="arming", orig_grace=raw.get("grace_minutes"), orig_catch_up=raw.get("catch_up"))
    save()
    check(cron({"action": "pause", "job_id": raw["id"],
                "reason": marker(rec["key"], rec["original_slot"], rec["orig_grace"], rec["orig_catch_up"])}))
    upd: Dict[str, Any] = {"grace_minutes": 0}
    if rec["orig_catch_up"] is True:
        upd["catch_up"] = False
    check(cron({"action": "update", "job_id": raw["id"], **upd}))
    script = install_script()
    rec["wake_ids"] = []
    for n, delta in ((1, RESUME_AFTER), (2, RESTORE_AFTER)):
        res = check(cron({"action": "create", "no_agent": True, "script": script, "deliver": "local",
                          "repeat": 1, "schedule": (_dt(rec["original_slot"]) + delta).isoformat(),
                          "name": f"{WAKE_PREFIX}{rec['key'][:8]} #{n}"}))
        rec["wake_ids"].append(res.get("job_id"))
        save()
    rec["phase"] = "paused"
    save()


def execute(cron: Cron, check: Callable, rec: Dict[str, Any], raw: Optional[Dict[str, Any]],
            actions: List[str], live_ids: set) -> None:
    for a in actions:
        if a == "restore" and raw is not None:
            check(cron({"action": "update", "job_id": raw["id"], **_restore_args(rec)}))
        elif a == "resume" and raw is not None:
            check(cron({"action": "resume", "job_id": raw["id"]}))
            rec["phase"] = "resumed"
        elif a == "cleanup":
            for wid in rec.get("wake_ids") or []:
                if wid and wid in live_ids:
                    check(cron({"action": "remove", "job_id": wid}))
        elif a in {"done", "abort"}:
            rec["phase"] = "done" if a == "done" else "aborted"


def rebuild(raw_jobs: List[Dict[str, Any]], recs: Dict[str, Any], fp: Callable[[Dict[str, Any]], str]) -> None:
    """Lost state: recover an armed skip from the source job's own paused_reason marker."""
    for raw in raw_jobs:
        m = parse_marker(raw.get("paused_reason"))
        if not m or m["k"] in recs or not _ours(raw, m["k"]):
            continue
        recs[m["k"]] = {"key": m["k"], "source_job_id": raw["id"], "original_slot": m["slot"],
                        "orig_grace": m.get("grace"), "orig_catch_up": m.get("catch_up"),
                        "fingerprint": fp(raw), "mode": "skip", "status": "pending", "phase": "paused",
                        "rebuilt": True, "wake_ids": [
                            j["id"] for j in raw_jobs if str(j.get("name", "")).startswith(WAKE_PREFIX + m["k"][:8])]}


SCRIPT = '''#!/usr/bin/env python3
"""Installed by the hermes-jobs plugin (experimental skip). Wakes its reconciler; silent on success."""
import subprocess, sys
r = subprocess.run([{hermes!r}, "hermes-jobs", "skip-tick", "--quiet"], capture_output=True, text=True, timeout=300)
out = (r.stdout + r.stderr).strip()
if out:
    print(out[-4000:])
sys.exit(r.returncode)
'''
