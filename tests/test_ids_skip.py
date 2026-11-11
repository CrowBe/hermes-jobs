import importlib
import json
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import test_runtime as TR
from test_runtime import M
from test_planner import job, ev, synced, T9, T10, T11

P, S = M.planner, M.skip
G = importlib.import_module(M.__name__ + ".gcal")
SLOT = "2026-11-10T09:00:00+11:00"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)  # ~8h before SLOT


def raw(**kw):
    return {"id": "j1", "name": "Daily brief", "prompt": "summarise", "enabled": True, "state": "scheduled",
            "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
            "schedule_display": "0 9 * * *", "next_run_at": SLOT, **kw}


class DerivedIdTest(unittest.TestCase):
    def test_key_is_stable_utc_canonical_and_a_valid_google_id(self):
        a = P.occurrence_key("j1", "2026-11-10T09:00:00+11:00")
        self.assertEqual(a, P.occurrence_key("j1", "2026-11-09T22:00:00+00:00"))  # same instant
        self.assertNotEqual(a, P.occurrence_key("j1", "2026-11-11T09:00:00+11:00"))
        self.assertNotEqual(a, P.occurrence_key("j2", "2026-11-10T09:00:00+11:00"))
        self.assertRegex(a, r"^[a-v0-9]{5,1024}$")
        with self.assertRaises(ValueError):
            P.occurrence_key("j1", "2026-11-10T09:00:00")  # naive instants have no identity

    def test_insert_uses_derived_id_and_tags_occurrence(self):
        e = P.plan([job()], [], {})[0]["event"]
        self.assertEqual(e["id"], P.occurrence_key("j1", T9))
        self.assertEqual(e["props"]["hermes_occurrence"], e["id"])

    def test_legacy_google_id_event_adopted_by_tag_not_recreated(self):
        legacy = ev(id="3i1am4tj6b5ribejq81s9ph0fg")
        self.assertEqual(P.plan([job()], [legacy], synced(event_id=legacy["id"])), [])
        lost = P.plan([job()], [legacy], {})  # state file lost: adopt, never insert/delete
        self.assertEqual([o["action"] for o in lost], ["record"])

    def test_lost_state_delete_pause_rebuilt_from_cron_marker(self):
        ops = P.plan([job(enabled=False, paused_by="delete")], [], {})
        self.assertEqual([o["action"] for o in ops], ["notice"])

    def test_to_google_always_writes_full_private_set(self):
        g = G.to_google({"id": "abc12", "start": T9, "hermes_job_id": "j1", "props": {"hermes_occurrence": "k"}})
        self.assertEqual(g["id"], "abc12")
        self.assertEqual(g["extendedProperties"]["private"], {"hermes_occurrence": "k", "hermes_job_id": "j1"})

    def test_insert_409_adopts_existing_and_revives_cancelled(self):
        calls = []

        def req(method, url, body=None):
            calls.append((method, body))
            if method == "POST":
                raise G.Conflict("409")
            if method == "GET":
                return {"id": "k1234", "status": "cancelled"}
            return {"id": "k1234", "summary": body["summary"], "start": body["start"],
                    "extendedProperties": body["extendedProperties"]}
        with patch.object(G, "_req", side_effect=req):
            out = G.Calendar("cal").insert({"id": "k1234", "summary": "x", "start": T9, "hermes_job_id": "j1"})
        self.assertEqual([c[0] for c in calls], ["POST", "GET", "PATCH"])
        self.assertEqual(calls[2][1]["status"], "confirmed")
        self.assertNotIn("id", calls[2][1])
        self.assertEqual(out["hermes_job_id"], "j1")

    def test_insert_409_refuses_event_owned_by_other_job(self):
        def req(method, url, body=None):
            if method == "POST":
                raise G.Conflict("409")
            return {"id": "k1234", "extendedProperties": {"private": {"hermes_job_id": "other"}}}
        with patch.object(G, "_req", side_effect=req), self.assertRaises(G.CalendarError):
            G.Calendar("cal").insert({"id": "k1234", "summary": "x", "start": T9, "hermes_job_id": "j1"})

    def test_from_google_exposes_rebuild_props(self):
        e = G.from_google({"id": "e", "start": {"dateTime": T10}, "extendedProperties": {"private": {
            "hermes_job_id": "c", "hermes_source_job_id": "j1", "hermes_original_slot": "x"}}})
        self.assertEqual(e["props"], {"hermes_source_job_id": "j1", "hermes_original_slot": "x"})


class SkipDecisionTest(unittest.TestCase):
    def rec(self, **kw):
        return {"key": "k" * 32, "source_job_id": "j1", "original_slot": SLOT, "fingerprint": "fp",
                "mode": "skip", "status": "pending", "phase": "paused", "orig_grace": None,
                "orig_catch_up": None, "wake_ids": ["w1", "w2"], **kw}

    def ours(self, **kw):
        return raw(enabled=False, state="paused", paused_reason=S.marker("k" * 32, SLOT, None, None), **kw)

    def at(self, minutes):
        return datetime.fromisoformat(SLOT) + timedelta(minutes=minutes)

    def test_eligibility_refusals(self):
        self.assertIsNone(S.eligibility(raw(), SLOT, NOW))
        self.assertIn("cron-expression", S.eligibility(raw(schedule={"kind": "interval", "minutes": 60}), SLOT, NOW))
        self.assertIn("paused", S.eligibility(raw(state="paused"), SLOT, NOW))
        self.assertIn("not the job's next run", S.eligibility(raw(next_run_at=T11), SLOT, NOW))
        self.assertIn("too late", S.eligibility(raw(), SLOT, self.at(-3)))

    def test_waits_until_after_grace_then_resumes(self):
        self.assertEqual(S.decide(self.rec(), self.ours(), "fp", self.at(5)), ([], []))
        self.assertEqual(S.decide(self.rec(), self.ours(), "fp", self.at(11))[0], ["resume"])

    def test_manual_pause_aborts_without_resume(self):
        acts, warn = S.decide(self.rec(), raw(state="paused", enabled=False, paused_reason="mine"), "fp", self.at(11))
        self.assertEqual(acts, ["restore", "cleanup", "abort"])
        self.assertIn("paused manually", warn[0])

    def test_manual_resume_aborts(self):
        acts, warn = S.decide(self.rec(), raw(), "fp", self.at(1))
        self.assertNotIn("resume", acts)
        self.assertIn("abort", acts)
        self.assertIn("resumed manually", warn[0])

    def test_series_edit_restores_and_resumes_our_pause(self):
        acts, warn = S.decide(self.rec(), self.ours(), "CHANGED", self.at(-60))
        self.assertEqual(acts, ["restore", "resume", "cleanup", "abort"])
        self.assertIn("edited", warn[0])

    def test_restore_only_after_hermes_consumed_slot(self):
        r = self.rec(phase="resumed")
        self.assertEqual(S.decide(r, raw(next_run_at=SLOT), "fp", self.at(20))[0], [])
        self.assertEqual(S.decide(r, raw(next_run_at="2026-11-11T09:00:00+11:00"), "fp", self.at(20))[0],
                         ["restore", "cleanup", "done"])

    def test_detects_original_slot_ran(self):
        acts, warn = S.decide(self.rec(phase="resumed"), raw(last_run_at=self.at(11).isoformat(),
                                                             next_run_at=SLOT), "fp", self.at(20))
        self.assertIn("done", acts)
        self.assertIn("RAN", warn[0])

    def test_interrupted_arming_is_undone(self):
        acts, _ = S.decide(self.rec(phase="arming"), self.ours(), "fp", self.at(-60))
        self.assertEqual(acts, ["restore", "resume", "cleanup", "abort"])

    def test_restore_args(self):
        self.assertEqual(S._restore_args(self.rec()), {"grace_minutes": ""})
        self.assertEqual(S._restore_args(self.rec(orig_grace=30, orig_catch_up=True)),
                         {"grace_minutes": 30, "catch_up": True})

    def test_arm_sequence_journals_then_pause_grace_wakeups(self):
        calls, saves = [], []
        cron = lambda a: calls.append(a) or {"success": True, "job_id": f"w{len(calls)}"}
        rec = self.rec(phase=None)
        S.arm(cron, lambda r: r, rec, raw(catch_up=True), lambda: saves.append(dict(rec)), lambda: S.SCRIPT_NAME)
        self.assertEqual(saves[0]["phase"], "arming")
        self.assertEqual(saves[0]["orig_catch_up"], True)
        self.assertEqual(calls[0]["action"], "pause")
        self.assertEqual(S.parse_marker(calls[0]["reason"])["slot"], P.utc_instant(SLOT))
        self.assertEqual(calls[1], {"action": "update", "job_id": "j1", "grace_minutes": 0, "catch_up": False})
        wakes = calls[2:]
        self.assertEqual([w["schedule"] for w in wakes], [self.at(10).isoformat(), self.at(25).isoformat()])
        for w in wakes:
            self.assertTrue(w["no_agent"])
            self.assertEqual((w["script"], w["deliver"], w["repeat"]), (S.SCRIPT_NAME, "local", 1))
            self.assertNotIn("prompt", w)
        self.assertEqual(rec["phase"], "paused")
        self.assertEqual(rec["wake_ids"], ["w3", "w4"])

    def test_arm_leaves_absent_catch_up_untouched(self):
        calls = []
        cron = lambda a: calls.append(a) or {"job_id": "w"}
        S.arm(cron, lambda r: r, self.rec(), raw(), lambda: None, lambda: S.SCRIPT_NAME)
        self.assertEqual(calls[1], {"action": "update", "job_id": "j1", "grace_minutes": 0})

    def test_rebuild_from_marker_when_state_lost(self):
        recs = {}
        wake = {"id": "w9", "name": S.WAKE_PREFIX + "k" * 8 + " #1", "script": S.SCRIPT_NAME}
        S.rebuild([self.ours(), wake], recs, lambda r: "fp")
        r = recs["k" * 32]
        self.assertEqual((r["phase"], r["original_slot"], r["wake_ids"]), ("paused", P.utc_instant(SLOT), ["w9"]))


class SeriesEditPlannerTest(unittest.TestCase):
    def test_series_edit_drops_pending_copy_and_replans_source(self):
        src = job(prompt="NEW prompt")
        child = job(id="c", schedule_kind="once", start=T10)
        recs = {"k": {"source_job_id": "j1", "child_id": "c", "event_id": "e1", "fingerprint": P.fingerprint(job()),
                      "status": "pending", "mode": "skip"}}
        state = {"j1": {"event_id": None, "job_hash": P.job_hash(job()), "original_slot": T9, "board_start": T11},
                 "c": {"event_id": "e1", "source_job_id": "j1"}}
        ops = P.plan([src, child], [ev(id="e1", hermes_job_id="c", start=T10)], state, recs)
        self.assertEqual([(o["job_id"], o["action"]) for o in ops], [("j1", "drop_move"), ("j1", "insert_event")])
        self.assertIn("undo experimental skip", ops[0]["reason"])
        self.assertEqual(ops[1]["event"]["start"], T9)  # cron truth, not the stale board preview

    def test_retitle_is_not_a_series_edit(self):
        self.assertEqual(P.fingerprint(job()), P.fingerprint(job(name="renamed", enabled=False)))


class SkipRuntimeTest(unittest.TestCase):
    def setUp(self):
        TR.RuntimeTest.setUp(self)  # reuse fixtures without re-running RuntimeTest's own tests
        self.calls = []
        self.dispatch.side_effect = lambda name, args: (self.calls.append(args) or
                                                        json.dumps({"success": True, "job_id": f"n{len(self.calls)}"}))
        self.now = patch.object(M, "_now", return_value=NOW)
        self.now.start()
        self.addCleanup(self.now.stop)
        self.inst = patch.object(M, "_install_skip_script", return_value=S.SCRIPT_NAME)
        self.inst.start()
        self.addCleanup(self.inst.stop)
        self.jobs = [job(start=SLOT, next_after="2026-11-11T09:00:00+11:00",
                         copy_args={"prompt": "summarise", "deliver": "local"})]
        self.raw = [raw()]
        M._save_state({"calendar_id": "cal", "jobs": {"j1": {"event_id": "e1", "job_hash": P.job_hash(self.jobs[0]),
                                                            "start": SLOT, "summary": "Daily brief"}}})
        self.events = [ev(start="2026-11-10T11:00:00+11:00")]

    def test_dry_run_shows_experimental_steps_and_writes_nothing(self):
        out = M.sync(skip_original="experimental")
        op = out["ops"][0]
        self.assertEqual((op["action"], op["mode"]), ("create_once", "skip"))
        self.assertEqual(len(op["skip_steps"]), 3)
        self.assertIn("'missed'", op["warning"])
        self.assertEqual(out["writes"], 0)
        self.dispatch.assert_not_called()
        self.cal.patch.assert_not_called()

    def test_confirm_without_any_flag_refused(self):
        out = M.sync(confirm=True)
        self.assertTrue(out["refused"])
        self.assertIn("--skip-original=experimental", out["reason"])
        self.dispatch.assert_not_called()

    def test_ineligible_skip_falls_back_to_refusal(self):
        self.raw = [raw(schedule={"kind": "interval", "minutes": 60})]
        out = M.sync(confirm=True, skip_original="experimental")
        self.assertTrue(out["refused"])
        self.assertIn("cron-expression", " ".join(out["messages"]))

    def test_confirm_experimental_creates_copy_then_arms_skip(self):
        self.cal.patch.return_value = ev()
        child = job(id="n1", schedule_kind="once", start="2026-11-10T11:00:00+11:00")
        orig = self.dispatch.side_effect
        self.dispatch.side_effect = lambda n, a: (self.jobs.append(child) if a.get("prompt") else None) or orig(n, a)
        with patch.object(M, "_snapshot"):
            out = M.sync(confirm=True, skip_original="experimental")
        self.assertEqual(out["errors"], [])
        self.assertEqual([c["action"] for c in self.calls], ["create", "pause", "update", "create", "create"])
        rec = next(iter(M._load_state()["occurrences"].values()))
        self.assertEqual((rec["mode"], rec["phase"], rec["child_id"]), ("skip", "paused", "n1"))
        self.assertEqual(rec["key"], P.occurrence_key("j1", SLOT))
        tags = self.cal.patch.call_args.args[1]["props"]
        self.assertEqual(tags["hermes_source_job_id"], "j1")

    def test_drop_move_undoes_skip_removes_copy_and_event(self):
        key = P.occurrence_key("j1", SLOT)
        st = M._load_state()
        st["occurrences"] = {key: {"key": key, "source_job_id": "j1", "child_id": "c", "event_id": "e9",
                                   "original_slot": SLOT, "mode": "skip", "phase": "paused", "status": "pending",
                                   "orig_grace": None, "orig_catch_up": None, "wake_ids": ["w1"],
                                   "child_owned": True}}
        M._save_state(st)
        self.raw = [raw(enabled=False, state="paused", paused_reason=S.marker(key, SLOT, None, None)),
                    {"id": "c", "state": "scheduled"}, {"id": "w1", "state": "scheduled"}]
        op = {"direction": "calendar->cron", "action": "drop_move", "job_id": "j1", "reason": "r",
              "occurrence": key, "child_id": "c", "event_id": "e9"}
        writes, errors = M._apply([op], "cal")
        self.assertEqual(errors, [])
        self.assertEqual([(c["action"], c["job_id"]) for c in self.calls],
                         [("update", "j1"), ("resume", "j1"), ("remove", "w1"), ("remove", "c")])
        self.cal.delete.assert_called_once_with("e9")
        self.assertEqual(M._load_state()["occurrences"][key]["status"], "dropped")

    def test_skip_tick_resumes_after_grace(self):
        key = P.occurrence_key("j1", SLOT)
        st = M._load_state()
        st["occurrences"] = {key: {"key": key, "source_job_id": "j1", "original_slot": SLOT,
                                   "fingerprint": P.fingerprint(M._view(raw())), "mode": "skip", "phase": "paused",
                                   "status": "fired", "wake_ids": []}}
        M._save_state(st)
        self.raw = [raw(enabled=False, state="paused", paused_reason=S.marker(key, SLOT, None, None))]
        with patch.object(M, "_now", return_value=datetime.fromisoformat(SLOT) + timedelta(minutes=11)), \
                patch.object(M, "_job_zone", return_value=timezone.utc):
            res = M.skip_tick()
        self.assertEqual(res["errors"], [])
        self.assertEqual(self.calls, [{"action": "resume", "job_id": "j1"}])
        self.assertEqual(M._load_state()["occurrences"][key]["phase"], "resumed")

    def test_wake_jobs_hidden_and_skip_paused_source_shown_active(self):
        self.patches[0].stop()  # real _crons over raw records
        key = "k" * 32
        self.raw = [raw(enabled=False, state="paused", paused_reason=S.marker(key, SLOT, None, None)),
                    {"id": "w", "name": S.WAKE_PREFIX + "kkkkkkkk #1", "script": S.SCRIPT_NAME,
                     "schedule": {"kind": "once", "run_at": SLOT}, "next_run_at": SLOT},
                    raw(id="d", enabled=False, state="paused", paused_reason=P.DELETE_MARKER)]
        with patch.object(M, "_job_zone", return_value=timezone.utc):
            views = {v["id"]: v for v in M._crons()}
        self.assertNotIn("w", views)
        self.assertTrue(views["j1"]["enabled"])
        self.assertEqual(views["j1"]["paused_by"], "skip")
        self.assertEqual((views["d"]["enabled"], views["d"]["paused_by"]), (False, "delete"))


if __name__ == "__main__":
    unittest.main()
