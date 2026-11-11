"""Recurring Google masters: convert table, the four live cases, split no-dup/no-orphan, fallback."""
import importlib
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import test_runtime as TR
from test_planner import job, ev, synced, T9, T10, T11

M = TR.M
B = importlib.import_module(M.__name__ + ".board")
R = importlib.import_module(M.__name__ + ".rrule")
G = importlib.import_module(M.__name__ + ".gcal")
P = M.planner
SYD = "Australia/Sydney"
DAILY = "RRULE:FREQ=DAILY"


def rjob(**kw):
    base = dict(cron_expr="0 9 * * *", tz=SYD, rrule=DAILY, rrule_refusal=None)
    base.update(kw)
    return job(**base)


def master(**kw):  # noqa: C901
    j = rjob()
    m = {"id": "m1", "summary": P.title(j), "description": P.event_body(j)["description"], "start": T9,
         "tz": SYD, "recurrence": [DAILY], "hermes_job_id": "j1", "props": {}, "status": "confirmed",
         "recurring_event_id": None, "original_start": None}
    m.update(kw)
    return m


def inst(start, orig, **kw):
    e = {"id": "m1_" + orig[:10].replace("-", ""), "summary": "Daily brief", "start": start, "tz": SYD,
         "recurrence": None, "hermes_job_id": "j1", "props": {}, "status": "confirmed",
         "recurring_event_id": "m1", "original_start": orig}
    e.update(kw)
    return e


def rstate(j=None, **kw):
    return synced(j or rjob(), **{"event_id": "m1", "kind": "recurring", **kw})


def acts(ops):
    return [o["action"] for o in ops]


# cron, Sydney RRULE, wall time used for the reverse direction
TABLE = [
    ("0 9 * * *", "RRULE:FREQ=DAILY", "2026-11-09T09:00"),
    ("0 18 * * 1-5", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "2026-11-09T18:00"),
    ("30 7 * * 1,3,5", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR", "2026-11-09T07:30"),
    ("0 9 1,15 * *", "RRULE:FREQ=MONTHLY;BYMONTHDAY=1,15", "2026-12-01T09:00"),
    ("0 9 25 12 *", "RRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=25", "2026-12-25T09:00"),
    ("0 9 * 6,7,8 *", "RRULE:FREQ=DAILY;BYMONTH=6,7,8", "2027-06-01T09:00"),
    ("0 9 * * 0", "RRULE:FREQ=WEEKLY;BYDAY=SU", "2026-11-08T09:00"),
]
REFUSED = ["0 2 * * *", "*/5 * * * *", "0 9,17 * * *", "0 9 1 * 1", "0 */2 * * *"]


class ConvertTableTest(unittest.TestCase):
    def test_round_trip(self):
        for cron, rule, wall in TABLE:
            with self.subTest(cron=cron):
                got, why = R.cron_to_rrule(cron, SYD)
                self.assertEqual((got, why), (rule, None))
                back, why = R.rrule_to_cron([rule], datetime.fromisoformat(wall).replace(tzinfo=ZoneInfo(SYD)))
                self.assertEqual(back, R.canonical_cron(cron), why)

    def test_refusals_stay_single(self):
        for cron in REFUSED:
            with self.subTest(cron=cron):
                self.assertIsNone(R.cron_to_rrule(cron, SYD)[0])

    def test_reverse_refusals(self):
        wall = datetime(2026, 11, 9, 9, tzinfo=ZoneInfo(SYD))
        for rule in ["RRULE:FREQ=DAILY;INTERVAL=2", "RRULE:FREQ=MONTHLY;BYDAY=1MO", "RRULE:FREQ=DAILY;COUNT=5",
                     "RRULE:FREQ=MONTHLY;BYMONTHDAY=-1", "RRULE:FREQ=DAILY;BYHOUR=9,17"]:
            with self.subTest(rule=rule):
                self.assertIsNone(R.rrule_to_cron([rule], wall)[0])

    def test_weekly_without_byday_uses_dtstart_weekday(self):
        thu = datetime(2026, 11, 12, 10, tzinfo=ZoneInfo(SYD))
        self.assertEqual(R.rrule_to_cron(["RRULE:FREQ=WEEKLY"], thu)[0], "0 10 * * 4")


class MasterLifecycleTest(unittest.TestCase):
    def test_new_convertible_job_inserts_one_master(self):
        ops = B.plan([rjob()], [], {})
        self.assertEqual(acts(ops), ["insert_event"])
        e = ops[0]["event"]
        self.assertEqual((e["recurrence"], e["tz"], e["start"], e["hermes_job_id"]), ([DAILY], SYD, T9, "j1"))
        self.assertEqual(e["id"], P.occurrence_key("j1", T9))
        g = G.to_google(e)
        self.assertEqual(g["recurrence"], [DAILY])
        self.assertEqual(g["start"]["timeZone"], SYD)
        self.assertEqual(g["extendedProperties"]["private"][G.TAG], "j1")

    def test_legacy_single_event_converts_in_place(self):
        ops = B.plan([rjob()], [ev(status="confirmed")], synced())
        self.assertEqual(acts(ops), ["convert_to_recurring"])
        self.assertEqual(ops[0]["event_id"], "e1")  # same id: no delete, no orphan
        self.assertEqual(ops[0]["event"]["recurrence"], [DAILY])

    def test_master_in_sync_and_does_not_patch_forward(self):
        self.assertEqual(B.plan([rjob()], [master()], rstate()), [])
        self.assertEqual(B.plan([rjob(start=T11)], [master()], rstate()), [])

    def test_cron_edit_wins_rewrites_master(self):
        j = rjob(schedule="0 10 * * *", cron_expr="0 10 * * *", start=T10)
        ops = B.plan([j], [master()], rstate())
        self.assertEqual(acts(ops), ["patch_event"])
        self.assertEqual((ops[0]["event"]["start"], ops[0]["event"]["recurrence"]), (T10, [DAILY]))

    def test_cron_no_longer_converts_goes_single(self):
        j = rjob(schedule="0 9,17 * * *", cron_expr="0 9,17 * * *", rrule=None, rrule_refusal="2 times")
        ops = B.plan([j], [master()], rstate())
        self.assertEqual(acts(ops), ["convert_to_single"])
        self.assertEqual(ops[0]["event"]["recurrence"], [])

    def test_deleted_master_is_soft_pause_then_recreated_on_resume(self):
        self.assertEqual(acts(B.plan([rjob()], [], rstate())), ["pause_job"])
        paused = rstate(paused_by_delete=True, event_id=None)
        self.assertEqual(acts(B.plan([rjob(enabled=False)], [], paused)), ["notice"])
        ops = B.plan([rjob()], [], paused)
        self.assertEqual(acts(ops), ["insert_event"])
        self.assertEqual(ops[0]["event"]["recurrence"], [DAILY])

    def test_removed_job_deletes_master_never_history(self):
        hist = master(id="m0", recurrence=["RRULE:FREQ=DAILY;UNTIL=20261101T000000Z"])
        ops = B.plan([], [master(), hist], {})
        self.assertEqual([(o["action"], o["event_id"]) for o in ops], [("delete_event", "m1")])

    def test_untagged_masters_never_become_jobs(self):
        stray = master(id="zzz", hermes_job_id=None)
        self.assertEqual(B.plan([], [stray], {}), [])
        self.assertEqual(B.plan([rjob()], [master(), stray], rstate()), [])


class FourCasesTest(unittest.TestCase):
    # 1. "This event"
    def test_this_event_move_is_instance_backed_one_shot(self):
        moved = "2026-11-08T11:00:00+11:00"
        ops = B.plan([rjob()], [master(), inst(moved, T11)], rstate())
        self.assertEqual(acts(ops), ["create_once"])
        op = ops[0]
        self.assertTrue(op["instance_backed"])
        self.assertEqual(op["warning"], P.INSTANCE_SKIP_WARNING)
        self.assertNotIn("previews its following slot", op["warning"])
        self.assertEqual((op["create_args"]["schedule"], op["original_slot"]), (moved, T11))
        self.assertEqual(op["occurrence"], P.occurrence_key("j1", T11))
        recs = {op["occurrence"]: {"source_job_id": "j1", "child_id": "c1", "status": "pending",
                                   "fingerprint": op["fingerprint"], "moved_start": moved,
                                   "instance_backed": True}}
        child = job(id="c1", schedule_kind="once", start=moved, schedule=moved)
        self.assertEqual(B.plan([rjob(), child], [master(), inst(moved, T11)], rstate(), recs), [])

    def test_past_exception_is_history(self):
        self.assertEqual(B.plan([rjob(start=T11)], [master(), inst(T10, T9)], rstate()), [])

    # 2. "All events"
    def test_all_events_time_change_updates_cron(self):
        ops = B.plan([rjob()], [master(start=T10)], rstate())
        self.assertEqual([(o["action"], o.get("updates")) for o in ops], [("update_job", {"schedule": "0 10 * * *"})])

    def test_all_events_weekday_change_updates_cron(self):
        ops = B.plan([rjob()], [master(recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"])], rstate())
        self.assertEqual(ops[0]["updates"], {"schedule": "0 9 * * 1,3"})

    def test_all_events_non_convertible_refused_nothing_changes(self):
        ops = B.plan([rjob()], [master(recurrence=["RRULE:FREQ=DAILY;INTERVAL=2"])], rstate())
        self.assertEqual(acts(ops), ["notice"])
        self.assertIn("nothing changed", ops[0]["reason"])

    def test_all_events_timezone_change_refused(self):
        ops = B.plan([rjob()], [master(tz="Europe/London")], rstate())
        self.assertEqual(acts(ops), ["notice"])

    # 3. "This and following"
    def _split(self, start, rule="RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", **kw):
        return master(**{"id": "m1_R20261110T000000", "start": start, "recurrence": [rule], "hermes_job_id": None, **kw})

    def test_split_adopts_new_series_deferred_until_old_ends(self):
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261109T125959Z"])
        new = self._split("2026-11-10T09:00:00+11:00")
        ops = B.plan([rjob(start=T11)], [old, new], rstate())
        self.assertEqual(acts(ops), ["adopt_split"])
        op = ops[0]
        self.assertEqual((op["series_id"], op["old_id"], op["cron"], op["cron_now"]),
                         (new["id"], "m1", "0 9 * * 1,2,3,4,5", False))
        self.assertNotIn("start", op["event"])
        self.assertNotIn("recurrence", op["event"])  # Google's own series kept; only our tags added
        self.assertFalse({"insert_event", "delete_event", "create_once"} & set(acts(ops)))  # no dup, no orphan

    def test_split_after_old_series_ended_updates_cron_now(self):
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261108T125959Z"])  # last run Sun 8 Nov
        later = "2026-11-10T09:00:00+11:00"
        ops = B.plan([rjob(start=later)], [old, self._split("2026-11-09T09:00:00+11:00")], rstate())
        self.assertEqual((ops[0]["action"], ops[0]["cron"]), ("adopt_split", "0 9 * * 1,2,3,4,5"))
        self.assertTrue(ops[0]["cron_now"])

    def test_split_same_start_rule_only(self):
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261106T125959Z"])
        new = self._split(T9, rule="RRULE:FREQ=WEEKLY;BYDAY=SA")
        ops = B.plan([rjob()], [old, new], rstate())
        self.assertEqual((ops[0]["action"], ops[0]["cron"]), ("adopt_split", "0 9 * * 6"))

    def test_split_tagged_copy_also_detected(self):
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261106T125959Z"])
        new = master(id="othername", start=T9)  # continues Sat 7 Nov, the day after the old series ends
        ops = B.plan([rjob(start=T11)], [old, new], rstate())
        self.assertEqual((ops[0]["action"], ops[0]["cron"]), ("adopt_split", None))

    def test_after_adoption_old_series_is_history_no_second_job(self):
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261109T125959Z"])
        new = self._split("2026-11-10T09:00:00+11:00", hermes_job_id="j1")
        st = rstate(event_id=new["id"], history_ids=["m1"],
                    split_pending={"cron": "0 9 * * 1,2,3,4,5", "until": "2026-11-09T12:59:59+00:00"})
        self.assertEqual(B.plan([rjob(start=T11)], [old, new], st), [])  # waits, no all-events change
        later = "2026-11-10T09:00:00+11:00"
        ops = B.plan([rjob(start=later)], [old, new], st)
        self.assertEqual([(o["action"], o["updates"]) for o in ops], [("apply_split", {"schedule": "0 9 * * 1,2,3,4,5"})])
        done = rstate(rjob(schedule="0 9 * * 1-5"), event_id=new["id"], history_ids=["m1"])
        weekday = rjob(schedule="0 9 * * 1-5", cron_expr="0 9 * * 1-5", start=later,
                       rrule="RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
        self.assertEqual(B.plan([weekday], [old, new], done), [])

    def test_until_without_follow_on_series_refused(self):
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261109T125959Z"])
        ops = B.plan([rjob()], [old], rstate())
        self.assertEqual(acts(ops), ["notice"])

    def test_split_unrelated_untagged_series_ignored(self):
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261109T125959Z"])
        stray = self._split(T11)
        stray["id"] = "zzz_R20261110"
        self.assertEqual(acts(B.plan([rjob()], [old, stray], rstate())), ["notice"])

    # 4. Deleted single instance
    def test_deleted_instance_plans_skip_instance(self):
        gone = inst(None, T11, status="cancelled", hermes_job_id=None)
        ops = B.plan([rjob()], [master(), gone], rstate())
        self.assertEqual(acts(ops), ["skip_instance"])
        self.assertEqual(ops[0]["original_slot"], T11)

    def test_deleting_moved_instance_pauses_pending_copy(self):
        key = P.occurrence_key("j1", T11)
        recs = {key: {"source_job_id": "j1", "child_id": "c1", "status": "pending", "instance_backed": True,
                      "instance_event_id": "m1_20261108", "fingerprint": P.fingerprint(rjob())}}
        gone = inst(None, T11, status="cancelled", hermes_job_id=None)
        child = job(id="c1", schedule_kind="once", start=T10)
        ops = B.plan([rjob(), child], [master(), gone], rstate(), recs)
        self.assertEqual([(o["action"], o["job_id"]) for o in ops], [("pause_job", "c1")])


class FallbackTest(unittest.TestCase):
    def test_single_mode_converts_master_back(self):
        ops = B.plan([rjob()], [master()], rstate(), recurring=False)
        self.assertEqual(acts(ops), ["convert_to_single"])
        self.assertEqual((ops[0]["event"]["recurrence"], ops[0]["event"]["start"]), ([], T9))

    def test_single_mode_matches_planner_exactly(self):
        for events, st in [([], {}), ([ev()], synced()), ([ev(start=T10)], synced()), ([], synced())]:
            self.assertEqual(B.plan([rjob()], events, st, recurring=False), P.plan([rjob()], events, st))

    def test_non_convertible_and_once_jobs_stay_single(self):
        j = rjob(rrule=None, rrule_refusal="DST")
        self.assertEqual(B.plan([j], [ev()], synced()), P.plan([j], [ev()], synced()))
        once = job(schedule_kind="once", rrule=None)
        self.assertEqual(B.plan([once], [], {}), P.plan([once], [], {}))


class RecurringRuntimeTest(TR.RuntimeTest):
    """Inherits the fixtures; only the tests below run under this class name."""

    def test_mode_switch_and_sync_reports_mode(self):
        self.assertEqual(M.mode()["event_mode"], "recurring")
        self.assertEqual(M.mode("single")["event_mode"], "single")
        self.jobs = [rjob()]
        self.events = [master()]
        M._save_state({**M._load_state(), "jobs": rstate()})
        res = M.sync()
        self.assertEqual((res["event_mode"], acts(res["ops"]), res["writes"]), ("single", ["convert_to_single"], 0))
        self.cal.patch.assert_not_called()

    def test_deleted_instance_without_flag_is_notice(self):
        self.jobs = [rjob()]
        self.events = [master(), inst(None, T11, status="cancelled", hermes_job_id=None)]
        M._save_state({"calendar_id": "test-calendar", "jobs": rstate()})
        res = M.sync()
        self.assertEqual((res["ops"], res["pending_ops"]), ([], 0))  # informational, not a pending op
        self.assertIn("can't skip a single run", res["info"][0])
        self.assertIn("nothing changed", res["info"][0])
        self.assertEqual(M.sync()["info"], res["info"])  # dry-runs keep showing it (no state writes)
        done = M.sync(confirm=True)
        self.assertEqual((done["writes"], done["errors"]), (0, []))
        self.dispatch.assert_not_called()
        self.cal.patch.assert_not_called()
        again = M.sync()
        self.assertEqual((again["ops"], again["info"]), ([], []))  # recorded once, shown once

    def test_noticed_instance_can_still_be_skipped_with_flag(self):
        slot = "2030-01-02T09:00:00+11:00"
        self.jobs = [rjob(start=slot)]
        self.raw = [{"id": "j1", "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "enabled": True,
                     "next_run_at": slot}]
        self.events = [master(), inst(None, slot, status="cancelled", hermes_job_id=None)]
        key = P.occurrence_key("j1", slot)
        M._save_state({"calendar_id": "test-calendar", "jobs": rstate(),
                       "occurrences": {key: {"key": key, "source_job_id": "j1", "status": "noticed", "mode": "notice"}}})
        self.assertEqual(M.sync()["info"], [])
        self.assertEqual(acts(M.sync(skip_original="experimental")["ops"]), ["skip_instance"])

    def test_deleted_instance_with_flag_routes_to_experimental_skip(self):
        slot = "2030-01-02T09:00:00+11:00"
        self.jobs = [rjob(start=slot)]
        self.raw = [{"id": "j1", "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "enabled": True,
                     "next_run_at": slot}]
        self.events = [master(), inst(None, slot, status="cancelled", hermes_job_id=None)]
        M._save_state({"calendar_id": "test-calendar", "jobs": rstate()})
        res = M.sync(skip_original="experimental")
        self.assertEqual((acts(res["ops"]), res["ops"][0]["mode"]), (["skip_instance"], "skip"))

    def test_single_mode_survives_a_confirmed_sync(self):
        M.mode("single")
        self.jobs = [rjob()]
        self.events = [master()]
        M._save_state({**M._load_state(), "jobs": rstate()})

        def patched(event_id, body):  # Google now holds the converted single event
            self.events = [master(recurrence=None)]
            return ev()
        self.cal.patch.side_effect = patched
        done = M.sync(confirm=True)
        self.assertEqual((acts(done["ops"]), done["errors"]), (["convert_to_single"], []))
        self.assertEqual(M._load_state()["event_mode"], "single")  # the snapshot kept it
        again = M.sync()
        self.assertEqual(again["event_mode"], "single")
        self.assertNotIn("convert_to_recurring", acts(again["ops"]))

    def test_adopt_split_apply_tags_series_records_history(self):
        op = {"direction": "calendar->cron", "action": "adopt_split", "job_id": "j1", "reason": "x",
              "series_id": "m1_R1", "old_id": "m1", "event": {"summary": "Daily brief", "hermes_job_id": "j1",
                                                               "props": {}}, "cron": "0 9 * * 1,2,3,4,5",
              "cron_now": True, "until": None}
        self.assertEqual(M._apply([op], "test-calendar"), (1, []))
        self.assertEqual(self.cal.patch.call_args.args[0], "m1_R1")
        self.assertEqual(self.dispatch.call_args.args[1], {"action": "update", "job_id": "j1", "schedule": "0 9 * * 1,2,3,4,5"})
        st = M._load_state()["jobs"]["j1"]
        self.assertEqual((st["event_id"], st["history_ids"]), ("m1_R1", ["m1"]))
        self.cal.delete.assert_not_called()  # old series kept as history

    def test_instance_backed_copy_gets_no_event_of_its_own(self):
        moved = "2026-11-08T11:00:00+11:00"
        self.jobs = [rjob()]
        child = job(id="c1", schedule_kind="once", start=moved, schedule=moved)
        self.dispatch.side_effect = lambda *_: (self.jobs.append(child) or json.dumps({"success": True, "job_id": "c1"}))
        ops = B.plan(self.jobs, [master(), inst(moved, T11)], rstate())
        self.assertEqual(M._apply(ops, "test-calendar"), (1, []))
        self.cal.patch.assert_not_called()
        self.cal.insert.assert_not_called()
        st = M._load_state()
        self.assertTrue(st["jobs"]["c1"]["instance_backed"])
        self.assertEqual(B.plan(self.jobs, [master(), inst(moved, T11)], rstate(), st["occurrences"]), [])


for _name in list(vars(TR.RuntimeTest)):
    if _name.startswith("test_"):
        setattr(RecurringRuntimeTest, _name, None)


class GcalMappingTest(unittest.TestCase):
    def test_from_google_keeps_series_fields(self):
        g = {"id": "m1_20261108T000000Z", "status": "cancelled", "recurringEventId": "m1",
             "originalStartTime": {"dateTime": T11}}
        e = G.from_google(g)
        self.assertEqual((e["recurring_event_id"], e["original_start"], e["status"], e["hermes_job_id"]),
                         ("m1", T11, "cancelled", None))
        m = G.from_google({"id": "m1", "recurrence": [DAILY], "start": {"dateTime": T9, "timeZone": SYD},
                           "extendedProperties": {"private": {G.TAG: "j1"}}})
        self.assertEqual((m["recurrence"], m["tz"], m["hermes_job_id"]), ([DAILY], SYD, "j1"))

    def test_to_google_empty_recurrence_clears_series(self):
        self.assertEqual(G.to_google({"start": T9, "recurrence": []})["recurrence"], [])
        self.assertNotIn("recurrence", G.to_google({"start": T9}))


if __name__ == "__main__":
    unittest.main()
