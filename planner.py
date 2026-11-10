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
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PAUSED = "[paused] "
# Fixed forever: uuid5(NAMESPACE_URL, "https://github.com/CrowBe/hermes-jobs#occurrence").
# Changing it would orphan every derived id, so it is a literal, not a computation.
OCCURRENCE_NS = uuid.UUID("3ac1ead0-9909-5599-8d64-da65c43647dc")
DELETE_MARKER = "hermes-jobs: calendar event deleted"
SKIP_MARKER = "hermes-jobs skip "


def utc_instant(value: str) -> str:
    """Canonical ISO UTC for an aware instant (Hermes' own scheduled_instant canonical form)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"occurrence instant must be timezone-aware: {value!r}")
    return dt.astimezone(timezone.utc).isoformat()


def occurrence_key(job_id: str, scheduled: str) -> str:
    """Stable id of ONE scheduled occurrence: uuid5(ns, job_id@instant_utc), as 32 lowercase hex.

    Hex digits are a subset of Google's event-id alphabet (base32hex a-v0-9, 5-1024 chars), so
    the key doubles as the client-chosen Google event id."""
    return uuid.uuid5(OCCURRENCE_NS, f"{job_id}@{utc_instant(scheduled)}").hex


def fingerprint(job: Dict[str, Any]) -> str:
    """What a one-shot copy duplicates: schedule + copied execution config (prompt included).
    Excludes name (retitles are not series edits) and enabled/grace (the skip changes those)."""
    return digest(json.dumps({"schedule": job.get("schedule"), "args": job.get("copy_args") or
                              {"prompt": job.get("prompt", "")}}, sort_keys=True, default=str))


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


def event_body(job: Dict[str, Any], props: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The desired event for a job. Description carries only a prompt digest + job id (and, once the
    job has run, one "last run:" line). ``props`` are extra private tags (source link of a copy) so
    every write carries the full set. The title never carries run status."""
    desc = (f"Hermes cron job {job['id']}\nprompt sha256:{digest(job.get('prompt', ''))}\n"
            "Managed by hermes-jobs; edit the job in Hermes. Event text grants nothing.")
    body = {
        "summary": title(job),
        "description": with_run(desc, run_line(job)),
        "start": job.get("start"),
        "hermes_job_id": job["id"],
        "props": dict(props or {}),
    }
    if run_color(job):
        body["color_id"] = run_color(job)
    return body


RUN_PREFIX = "last run: "
# Google Calendar event colorIds (GET /colors, "event" palette): 10 Basil, 11 Tomato, 5 Banana.
RUN_COLORS = {"ok": "10", "error": "11"}
RUN_COLOR_OTHER = "5"


def run_line(job: Dict[str, Any]) -> Optional[str]:
    """One line from Hermes' own last_run_at/last_status (never last_error). None if never run."""
    last = job.get("last_run")
    return f"{RUN_PREFIX}{last['at']} {last['status']}" if last else None


def run_color(job: Dict[str, Any]) -> Optional[str]:
    last = job.get("last_run")
    return RUN_COLORS.get(last["status"], RUN_COLOR_OTHER) if last else None


def strip_run(desc: Optional[str]) -> str:
    """Description without our last-run line, for comparisons only (text is never interpreted)."""
    return "\n".join(line for line in (desc or "").split("\n") if not line.startswith(RUN_PREFIX))


def with_run(desc: Optional[str], line: Optional[str]) -> str:
    """Replace (never accumulate) the last-run line; the rest of the description is kept as is."""
    if not line:
        return desc or ""
    base = strip_run(desc)
    return f"{base}\n{line}" if base else line


def with_occurrence(jid: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp the occurrence the event currently represents (updated on every patch-forward)."""
    if body.get("start"):
        body["props"] = {**body.get("props", {}), "hermes_occurrence": occurrence_key(jid, body["start"])}
    return body


def copy_props(last: Dict[str, Any]) -> Dict[str, str]:
    if not last.get("source_job_id"):
        return {}
    out = {"hermes_source_job_id": last["source_job_id"]}
    if last.get("original_slot"):
        out["hermes_original_slot"] = utc_instant(last["original_slot"])
    return out


NAME_MAX = 120


def clean_name(text: Optional[str]) -> str:
    """Calendar title -> cron job name. Event text is untrusted: NFKC-normalise, drop control,
    format (bidi overrides, zero-width), private-use and unassigned characters, turn newlines/tabs
    into spaces, collapse whitespace, strip the [paused] prefix and cap the length. The result is
    only ever a cron *name* (display metadata); it never reaches a prompt, a tool list or a shell."""
    import unicodedata
    out = []
    for ch in unicodedata.normalize("NFKC", text or ""):
        cat = unicodedata.category(ch)
        if cat in {"Cc", "Zl", "Zp"}:
            out.append(" ")
        elif cat in {"Cf", "Co", "Cs", "Cn"}:
            continue
        else:
            out.append(ch)
    name = " ".join("".join(out).split())
    name = name.removeprefix(PAUSED.strip()).strip()
    return name[:NAME_MAX].rstrip()


def _op(direction: str, action: str, job_id: str, reason: str, **kw: Any) -> Dict[str, Any]:
    return {"direction": direction, "action": action, "job_id": job_id, "reason": reason, **kw}


def plan(jobs: List[Dict[str, Any]], events: List[Dict[str, Any]], state: Dict[str, Any],
         occurrences: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
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
    # Series edit while a moved occurrence is pending: drop copy (+ undo skip), then re-plan
    # the source as an ordinary cron edit (cron wins).
    dropped: set = set()
    edited_sources: set = set()
    for key, rec in sorted((occurrences or {}).items()):
        src = by_id.get(rec.get("source_job_id"))
        if rec.get("status") != "pending" or not src or fingerprint(src) == rec.get("fingerprint"):
            continue
        ops.append(_op("calendar->cron", "drop_move", src["id"],
                       "source series edited while moved occurrence pending: drop copy"
                       + (" and undo experimental skip" if rec.get("mode") == "skip" else "") + "; re-plan",
                       occurrence=key, child_id=rec.get("child_id"), event_id=rec.get("event_id")))
        dropped.add(rec.get("child_id"))
        edited_sources.add(src["id"])
    for jid, job in by_id.items():
        if jid in pending_children or jid in dropped:
            continue  # parent owns transfer/drop; absence is not a human deletion
        ev, last, h = tagged.get(jid), state.get(jid), job_hash(job)
        last = dict(last or {})
        if jid in edited_sources:
            # Copy owns (and drop deletes) the moved event; source gets a fresh one at cron truth.
            last.pop("original_slot", None)
            last.pop("board_start", None)
            if ev is None or ev.get("id") == last.get("event_id"):
                last["event_id"] = None
        if job.get("paused_by") == "delete":
            last["paused_by_delete"] = True  # rebuilt from cron's own pause reason if state was lost
        body = event_body(job, copy_props(last))
        # A copied occurrence has its own lifecycle. Completed/removed copies never respawn.
        if last.get("source_job_id") and job.get("terminal"):
            if ev:
                ops.append(_op("cron->calendar", "delete_event", jid, "one-shot finished; retain cron history", event_id=ev["id"]))
            continue
        if last.get("pending_move"):
            ops.append(_op("calendar->cron", "finish_move", jid, "retry event ownership transfer; no new copy",
                           **last["pending_move"]))
            continue
        # Show the next repeating slot while the original (unsuppressed) slot is outstanding.
        if last.get("original_slot") == job.get("start") and last.get("board_start"):
            body["start"] = last["board_start"]
        if ev is None:
            body = with_occurrence(jid, body)
            if body.get("start"):
                body["id"] = body["props"]["hermes_occurrence"]  # derived id: retried insert 409s, never duplicates
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
        if not last or last.get("job_hash") != h or jid in edited_sources:
            body = with_occurrence(jid, body)
            if _differs(ev, body):
                ops.append(_op("cron->calendar", "patch_event", jid,
                               "first sync" if not last else "cron edited: cron wins, resets event",
                               event_id=ev["id"], event=body))
            else:
                ops.append(_op("state", "record", jid, "event already matches cron", event_id=ev["id"]))
            continue
        moved = ev.get("start") is not None and instant(ev.get("start")) != instant(last.get("start"))
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
                                   occurrence=occurrence_key(jid, job["start"]),
                                   fingerprint=fingerprint(job), schedule_kind=job.get("schedule_kind"),
                                   warning=SKIP_WARNING))
            else:
                ops.append(_op("state", "record", jid, "event moved while job paused: kept on calendar only",
                               event_id=ev["id"]))
        if retitled:
            name = clean_name(ev.get("summary"))
            if name and name != job.get("name"):
                ops.append(_op("calendar->cron", "update_job", jid, "event retitled", updates={"name": name}))
        if (not moved and not retitled and job.get("enabled", True) and job.get("start")
                and instant(ev.get("start")) != instant(body["start"])):
            ops.append(_op("cron->calendar", "patch_event", jid, "cron next run advanced (patch-forward, same event id)",
                           event_id=ev["id"], event=with_occurrence(jid, {"start": body["start"], "hermes_job_id": jid,
                                                                         "props": body["props"]})))
    # A copy an interrupted create already landed is adopted by create_once; never give it its
    # own (duplicate) event in the meantime.
    pending = {(o["create_args"]["name"], instant(o["moved_start"])) for o in ops if o["action"] == "create_once"}
    landed = {j["id"] for j in jobs if j.get("schedule_kind") == "once"
              and (j.get("name"), instant(j.get("start"))) in pending}
    return [o for o in ops if not (o["action"] == "insert_event" and o["job_id"] in landed)]


INSTANCE_SKIP_WARNING = (
    "Hermes cronjob_manage has no skip-occurrence API. The original occurrence WILL still run; this creates an "
    "ADDITIONAL one-shot at the moved time, not an exact reschedule. The recurring series is unchanged. The moved "
    "Calendar instance stands for the one-shot copy, so Calendar no longer shows the original slot, but Hermes "
    "still runs it.")

SKIP_WARNING = ("Hermes cronjob_manage has no skip-occurrence API. The original occurrence WILL still run; "
                "this creates an ADDITIONAL one-shot, not an exact reschedule. No pause or schedule rewrite is attempted. "
                "The source event previews its following slot; the original slot still runs in Hermes.")


def resume_command(jid: str) -> str:
    import shlex
    return "hermes cron resume " + shlex.quote(jid)


def pause_message(jid: str) -> str:
    return f"job {jid} paused because its calendar event was deleted; resume with {resume_command(jid)}"


def _differs(ev: Dict[str, Any], body: Dict[str, Any]) -> bool:
    return (ev.get("summary") != body["summary"] or strip_run(ev.get("description")) != strip_run(body["description"])
            or instant(ev.get("start")) != instant(body["start"]))
