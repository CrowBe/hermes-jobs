"""Pure sync planner: (cron jobs, tagged calendar events, last-sync state) -> ops. No I/O.

Rules: cron is the system of record. Only events tagged with a hermes job id are considered;
untagged events are ignored and never become jobs. Event text is never parsed for tools,
permissions, or prompts -- only start time and title flow back, as schedule metadata.
Conflicts: cron edits win. A moved recurring occurrence creates a one-shot copy; it never
rewrites the recurring schedule. Hermes has no public skip-occurrence API: duplicate-run
warnings are part of the plan, not a promise of suppression.
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

    pending_children = {last["pending_move"].get("child_id") for last in state.values() if last.get("pending_move")}
    for jid, job in by_id.items():
        if jid in pending_children:
            continue  # parent owns transfer recovery; absence is not a human deletion
        ev, last, h = tagged.get(jid), state.get(jid), job_hash(job)
        last = last or {}
        body = event_body(job)
        # A copied occurrence has its own lifecycle. Completed/removed copies never respawn.
        if last.get("source_job_id") and job.get("terminal"):
            if ev:
                ops.append(_op("cron->calendar", "delete_event", jid, "one-shot finished; retain cron history", event_id=ev["id"]))
            continue
        if last.get("move_intent"):
            ops.append(_op("report", "notice", jid, "uncertain one-shot create result: reconcile move_intent in sync state before retry; no duplicate created"))
            continue
        if last.get("pending_move"):
            ops.append(_op("calendar->cron", "finish_move", jid, "retry event ownership transfer; no new copy",
                           **last["pending_move"]))
            continue
        # Show the next repeating slot while the original (unsuppressed) slot is outstanding.
        if last.get("original_slot") == job.get("start") and last.get("board_start"):
            body["start"] = last["board_start"]
        if ev is None:
            if last.get("paused_by_delete"):
                if job.get("enabled", True) and job.get("start"):
                    ops.append(_op("cron->calendar", "insert_event", jid, "job resumed in Hermes; recreate event", event=body))
                else:
                    ops.append(_op("report", "notice", jid, pause_message(jid)))
            elif last.get("event_id"):
                ops.append(_op("calendar->cron", "pause_job", jid, pause_message(jid),
                               resume_command=resume_command(jid)))
            elif job.get("start") and not job.get("terminal"):
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
                if job.get("schedule_kind") == "once":
                    ops.append(_op("calendar->cron", "update_job", jid, "one-shot moved; update its single instant",
                                   updates={"schedule": ev["start"]}))
                elif job.get("copy_error"):
                    ops.append(_op("report", "notice", jid, "move refused: " + job["copy_error"]))
                elif not job.get("next_after"):
                    ops.append(_op("report", "notice", jid, "move refused: cannot calculate next occurrence safely"))
                else:
                    args = dict(job.get("copy_args") or {"prompt": job.get("prompt", "")})
                    args.update(schedule=ev["start"], repeat=1,
                                name=f"{job.get('name') or jid} (moved {ev['start'][:10]})")
                    ops.append(_op("calendar->cron", "create_once", jid,
                                   "moved occurrence creates one-shot copy; recurring schedule unchanged",
                                   create_args=args, event_id=ev["id"], moved_start=ev["start"],
                                   original_slot=job.get("start"), board_start=job["next_after"],
                                   warning=SKIP_WARNING))
            else:
                ops.append(_op("state", "record", jid, "event moved while job paused: kept on calendar only",
                               event_id=ev["id"]))
        if retitled:
            name = (ev.get("summary") or "").removeprefix(PAUSED).strip()
            if name and name != job.get("name"):
                ops.append(_op("calendar->cron", "update_job", jid, "event retitled", updates={"name": name}))
        if (not moved and not retitled and job.get("enabled", True) and job.get("start")
                and instant(ev.get("start")) != instant(body["start"])):
            ops.append(_op("cron->calendar", "patch_event", jid, "cron next run advanced",
                           event_id=ev["id"], event={"start": body["start"]}))
    return ops


SKIP_WARNING = ("Hermes cronjob_manage has no skip-occurrence API. The original occurrence WILL still run; "
                "this creates an ADDITIONAL one-shot, not an exact reschedule. No pause or schedule rewrite is attempted. "
                "The source event previews its following slot; the original slot still runs in Hermes.")


def resume_command(jid: str) -> str:
    import shlex
    return "hermes cron resume " + shlex.quote(jid)


def pause_message(jid: str) -> str:
    return f"job {jid} paused because its calendar event was deleted; resume with {resume_command(jid)}"


def _differs(ev: Dict[str, Any], body: Dict[str, Any]) -> bool:
    return (ev.get("summary") != body["summary"] or ev.get("description") != body["description"]
            or instant(ev.get("start")) != instant(body["start"]))
