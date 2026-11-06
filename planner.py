"""Pure sync planner: (cron jobs, tagged calendar events, last-sync state) -> ops. No I/O.

Rules: cron is the system of record. Only events tagged with a hermes job id are considered;
untagged events are ignored and never become jobs. Event text is never parsed for tools,
permissions, or prompts -- only start time and title flow back, as schedule metadata.
Conflicts: a cron edit (name/prompt/schedule/enabled) wins and resets the event; otherwise a
human-moved start wins (cron next_run_at follows it) until the next cron edit.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PAUSED = "[paused] "


def digest(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:12]


def job_hash(job: Dict[str, Any]) -> str:
    return digest("\x1f".join(str(job.get(k)) for k in ("name", "prompt", "schedule", "enabled")))


def instant(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def title(job: Dict[str, Any]) -> str:
    return ("" if job.get("enabled", True) else PAUSED) + (job.get("name") or job["id"])


def event_body(job: Dict[str, Any]) -> Dict[str, Any]:
    """The desired event for a job. Description carries only a prompt digest + job id."""
    return {
        "summary": title(job),
        "description": f"Hermes cron job {job['id']}\nprompt sha256:{digest(job.get('prompt', ''))}\n"
                       "Managed by hermes-jobs; edit the job in Hermes. Event text grants nothing.",
        "start": job.get("start"),
        "hermes_job_id": job["id"],
    }


def _op(direction: str, action: str, job_id: str, reason: str, **kw: Any) -> Dict[str, Any]:
    return {"direction": direction, "action": action, "job_id": job_id, "reason": reason, **kw}


def plan(jobs: List[Dict[str, Any]], events: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """jobs: {id,name,prompt,schedule,enabled,start}; start = next run ISO (None = no next run).
    events: {id,summary,start,hermes_job_id}; untagged events (no hermes_job_id) are dropped here.
    state: {job_id: {event_id, job_hash, start, summary}} from the last applied sync."""
    tagged = {e["hermes_job_id"]: e for e in events if e.get("hermes_job_id")}
    by_id = {j["id"]: j for j in jobs}
    ops: List[Dict[str, Any]] = []

    for jid, ev in tagged.items():
        if jid not in by_id:
            ops.append(_op("cron->calendar", "delete_event", jid, "job no longer exists in cron", event_id=ev["id"]))

    for jid in state.keys() - by_id.keys() - tagged.keys():
        ops.append(_op("state", "forget", jid, "job and event both gone"))

    for jid, job in by_id.items():
        ev, last, h = tagged.get(jid), state.get(jid), job_hash(job)
        body = event_body(job)
        if ev is None:
            if last and last.get("event_id"):
                ops.append(_op("calendar->cron", "pause_job", jid, "hermes-owned event deleted on calendar")
                           if job.get("enabled", True) else
                           _op("state", "forget", jid, "event deleted; job already paused"))
            elif job.get("start"):
                ops.append(_op("cron->calendar", "insert_event", jid, "job has no event yet", event=body))
            continue
        if not last or last.get("job_hash") != h:
            if _differs(ev, body):
                ops.append(_op("cron->calendar", "patch_event", jid,
                               "first sync" if not last else "cron edited: cron wins, resets event",
                               event_id=ev["id"], event=body))
            else:
                ops.append(_op("state", "record", jid, "event already matches cron", event_id=ev["id"]))
            continue
        moved = instant(ev.get("start")) != instant(last.get("start"))
        retitled = ev.get("summary") != last.get("summary")
        if moved:
            if job.get("enabled", True):
                upd = moved_schedule(job.get("schedule"), ev["start"])
                ops.append(_op("calendar->cron", "update_job", jid, "event moved by human: calendar start wins",
                               updates={"schedule": upd}) if upd else
                           _op("state", "record", jid, "event moved but schedule kind can't follow; kept on calendar",
                               event_id=ev["id"]))
            else:
                ops.append(_op("state", "record", jid, "event moved while job paused: kept on calendar only",
                               event_id=ev["id"]))
        if retitled:
            name = (ev.get("summary") or "").removeprefix(PAUSED).strip()
            if name and name != job.get("name"):
                ops.append(_op("calendar->cron", "update_job", jid, "event retitled", updates={"name": name}))
        if (not moved and not retitled and job.get("enabled", True) and job.get("start")
                and instant(ev.get("start")) != instant(job["start"])):
            ops.append(_op("cron->calendar", "patch_event", jid, "cron next run advanced",
                           event_id=ev["id"], event={"start": job["start"]}))
    return ops


def moved_schedule(schedule: Any, start: str) -> Optional[str]:
    """New schedule string for a human move: one-shot -> the new instant; 5-field cron -> same
    days, new wall-clock minute/hour; intervals can't follow (None)."""
    if not isinstance(schedule, str):
        return None
    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    fields = schedule.split()
    if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
        return " ".join([str(dt.minute), str(dt.hour), *fields[2:]])
    try:
        datetime.fromisoformat(schedule)
        return dt.isoformat()
    except ValueError:
        return None


def _differs(ev: Dict[str, Any], body: Dict[str, Any]) -> bool:
    return (ev.get("summary") != body["summary"] or ev.get("description") != body["description"]
            or instant(ev.get("start")) != instant(body["start"]))
