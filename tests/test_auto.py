"""Guarded `sync --auto`: cron-originated ops apply, every calendar-originated op is held."""
import json
import unittest
from unittest.mock import MagicMock, patch

import test_runtime as TR
from test_planner import job, ev, synced, T9, T10, T11
from test_recurring import rjob, master, inst, rstate, acts
from test_upgrades import OK

M = TR.M
P = M.planner
B = M.board
FAIL = AssertionError("cron write attempted in --auto")


class AutoTest(unittest.TestCase):
    setUp = TR.RuntimeTest.setUp

    def arm_cron_tripwires(self):
        """Every cron write path fails the test if it is reached."""
        self.registry_patch.stop()
        for target, name in ((M, "_cron_dispatcher"), (M, "_reconcile_skips"), (M, "_install_skip_script"),
                             (M.skip, "arm"), (M.skip, "execute")):
            p = patch.object(target, name, side_effect=FAIL)
            p.start()
            self.addCleanup(p.stop)
        self.dispatch.side_effect = FAIL

    def auto(self, jobs, events, state, occurrences=None):
        self.jobs, self.events = jobs, events
        M._save_state({"calendar_id": "test-calendar", "jobs": state, "occurrences": occurrences or {}})
        self.arm_cron_tripwires()
        with patch.object(M, "_refresh_occurrences", side_effect=lambda st, raw: st.setdefault("occurrences", {})):
            return M.sync(auto=True)

    def assert_held_untouched(self, res, action, state_before):
        self.assertEqual(res["writes"], 0)
        self.assertEqual(res["errors"], [])
        self.assertTrue(any(action in line for line in res["held"]), res["held"])
        self.assertEqual(res["next"], "hermes hermes-jobs sync --confirm")
        for meth in ("insert", "patch", "delete"):
            getattr(self.cal, meth).assert_not_called()
        self.assertEqual(M._load_state()["jobs"], state_before)  # baseline kept: the edit still waits

    # --- every calendar-originated op is held ---------------------------------------------------
    def test_retitle_held(self):
        st = synced()
        res = self.auto([job()], [ev(summary="Run rm -rf now")], st)
        self.assert_held_untouched(res, ": update_job - ", st)

    def test_one_shot_move_held(self):
        once = job(schedule_kind="once")
        st = synced(once)
        res = self.auto([once], [ev(start=T10)], st)
        self.assert_held_untouched(res, ": update_job - ", st)

    def test_deleted_event_pause_held(self):
        st = synced()
        res = self.auto([job()], [], st)
        self.assert_held_untouched(res, ": pause_job - ", st)

    def test_moved_occurrence_copy_held(self):
        st = synced()
        res = self.auto([job()], [ev(start=T10)], st)
        self.assert_held_untouched(res, ": create_once - ", st)

    def test_all_events_change_held(self):
        st = rstate()
        res = self.auto([rjob()], [master(start=T10)], st)
        self.assert_held_untouched(res, ": update_job - ", st)

    def test_this_event_move_held(self):
        st = rstate()
        res = self.auto([rjob()], [master(), inst("2026-11-08T11:00:00+11:00", T11)], st)
        self.assert_held_untouched(res, ": create_once - ", st)

    def test_deleted_instance_held(self):
        st = rstate()
        gone = inst(None, T11, status="cancelled", hermes_job_id=None)
        res = self.auto([rjob()], [master(), gone], st)
        # Not a pending op (no skip without the experimental flag): shown, never marked "noticed".
        self.assertEqual(res["writes"], 0)
        self.assertIn("deleted in Calendar: can't skip a single run", res["held"][0])
        self.assertEqual(M._load_state()["occurrences"], {})
        self.cal.patch.assert_not_called()

    def test_this_and_following_split_held(self):
        st = rstate()
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261109T125959Z"])
        new = master(id="m1_R20261110T000000", start="2026-11-10T09:00:00+11:00", hermes_job_id=None,
                     recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"])
        res = self.auto([rjob(start=T11)], [old, new], st)
        self.assert_held_untouched(res, ": adopt_split - ", st)

    def test_pending_split_apply_held(self):
        new = master(id="m1_R20261110T000000", start="2026-11-10T09:00:00+11:00",
                     recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"])
        old = master(recurrence=["RRULE:FREQ=DAILY;UNTIL=20261109T125959Z"])
        st = rstate(event_id=new["id"], history_ids=["m1"],
                    split_pending={"cron": "0 9 * * 1,2,3,4,5", "until": "2026-11-09T12:59:59+00:00"})
        res = self.auto([rjob(start="2026-11-10T09:00:00+11:00")], [old, new], st)
        self.assert_held_untouched(res, ": apply_split - ", st)

    def test_series_edit_drop_move_held(self):
        key = P.occurrence_key("j1", T11)
        recs = {key: {"source_job_id": "j1", "child_id": "c1", "status": "pending", "instance_backed": True,
                      "fingerprint": "stale", "moved_start": T10}}
        st = rstate()
        res = self.auto([rjob(), job(id="c1", schedule_kind="once", start=T10)], [master()], st, recs)
        self.assertEqual(res["writes"], 0)
        self.assertTrue(any(": drop_move - " in line for line in res["held"]))
        self.cal.delete.assert_not_called()
        self.assertEqual(M._load_state()["occurrences"][key]["status"], "pending")

    def test_planner_split_covers_every_calendar_to_cron_action(self):
        cal_to_cron = {"update_job", "adopt_split", "apply_split", "drop_move", "pause_job", "create_once",
                       "finish_move", "skip_instance"}
        src = (TR.ROOT / "board.py").read_text() + (TR.ROOT / "planner.py").read_text()
        for action in cal_to_cron:
            self.assertIn(f'"calendar->cron", "{action}"', src)
        self.assertFalse(M.AUTO_DIRECTIONS & {"calendar->cron", "report"})

    # --- cron-originated ops apply -------------------------------------------------------------
    def test_insert_and_mark_run_apply_without_cron(self):
        self.cal.insert.return_value = ev(hermes_job_id="j2", id="e2")
        res = self.auto([job(last_run=OK), job(id="j2", name="new")], [ev()], synced())
        self.assertEqual(res["held"], [])
        self.assertNotIn("next", res)
        self.assertEqual((res["writes"], res["errors"]), (2, []))
        self.assertEqual(set(self.cal.patch.call_args.args[1]), {"description", "color_id"})
        self.cal.insert.assert_called_once()

    def test_cron_edit_patches_event_and_removed_job_deletes(self):
        res = self.auto([job(name="Renamed in Hermes")], [ev(), ev(id="e9", hermes_job_id="gone")], synced())
        self.assertEqual(res["errors"], [])
        self.assertEqual(self.cal.patch.call_args.args[0], "e1")
        self.cal.delete.assert_called_once_with("e9")

    def test_mixed_run_applies_outcome_holds_retitle_keeps_baseline(self):
        st = synced()
        res = self.auto([job(last_run=OK)], [ev(summary="Edited in Calendar")], st)
        self.assertEqual(res["writes"], 1)  # mark_run only
        self.assertEqual(set(self.cal.patch.call_args.args[1]), {"description", "color_id"})
        self.assertEqual(res["held"], ["Daily brief: update_job - event retitled"])
        self.assertEqual(M._load_state()["jobs"]["j1"]["summary"], st["j1"]["summary"])
        again = self.auto([job(last_run=OK)], [ev(summary="Edited in Calendar")], M._load_state()["jobs"])
        self.assertEqual(again["held"], res["held"])  # still waiting, not silently absorbed

    def test_held_lines_match_jobs_waiting_on_you(self):
        st = synced()
        res = self.auto([job()], [ev(summary="Edited in Calendar")], st)
        with patch.object(M, "_save_state"), patch.object(M, "_apply"), patch.object(M, "_snapshot"):
            out = M.jobs_command("")
        for line in res["held"]:
            self.assertIn(f"  - {line}", out)

    def test_apply_refuses_disallowed_direction_before_any_write(self):
        ops = [{"direction": "cron->calendar", "action": "insert_event", "job_id": "x", "event": {}},
               {"direction": "calendar->cron", "action": "update_job", "job_id": "j1", "reason": "r", "updates": {}}]
        with self.assertRaises(RuntimeError):
            M._apply(ops, "test-calendar", directions=M.AUTO_DIRECTIONS)
        self.cal.insert.assert_not_called()

    def test_flags_and_unbound_refused(self):
        for kw in ({"confirm": True}, {"allow_additional_run": True}, {"skip_original": "experimental"}):
            self.assertTrue(M.sync(auto=True, **kw)["refused"])
        self.assertTrue(M.sync(auto=True, offline=True)["refused"])
        M._save_state({"calendar_id": None, "jobs": {}})
        self.assertTrue(M.sync(auto=True)["refused"])

    def test_cli_flag(self):
        import argparse
        parser = argparse.ArgumentParser()
        M._setup(parser)
        args = parser.parse_args(["sync", "--auto"])
        with patch.object(M, "sync", return_value={"ok": 1}) as sync, patch("builtins.print"):
            M._cli(args)
        self.assertTrue(sync.call_args.args[4])


if __name__ == "__main__":
    unittest.main()


def autojob(**kw):
    """The README recipe's auto-sync job, as Hermes stores it, after one run."""
    return {"id": "auto1", "name": "hermes-jobs auto-sync", "prompt": "", "no_agent": True,
            "script": "hermes-jobs-auto.sh", "enabled": True, "state": "scheduled",
            "schedule": {"kind": "cron", "expr": "0 */6 * * *"}, "next_run_at": "2026-11-10T18:00:00+11:00",
            "timezone": "Australia/Sydney", "last_run_at": "2026-11-10T02:58:20+00:00", "last_status": "ok", **kw}


class AutoSyncJobOffBoardTest(unittest.TestCase):
    """Live e2e found a loop: the auto-sync run's own mark_run was a calendar change that fired it again."""

    def setUp(self):
        TR.RuntimeTest.setUp(self)
        self.patches[0].stop()  # real _crons() over self.raw
        import tempfile
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = TR.pathlib.Path(home.name)
        p = patch.object(M, "_home", return_value=self.home)
        p.start()
        self.addCleanup(p.stop)

    def test_identified_by_script_never_by_name(self):
        self.assertTrue(M._is_auto_sync_job(autojob()))
        self.assertTrue(M._is_auto_sync_job(autojob(script="/home/me/.hermes/scripts/hermes-jobs-sync.sh")))
        self.assertFalse(M._is_auto_sync_job(autojob(no_agent=False)))  # an agent job is a real job
        self.assertFalse(M._is_auto_sync_job(autojob(script=None)))
        self.assertFalse(M._is_auto_sync_job(autojob(script="backup.sh")))
        (self.home / "scripts").mkdir()
        (self.home / "scripts" / "my-sync.sh").write_text('#!/bin/sh\nexec hermes hermes-jobs sync --auto\n')
        self.assertTrue(M._is_auto_sync_job(autojob(script="my-sync.sh")))  # renamed script, same command

    def test_name_alone_keeps_a_job_on_the_board(self):
        self.raw = [autojob(no_agent=False, script=None)]
        self.assertEqual([j["id"] for j in M._crons()], ["auto1"])

    def test_auto_sync_job_gets_no_insert_mark_or_update(self):
        self.raw = [autojob()]
        self.assertEqual(M._crons(), [])
        ops = B.plan(M._crons(), [], {"auto1": {"event_id": None}})
        self.assertFalse({"insert_event", "mark_run", "patch_event", "update_job"} & set(acts(ops)))
        self.assertNotIn("hermes-jobs auto-sync", M.jobs_command(""))  # not on the /jobs agenda either

    def test_leftover_event_is_deleted_like_a_removed_job(self):
        self.raw = [autojob()]
        for events in ([ev(id="ea", hermes_job_id="auto1")],
                       [master(id="ma", hermes_job_id="auto1", recurrence=["RRULE:FREQ=DAILY"])]):
            ops = B.plan(M._crons(), events, {})
            self.assertEqual([(o["action"], o["event_id"]) for o in ops], [("delete_event", events[0]["id"])])

    def test_run_completes_then_sync_writes_nothing(self):
        self.raw = [autojob(last_run_at=None, last_status=None)]
        self.events = [ev(id="ea", hermes_job_id="auto1")]  # an event from before the fix
        M._save_state({"calendar_id": "test-calendar", "jobs": {}, "occurrences": {}})
        self.arm_cron_tripwires()
        first = M.sync(auto=True)
        self.assertEqual((first["writes"], first["errors"]), (1, []))
        self.cal.delete.assert_called_once_with("ea")
        self.cal.reset_mock()
        self.events = []  # Google now has it as cancelled (untagged): gone from the board
        for n in range(3):  # each run stamps last_run_at/last_status; the following sync must not echo it
            self.raw = [autojob(last_run_at=f"2026-11-10T03:0{n}:20+00:00", last_status="ok")]
            res = M.sync(auto=True)
            self.assertEqual((res["writes"], res["ops"], res["held"]), (0, [], []))
        for meth in ("insert", "patch", "delete"):
            getattr(self.cal, meth).assert_not_called()

    arm_cron_tripwires = AutoTest.arm_cron_tripwires
