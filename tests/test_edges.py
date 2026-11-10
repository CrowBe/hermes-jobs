"""Edges of the four recurring edits (time zones/DST, cleared exceptions, foreign series, retitle,
all-day conversions) and review hardening (dispatch gate on splits, wake-job ownership on rebuild).
Every assertion states what the code does today; nothing here is a wish."""
import importlib
import json
import unittest

import test_runtime as TR
from test_planner import job, ev, synced, T9, T10, T11
from test_recurring import rjob, master, inst, rstate, acts

M = TR.M
P = M.planner
B = importlib.import_module(M.__name__ + ".board")
R = importlib.import_module(M.__name__ + ".rrule")
S = importlib.import_module(M.__name__ + ".skip")
MOVED = "2026-11-08T11:00:00+11:00"


class AllEventsEdgeTest(unittest.TestCase):
    def test_utc_expressed_start_is_not_a_series_change(self):
        # 2026-11-06T22:00Z is 09:00 Sydney: same wall time, same cron, no op.
        self.assertEqual(B.plan([rjob()], [master(start="2026-11-06T22:00:00+00:00")], rstate()), [])

    def test_same_wall_time_after_dst_end_is_not_a_series_change(self):
        apr = "2026-04-06T09:00:00+10:00"  # Sydney standard time; the master was created at +11:00
        self.assertEqual(B.plan([rjob(start=apr)], [master(start=apr)], rstate()), [])

    def test_time_and_title_change_together_give_two_name_or_schedule_updates(self):
        ops = B.plan([rjob()], [master(start=T10, summary="Morning brief")], rstate())
        self.assertEqual([(o["action"], o["updates"]) for o in ops],
                         [("update_job", {"schedule": "0 10 * * *"}), ("update_job", {"name": "Morning brief"})])

    def test_all_events_edit_clears_exceptions_then_pending_copy_is_dropped(self):
        key = P.occurrence_key("j1", T11)
        recs = {key: {"source_job_id": "j1", "child_id": "c1", "status": "pending", "instance_backed": True,
                      "fingerprint": P.fingerprint(rjob()), "moved_start": MOVED}}
        child = job(id="c1", schedule_kind="once", start=MOVED, schedule=MOVED)
        # Google dropped the moved instance with the series edit: only the master is left, at 10:00.
        ops = B.plan([rjob(), child], [master(start=T10)], rstate(), recs)
        self.assertEqual([(o["action"], o.get("updates")) for o in ops], [("update_job", {"schedule": "0 10 * * *"})])
        # Next sync: cron now reads 0 10 * * *, the copy's fingerprint no longer matches -> dropped.
        moved = rjob(schedule="0 10 * * *", cron_expr="0 10 * * *", start=T10)
        ops = B.plan([moved, child], [master(start=T10)], rstate(), recs)
        self.assertEqual(acts(ops), ["drop_move", "record"])
        self.assertEqual(ops[0]["child_id"], "c1")

    def test_all_events_to_dst_transition_time_follows_cron_then_master_goes_single(self):
        # 02:00 Sydney is a documented refusal for cron -> RRULE; the reverse direction accepts it,
        # so cron follows first and the master is converted to a single event on the next sync.
        two = "2026-11-07T02:00:00+11:00"
        ops = B.plan([rjob()], [master(start=two)], rstate())
        self.assertEqual([(o["action"], o["updates"]) for o in ops], [("update_job", {"schedule": "0 2 * * *"})])
        j = rjob(schedule="0 2 * * *", cron_expr="0 2 * * *", start=two, rrule=None, rrule_refusal="DST")
        self.assertEqual(acts(B.plan([j], [master(start=two)], rstate())), ["convert_to_single"])

    def test_all_day_master_is_refused_not_a_crash(self):
        ops = B.plan([rjob()], [master(start=None)], rstate())
        self.assertEqual(acts(ops), ["notice"])
        self.assertIn("no timed start", ops[0]["reason"])


class ThisEventEdgeTest(unittest.TestCase):
    def test_moved_and_cancelled_instances_of_a_foreign_master_are_ignored(self):
        stray = master(id="zzz", hermes_job_id=None)
        moved = inst(MOVED, T11, recurring_event_id="zzz", hermes_job_id=None)
        gone = inst(None, T11, recurring_event_id="zzz", hermes_job_id=None, status="cancelled")
        self.assertEqual(B.plan([rjob()], [master(), stray, moved, gone], rstate()), [])

    def test_moved_instance_while_job_paused_is_recorded_only(self):
        paused = rjob(enabled=False)
        ops = B.plan([paused], [master(summary=P.title(paused)), inst(MOVED, T11)], rstate(paused))
        self.assertEqual(acts(ops), ["record"])

    def test_past_cancelled_instance_is_history(self):
        gone = inst(None, T9, status="cancelled", hermes_job_id=None)
        self.assertEqual(B.plan([rjob(start=T11)], [master(), gone], rstate()), [])

    def test_moved_instance_without_timed_start_is_noticed_not_copied(self):
        ops = B.plan([rjob()], [master(), inst(None, T11)], rstate())
        self.assertEqual(acts(ops), ["notice"])
        self.assertIn("no timed start", ops[0]["reason"])


class ThisAndFollowingEdgeTest(unittest.TestCase):
    OLD = ["RRULE:FREQ=DAILY;UNTIL=20261106T125959Z"]

    def _new(self, **kw):
        base = {"id": "m1_R20261108T000000", "start": T11, "hermes_job_id": None}
        return master(**{**base, **kw})

    def test_split_in_another_time_zone_is_refused(self):
        ops = B.plan([rjob(start=T11)], [master(recurrence=self.OLD), self._new(tz="Europe/London")], rstate())
        self.assertEqual(acts(ops), ["notice"])
        self.assertIn("time zone", ops[0]["reason"])

    def test_split_with_non_convertible_rule_is_not_adopted(self):
        new = self._new(recurrence=["RRULE:FREQ=DAILY;INTERVAL=2"])
        ops = B.plan([rjob(start=T11)], [master(recurrence=self.OLD), new], rstate())
        self.assertEqual(acts(ops), ["notice"])
        self.assertIn("series not adopted", ops[0]["reason"])

    def test_all_day_split_is_refused_not_a_crash(self):
        ops = B.plan([rjob(start=T11)], [master(recurrence=self.OLD), self._new(start=None)], rstate())
        self.assertEqual(acts(ops), ["notice"])

    def test_malformed_until_reads_as_no_end(self):
        self.assertIsNone(R.until_instant(["RRULE:FREQ=DAILY;UNTIL=garbage"]))
        self.assertIsNone(R.until_instant(["RRULE:FREQ=DAILY"]))


class RetitleAndDeleteTest(unittest.TestCase):
    def test_master_retitle_updates_name_only(self):
        ops = B.plan([rjob()], [master(summary="Morning brief")], rstate())
        self.assertEqual([(o["action"], o["updates"]) for o in ops], [("update_job", {"name": "Morning brief"})])

    def test_paused_prefix_is_stripped_from_a_retitle(self):
        paused = rjob(enabled=False)
        ops = B.plan([paused], [master(summary="[paused] Morning brief")], rstate(paused))
        self.assertEqual(ops[0]["updates"], {"name": "Morning brief"})

    def test_retitle_to_nothing_but_format_characters_changes_nothing(self):
        self.assertEqual(B.plan([rjob()], [master(summary="​‮")], rstate()), [])

    def test_single_event_made_all_day_is_restored_by_cron(self):
        ops = P.plan([job()], [ev(start=None)], synced())
        self.assertEqual([(o["action"], o["event"]["start"]) for o in ops], [("patch_event", T9)])


class SkipRebuildOwnershipTest(unittest.TestCase):
    def test_rebuild_claims_wake_jobs_only_when_they_run_our_script(self):
        key = "a" * 32
        recs = {}
        name = S.WAKE_PREFIX + key[:8] + " #1"  # a Calendar retitle can give any job this name
        raws = [{"id": "src", "enabled": False, "state": "paused", "paused_reason": S.marker(key, T11, None, None)},
                {"id": "victim", "name": name, "schedule": {"kind": "cron", "expr": "0 9 * * *"}},
                {"id": "wake", "name": name, "script": S.SCRIPT_NAME, "no_agent": True}]
        S.rebuild(raws, recs, lambda r: "fp")
        self.assertEqual(recs[key]["wake_ids"], ["wake"])


class SplitRuntimeTest(TR.RuntimeTest):
    """Inherits the runtime fixtures; only the tests below run under this class name."""

    def _adopt(self, **kw):
        return {"direction": "calendar->cron", "action": "adopt_split", "job_id": "j1", "reason": "x",
                "series_id": "m1_R1", "old_id": "m1", "event": {"summary": "Daily brief", "hermes_job_id": "j1",
                                                                 "props": {}}, **kw}

    def test_deferred_split_records_split_pending_and_touches_no_cron(self):
        until = "2026-11-09T12:59:59+00:00"
        op = self._adopt(cron="0 9 * * 1,2,3,4,5", cron_now=False, until=until)
        self.assertEqual(M._apply([op], "test-calendar"), (1, []))
        self.assertEqual(self.cal.patch.call_args.args[0], "m1_R1")
        self.dispatch.assert_not_called()
        st = M._load_state()["jobs"]["j1"]
        self.assertEqual(st["split_pending"], {"cron": "0 9 * * 1,2,3,4,5", "until": until})
        self.assertEqual((st["event_id"], st["history_ids"]), ("m1_R1", ["m1"]))

    def test_split_schedules_pass_the_dispatch_gate(self):
        bad = self._adopt(cron="0 9 * * 1 ; rm -rf /", cron_now=True, until=None)
        writes, errors = M._apply([bad], "test-calendar")
        self.assertEqual((writes, len(errors)), (0, 1))
        self.cal.patch.assert_not_called()  # refused before any write
        self.dispatch.assert_not_called()
        apply = {"direction": "calendar->cron", "action": "apply_split", "job_id": "j1", "reason": "x",
                 "updates": {"schedule": "$(id)", "prompt": "evil"}}
        self.assertEqual(len(M._apply([apply], "test-calendar")[1]), 1)
        self.dispatch.assert_not_called()
        good = {**apply, "updates": {"schedule": "0 9 * * 1,2,3,4,5", "prompt": "evil"}}
        self.assertEqual(M._apply([good], "test-calendar"), (1, []))
        self.assertEqual(self.dispatch.call_args.args[1], {"action": "update", "job_id": "j1", "schedule": "0 9 * * 1,2,3,4,5"})


for _name in list(vars(TR.RuntimeTest)):
    if _name.startswith("test_"):
        setattr(SplitRuntimeTest, _name, None)


if __name__ == "__main__":
    unittest.main()
