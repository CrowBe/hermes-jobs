"""v0.3.2 review hardening: copy provenance (never adopt or delete a job we cannot prove we
created), the per-profile sync lock, and the experimental skip's own catch_up override."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_runtime as TR
from test_runtime import M
from test_planner import job, ev, synced, T9, T10
from test_ids_skip import raw, SLOT, NOW

P, S = M.planner, M.skip
MOVED = "2026-11-10T11:00:00+11:00"


class ProvenanceTest(TR.RuntimeTest):
    """Inherits the runtime fixtures; only the tests below run under this class name."""

    def lookalike(self, jid, name):
        return {"id": jid, "name": name, "state": "scheduled", "prompt": "summarise",
                "schedule": {"kind": "once", "run_at": T10}, "next_run_at": T10}

    def plan(self):
        return M.planner.plan(self.jobs, [ev(start=T10)], M._load_state()["jobs"])

    def create_lands(self, jid="mine"):
        def dispatch(name, args):
            if args["action"] == "create":
                self.raw.append(self.lookalike(jid, args["name"]))
                self.jobs.append(job(id=jid, name=args["name"], schedule_kind="once", start=T10))
                return json.dumps({"success": True, "job_id": jid})
            return json.dumps({"success": True})
        self.dispatch.side_effect = dispatch

    def test_unrelated_same_name_and_time_job_is_never_adopted_or_removed(self):
        ops = self.plan()
        name = ops[0]["create_args"]["name"]
        self.raw = [self.lookalike("victim", name)]  # someone else's job, same name, instant and prompt
        self.create_lands()
        self.assertFalse(M._apply(ops, "test-calendar")[1])
        rec = M._load_state()["occurrences"][ops[0]["occurrence"]]
        self.assertEqual((rec["child_id"], rec["child_owned"], rec["preexisting"]), ("mine", True, ["victim"]))
        self.dispatch.reset_mock()
        drop = {"direction": "calendar->cron", "action": "drop_move", "job_id": "j1", "reason": "r",
                "occurrence": ops[0]["occurrence"], "child_id": "mine", "event_id": "e1"}
        self.assertEqual(M._apply([drop], "test-calendar")[1], [])
        removed = [c.args[1]["job_id"] for c in self.dispatch.call_args_list if c.args[1]["action"] == "remove"]
        self.assertEqual(removed, ["mine"])  # the victim survives

    def test_retry_adopts_only_the_job_that_appeared_since_the_journal(self):
        ops = self.plan()
        name = ops[0]["create_args"]["name"]
        self.raw = [self.lookalike("victim", name)]
        self.dispatch.return_value = "not JSON"  # create landed, result lost
        self.assertTrue(M._apply(ops, "test-calendar")[1])
        self.raw.append(self.lookalike("landed", name))
        self.jobs.append(job(id="landed", name=name, schedule_kind="once", start=T10))
        self.dispatch.reset_mock()
        self.assertFalse(M._apply(self.plan(), "test-calendar")[1])
        self.dispatch.assert_not_called()
        rec = M._load_state()["occurrences"][ops[0]["occurrence"]]
        self.assertEqual((rec["child_id"], rec["preexisting"]), ("landed", ["victim"]))

    def test_ambiguous_or_unverifiable_recovery_is_refused(self):
        ops = self.plan()
        name = ops[0]["create_args"]["name"]
        self.dispatch.return_value = "not JSON"
        self.assertTrue(M._apply(ops, "test-calendar")[1])
        for landed in ([self.lookalike("a", name), self.lookalike("b", name)],
                       [{**self.lookalike("x", name), "prompt": "something else"}]):
            with self.subTest(n=len(landed)):
                self.raw = landed
                self.dispatch.reset_mock()
                errors = M._apply(self.plan(), "test-calendar")[1]
                self.assertIn("none is provably ours", errors[0]["error"])
                self.dispatch.assert_not_called()  # nothing adopted, nothing created
                self.assertIsNone(M._load_state()["occurrences"][ops[0]["occurrence"]]["child_id"])

    def test_fresh_attempt_never_adopts_a_lookalike(self):
        ops = self.plan()
        self.raw = [self.lookalike("victim", ops[0]["create_args"]["name"])]
        self.create_lands()
        M._apply(ops, "test-calendar")
        creates = [c for c in self.dispatch.call_args_list if c.args[1]["action"] == "create"]
        self.assertEqual(len(creates), 1)

    def test_drop_move_leaves_a_copy_without_provenance_in_place(self):
        key = P.occurrence_key("j1", T9)
        st = M._load_state()
        st["occurrences"] = {key: {"key": key, "source_job_id": "j1", "child_id": "legacy", "event_id": "e9",
                                   "original_slot": T9, "mode": "additional", "status": "pending"}}
        M._save_state(st)
        self.raw = [{"id": "legacy", "state": "scheduled"}]
        drop = {"direction": "calendar->cron", "action": "drop_move", "job_id": "j1", "reason": "r",
                "occurrence": key, "child_id": "legacy", "event_id": "e9"}
        errors = M._apply([drop], "test-calendar")[1]
        self.dispatch.assert_not_called()
        self.assertIn("hermes cron remove legacy", errors[0]["error"])
        self.assertEqual(M._load_state()["occurrences"][key]["status"], "dropped")


class LockTest(TR.RuntimeTest):
    def test_applying_runs_and_state_commands_refuse_while_locked(self):
        self.events = [ev(start=T10)]
        with M._lock():
            for res in (M.sync(confirm=True, allow_additional_run=True), M.sync(auto=True), M.mode("single"),
                        M.bind("other")):
                self.assertTrue(res["refused"])
                self.assertIn("already running", res["reason"])
            self.assertEqual(M.sync()["pending_ops"], 1)  # a dry run only reads, so it still answers
        self.dispatch.assert_not_called()
        self.cal.patch.assert_not_called()
        self.assertEqual(M._load_state()["calendar_id"], "test-calendar")

    def test_overlapping_confirm_creates_exactly_one_copy(self):
        self.events = [ev(start=T10)]
        inner = []
        child = job(id="child", schedule_kind="once", start=T10)

        def dispatch(name, args):
            if args["action"] == "create":
                inner.append(M.sync(confirm=True, allow_additional_run=True))  # a second process overlaps
                self.jobs.append(child)
                return json.dumps({"success": True, "job_id": "child"})
            return json.dumps({"success": True})
        self.dispatch.side_effect = dispatch
        with patch.object(M, "_snapshot"):
            out = M.sync(confirm=True, allow_additional_run=True)
        self.assertEqual(out["errors"], [])
        self.assertEqual(self.dispatch.call_count, 1)
        self.assertTrue(inner[0]["busy"])

    def test_skip_tick_waits_then_reports_busy(self):
        with M._lock(), patch("time.sleep"), patch("time.monotonic", side_effect=[0, 0, 200]):
            self.assertEqual(M.skip_tick()["errors"], [M.BUSY])
        self.dispatch.assert_not_called()

    def test_writers_never_share_a_tmp_file(self):
        names = []
        real = M.Path.write_text
        with patch.object(M.Path, "write_text", autospec=True,
                          side_effect=lambda p, *a, **k: names.append(p.name) or real(p, *a, **k)):
            M._save_state({"calendar_id": "a"})
            M._save_state({"calendar_id": "b"})
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(n.endswith(".tmp") and n.startswith(self.path.name) for n in names))
        self.assertEqual(M._load_state()["calendar_id"], "b")


class SkipCatchUpTest(unittest.TestCase):
    """Arm with catch_up=true, then reconcile: the plugin's own catch_up=false is not an edit."""

    def setUp(self):
        TR.RuntimeTest.setUp(self)
        self.patches[0].stop()  # real _crons/_view over the fake cron store below
        for p in (patch.object(M, "_now", return_value=NOW), patch.object(M, "_job_zone", return_value=timezone.utc),
                  patch.object(M, "_install_skip_script", return_value=S.SCRIPT_NAME), patch.object(M, "_snapshot")):
            p.start()
            self.addCleanup(p.stop)
        self.raw = [raw(catch_up=True, deliver="local")]
        self.calls = []
        self.dispatch.side_effect = self.hermes
        view = M._view(self.raw[0])
        M._save_state({"calendar_id": "cal", "jobs": {"j1": {"event_id": "e1", "job_hash": P.job_hash(view),
                                                            "start": SLOT, "summary": "Daily brief"}}})
        self.events = [ev(start=MOVED)]
        self.cal.patch.return_value = ev()

    def hermes(self, name, a):
        """Just enough of cronjob_manage to mutate the fake store like Hermes does."""
        self.calls.append(a)
        src = self.raw[0]
        if a["action"] == "create":
            jid = f"n{len(self.calls)}"
            self.raw.append({"id": jid, "name": a["name"], "state": "scheduled", "next_run_at": a["schedule"],
                             "schedule": {"kind": "once", "run_at": a["schedule"]},
                             **{k: v for k, v in a.items() if k not in {"action", "name", "schedule", "repeat"}}})
            return json.dumps({"success": True, "job_id": jid})
        if a["action"] == "pause":
            src.update(enabled=False, state="paused", paused_reason=a["reason"])
        elif a["action"] == "resume":
            src.update(enabled=True, state="scheduled", paused_reason=None)
        elif a["action"] == "update":
            src.update({k: v for k, v in a.items() if k not in {"action", "job_id"}})
        return json.dumps({"success": True})

    def test_arm_then_reconcile_with_catch_up_true(self):
        out = M.sync(confirm=True, skip_original="experimental")
        self.assertEqual(out["errors"], [])
        self.assertIs(self.raw[0]["catch_up"], False)  # our override is live
        plan = M.sync()
        self.assertNotIn("drop_move", [o["action"] for o in plan["ops"]])
        self.assertNotIn("skip_reconcile", plan)  # no abort planned
        self.calls.clear()
        with patch.object(M, "_now", return_value=datetime.fromisoformat(SLOT) + timedelta(minutes=11)):
            tick = M.skip_tick()
        self.assertEqual((tick["errors"], tick["warnings"]), ([], []))
        self.assertEqual(self.calls, [{"action": "resume", "job_id": "j1"}])  # resumed, not aborted
        self.raw[0]["next_run_at"] = "2026-11-11T09:00:00+11:00"  # Hermes skipped the elapsed slot
        self.calls.clear()
        with patch.object(M, "_now", return_value=datetime.fromisoformat(SLOT) + timedelta(minutes=26)):
            tick = M.skip_tick()
        self.assertEqual(tick["warnings"], [])
        self.assertEqual(self.calls[0], {"action": "update", "job_id": "j1", "grace_minutes": "", "catch_up": True})

    def test_a_real_edit_during_the_skip_still_aborts(self):
        M.sync(confirm=True, skip_original="experimental")
        self.raw[0]["prompt"] = "something else"
        acts = [o["action"] for o in M.sync()["ops"]]
        self.assertIn("drop_move", acts)


for _name in list(vars(TR.RuntimeTest)):
    if _name.startswith("test_"):
        setattr(ProvenanceTest, _name, None)
        setattr(LockTest, _name, None)


if __name__ == "__main__":
    unittest.main()
