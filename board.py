"""Board planner: Google RECURRING masters for jobs whose cron converts exactly to an RRULE, and
today's single patch-forward events (planner.plan) for everything else. Pure, no I/O.

Event kinds after gcal.from_google:
  master     recurrence set, no recurring_event_id   (one per recurring job; DTSTART fixed)
  single     no recurrence, no recurring_event_id     (planner.plan path)
  exception  recurring_event_id set: a moved ("this event") or cancelled instance of a master
  split      a master whose id is <our master id>_R<date> (Google's "this and following"),
             possibly WITHOUT our tags; tied to a job only through that id prefix.
History masters (ours, shortened with UNTIL by a split) are never edited, adopted or deleted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import planner, rrule

P = planner
_op = planner._op


def _wall(ev: Dict[str, Any], tz: Optional[str]) -> Optional[datetime]:
    if not ev.get("start"):
        return None
    dt = datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
    for zone in (ev.get("tz"), tz):
        try:
            return dt.astimezone(ZoneInfo(zone)) if zone else dt
        except Exception:  # noqa: BLE001 -- untrusted zone string from Calendar
            continue
    return dt


def is_master(ev) -> bool:
    return bool(ev.get("recurrence")) and not ev.get("recurring_event_id")


def is_history(ev) -> bool:
    return is_master(ev) and rrule.has_until(ev.get("recurrence"))


def master_body(job: Dict[str, Any], props: Dict[str, str]) -> Dict[str, Any]:
    body = P.with_occurrence(job["id"], P.event_body(job, props))
    body.update(recurrence=[job["rrule"]], tz=job["tz"])
    return body


def _master_differs(ev, body) -> bool:
    wall_ev, wall_b = _wall(ev, body["tz"]), _wall(body, body["tz"])
    return (ev.get("summary") != body["summary"] or ev.get("description") != body["description"]
            or list(ev.get("recurrence") or []) != body["recurrence"] or (ev.get("tz") or body["tz"]) != body["tz"]
            or (wall_ev and wall_b and (wall_ev.hour, wall_ev.minute) != (wall_b.hour, wall_b.minute)))


def choose_current(jid: str, last: Dict[str, Any], series: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    mine = [e for e in series if e.get("hermes_job_id") == jid]
    for e in mine:
        if e["id"] == last.get("event_id"):
            return e
    hist = set(last.get("history_ids") or [])
    active = [e for e in mine if is_master(e) and not is_history(e) and e["id"] not in hist]
    if active:
        return active[0]
    singles = [e for e in mine if not is_master(e)]
    return singles[0] if singles else None


def plan(jobs: List[Dict[str, Any]], events: List[Dict[str, Any]], state: Dict[str, Any],
         occurrences: Optional[Dict[str, Any]] = None, recurring: bool = True) -> List[Dict[str, Any]]:
    occurrences = occurrences or {}
    live = [e for e in events if e.get("status") != "cancelled" or e.get("recurring_event_id")]
    exceptions: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for e in live:
        if e.get("recurring_event_id"):
            exceptions.setdefault(e["recurring_event_id"], {})[e["id"]] = e
    series = [e for e in live if not e.get("recurring_event_id")]
    by_id = {j["id"]: j for j in jobs}
    instance_children = {r.get("child_id") for r in occurrences.values() if r.get("instance_backed")}

    rec_jobs: Dict[str, Optional[Dict[str, Any]]] = {}
    for jid, job in by_id.items():
        if jid in instance_children:
            continue
        last = state.get(jid) or {}
        cur = choose_current(jid, last, series)
        wants = recurring and job.get("rrule") and not job.get("terminal")
        if wants or (cur is not None and is_master(cur)):
            rec_jobs[jid] = cur

    ops: List[Dict[str, Any]] = []
    # Masters of jobs that no longer exist (history masters are left alone).
    for e in series:
        if is_master(e) and not is_history(e) and e.get("hermes_job_id") and e["hermes_job_id"] not in by_id:
            ops.append(_op("cron->calendar", "delete_event", e["hermes_job_id"], "job no longer exists in cron",
                           event_id=e["id"]))

    for jid, cur in rec_jobs.items():
        ops += _plan_job(by_id[jid], dict(state.get(jid) or {}), cur, series, exceptions, occurrences, recurring)

    # Single path for everyone else, on single events only.
    skip_ids = set(rec_jobs) | instance_children
    single_jobs = [j for j in jobs if j["id"] not in skip_ids]
    single_events = [e for e in series if not is_master(e) and e.get("hermes_job_id") not in skip_ids
                     and e.get("hermes_job_id")]
    single_state = {k: v for k, v in state.items() if k not in skip_ids}
    single_occ = {k: r for k, r in occurrences.items() if r.get("source_job_id") not in rec_jobs}
    ops += planner.plan(single_jobs, single_events, single_state, single_occ)
    return ops


def _plan_job(job, last, cur, series, exceptions, occurrences, recurring) -> List[Dict[str, Any]]:
    jid = job["id"]
    ops: List[Dict[str, Any]] = []
    wants = bool(recurring and job.get("rrule") and not job.get("terminal"))
    props = P.copy_props(last)
    paused_by_delete = last.get("paused_by_delete") or job.get("paused_by") == "delete"

    # Series edit (fingerprint changed) drops pending moved/cancelled-instance work, as today.
    edited = False
    for key, rec in sorted(occurrences.items()):
        if rec.get("source_job_id") == jid and rec.get("status") == "pending" \
                and P.fingerprint(job) != rec.get("fingerprint"):
            edited = True
            ops.append(_op("calendar->cron", "drop_move", jid, "source series edited while a moved/skipped "
                           "occurrence was pending: drop copy/undo skip; re-plan", occurrence=key,
                           child_id=rec.get("child_id"), event_id=rec.get("event_id")))

    if cur is None:
        if paused_by_delete:
            if job.get("enabled", True) and job.get("start") and wants:
                body = master_body(job, props)
                body["id"] = body["props"]["hermes_occurrence"]
                ops.append(_op("cron->calendar", "insert_event", jid, "job resumed in Hermes; recreate recurring event",
                               event=body))
            else:
                ops.append(_op("report", "notice", jid, P.pause_message(jid)))
        elif last.get("event_id"):
            ops.append(_op("calendar->cron", "pause_job", jid, P.pause_message(jid) + " (recurring series deleted)",
                           resume_command=P.resume_command(jid)))
        elif wants and job.get("start"):
            body = master_body(job, props)
            body["id"] = body["props"]["hermes_occurrence"]
            ops.append(_op("cron->calendar", "insert_event", jid, "job has no event yet: recurring master "
                           + job["rrule"], event=body))
        return ops

    if not is_master(cur):  # legacy/single event for a job that should now be recurring
        if wants and job.get("start"):
            body = master_body(job, props)
            ops.append(_op("cron->calendar", "convert_to_recurring", jid,
                           f"single event -> recurring master ({job['rrule']}, {job['tz']}); same event id, patched",
                           event_id=cur["id"], event=body))
        return ops

    if not wants:  # master but job is now single (fallback mode, or cron no longer converts)
        if job.get("start") and not job.get("terminal"):
            body = P.with_occurrence(jid, P.event_body(job, props))
            body["recurrence"] = []
            ops.append(_op("cron->calendar", "convert_to_single", jid,
                           "recurring master -> single patch-forward event ("
                           + ("event mode single" if not recurring else job.get("rrule_refusal") or "no RRULE") + ")",
                           event_id=cur["id"], event=body))
        return ops

    body = master_body(job, props)
    hashed = last.get("job_hash") == P.job_hash(job)

    # --- "This and following": our master gained UNTIL; adopt the new series. -----------------
    if is_history(cur) and hashed:
        cands = [e for e in series if is_master(e) and not is_history(e) and e["id"] != cur["id"]
                 and e["id"] not in (last.get("history_ids") or [])
                 and (e.get("hermes_job_id") == jid
                      or (not e.get("hermes_job_id") and e["id"].startswith(cur["id"] + "_R")))]
        if len(cands) != 1:
            ops.append(_op("report", "notice", jid,
                           "series was given an end date in Calendar but no single follow-on series was found "
                           f"({len(cands)} candidates); cron jobs cannot end on a date; nothing changed"))
            return ops
        new = cands[0]
        if (new.get("tz") or job["tz"]) != job["tz"]:
            ops.append(_op("report", "notice", jid, f"split series uses time zone {new.get('tz')}, job uses "
                           f"{job['tz']}; nothing changed"))
            return ops
        expr, why = rrule.rrule_to_cron(new.get("recurrence"), _wall(new, job["tz"]))
        if not expr:
            ops.append(_op("report", "notice", jid, f"'this and following' rule cannot become cron ({why}); "
                           "nothing changed, series not adopted"))
            return ops
        until = rrule.until_instant(cur.get("recurrence"))
        now = bool(until is None or P.instant(job.get("start")) is None
                   or P.instant(job["start"]) > until.timestamp())
        changes = rrule.canonical_cron(job["cron_expr"]) != expr
        tagged = dict(body)
        for k in ("start", "recurrence", "tz", "id"):
            tagged.pop(k, None)
        tagged["props"] = {k: v for k, v in tagged["props"].items() if k != "hermes_occurrence"}
        ops.append(_op("calendar->cron", "adopt_split", jid,
                       "'this and following': adopt series " + new["id"] + " as the job's event; old series kept as "
                       "history" + ("; cron -> " + expr if changes else "; rule unchanged")
                       + ("" if now or not changes else f" (deferred until the old series ends {until.isoformat()})"),
                       series_id=new["id"], old_id=cur["id"], event=tagged,
                       cron=expr if changes else None, cron_now=now, until=until.isoformat() if until else None))
        return ops

    if last.get("split_pending"):
        sp = last["split_pending"]
        if not hashed:
            ops.append(_op("report", "notice", jid, "cron edited while a 'this and following' change was pending: "
                           "cron wins; pending split dropped"))
        elif P.instant(job.get("start")) and P.instant(job["start"]) > P.instant(sp["until"]):
            ops.append(_op("calendar->cron", "apply_split", jid, "old series ended: apply 'this and following' rule",
                           updates={"schedule": sp["cron"]}))
            return ops
        else:
            return ops  # still inside the old series; nothing to do yet

    if not hashed or edited:
        if _master_differs(cur, body):
            ops.append(_op("cron->calendar", "patch_event", jid,
                           "first sync" if not last else "cron edited: cron wins, rewrites master RRULE/start",
                           event_id=cur["id"], event=body))
        else:
            ops.append(_op("state", "record", jid, "master already matches cron", event_id=cur["id"]))
        return ops

    # --- "All events": master changed in Calendar. -------------------------------------------
    if (cur.get("tz") or job["tz"]) != job["tz"]:
        ops.append(_op("report", "notice", jid, f"series time zone changed to {cur.get('tz')} in Calendar; "
                       f"job uses {job['tz']}; nothing changed"))
    else:
        expr, why = rrule.rrule_to_cron(cur.get("recurrence"), _wall(cur, job["tz"]))
        if not expr:
            ops.append(_op("report", "notice", jid, f"Calendar series change cannot be expressed as cron ({why}); "
                           "nothing changed. Undo it in Calendar, or edit the job in Hermes (cron wins)."))
        elif expr != rrule.canonical_cron(job["cron_expr"]):
            ops.append(_op("calendar->cron", "update_job", jid, "'all events' changed in Calendar: cron follows "
                           f"({job['cron_expr']} -> {expr})", updates={"schedule": expr}))
    if cur.get("summary") != last.get("summary"):
        name = P.clean_name(cur.get("summary"))
        if name and name != job.get("name"):
            ops.append(_op("calendar->cron", "update_job", jid, "series retitled", updates={"name": name}))

    # --- Instances: "this event" moves and deleted single runs. ------------------------------
    for e in sorted((exceptions.get(cur["id"]) or {}).values(), key=lambda x: x.get("original_start") or ""):
        orig = e.get("original_start")
        if not orig or not job.get("start") or P.instant(orig) < P.instant(job["start"]):
            continue  # past instances are history
        key = P.occurrence_key(jid, orig)
        rec = occurrences.get(key)
        if e.get("status") == "cancelled":
            if rec and rec.get("child_id") and rec.get("instance_event_id") == e["id"] \
                    and rec.get("status") == "pending" and not rec.get("child_paused"):
                ops.append(_op("calendar->cron", "pause_job", rec["child_id"],
                               P.pause_message(rec["child_id"]) + " (moved instance deleted)",
                               resume_command=P.resume_command(rec["child_id"]), occurrence=key))
            elif not rec or rec.get("status") == "noticed":
                ops.append(_op("calendar->cron", "skip_instance", jid, f"instance {orig} deleted in Calendar",
                               original_slot=orig, occurrence=key, fingerprint=P.fingerprint(job), event_id=e["id"]))
            continue
        if rec:
            if rec.get("child_id") and rec.get("status") == "pending" and e.get("start") \
                    and P.instant(e["start"]) != P.instant(rec.get("moved_start")):
                ops.append(_op("calendar->cron", "update_job", rec["child_id"], "moved instance moved again: "
                               "one-shot copy follows", updates={"schedule": e["start"]}, occurrence=key,
                               moved_start=e["start"]))
            continue
        if not e.get("start"):
            ops.append(_op("report", "notice", jid, f"instance {orig} has no timed start (all-day?); ignored"))
        elif not job.get("enabled", True):
            ops.append(_op("state", "record", jid, "instance moved while job paused: kept on calendar only"))
        elif job.get("copy_error"):
            ops.append(_op("report", "notice", jid, "move refused: " + job["copy_error"]))
        else:
            args = dict(job.get("copy_args") or {"prompt": job.get("prompt", "")})
            args.update(schedule=e["start"], repeat=1, name=f"{job.get('name') or jid} (moved {e['start'][:10]})")
            ops.append(_op("calendar->cron", "create_once", jid, "'this event' moved: one-shot copy; series unchanged",
                           create_args=args, event_id=e["id"], moved_start=e["start"], original_slot=orig,
                           board_start=None, occurrence=key, fingerprint=P.fingerprint(job),
                           schedule_kind=job.get("schedule_kind"), instance_backed=True, warning=P.INSTANCE_SKIP_WARNING))
    return ops
