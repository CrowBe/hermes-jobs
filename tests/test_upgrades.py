"""Run outcome on the event (colorId + one "last run:" line) and the read-only /jobs command."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import test_runtime as TR
from test_planner import job, ev, synced, T9
from test_recurring import rjob, master, rstate, acts

M = TR.M
P = M.planner
B = M.board
G = M.gcal
OK = {"at": "Sat 07 Nov 09:00 AEDT", "status": "ok"}
ERR = {"at": "Sat 07 Nov 09:00 AEDT", "status": "error"}


class OutcomeTest(unittest.TestCase):
    def test_ok_run_marks_green_with_one_line(self):
        ops = B.plan([job(last_run=OK)], [ev()], synced())
        self.assertEqual(acts(ops), ["mark_run"])
        self.assertEqual(ops[0]["event"]["color_id"], "10")
        self.assertTrue(ops[0]["event"]["description"].endswith("\nlast run: Sat 07 Nov 09:00 AEDT ok"))
        self.assertEqual(P.strip_run(ops[0]["event"]["description"]), ev()["description"])

    def test_error_run_marks_red(self):
        ops = B.plan([rjob(last_run=ERR)], [master()], rstate())
        self.assertEqual(acts(ops), ["mark_run"])
        self.assertEqual((ops[0]["event_id"], ops[0]["event"]["color_id"]), ("m1", "11"))
        self.assertIn("last run: Sat 07 Nov 09:00 AEDT error", ops[0]["event"]["description"])

    def test_other_status_is_banana(self):
        ops = B.plan([job(last_run={**OK, "status": "delivery_failed"})], [ev()], synced())
        self.assertEqual(ops[0]["event"]["color_id"], "5")

    def test_never_run_does_nothing(self):
        self.assertEqual(B.plan([job()], [ev()], synced()), [])
        self.assertNotIn("color_id", P.event_body(job()))
        self.assertNotIn("last run", P.event_body(job())["description"])

    def test_unchanged_outcome_is_zero_ops(self):
        body = P.event_body(job(last_run=OK))
        self.assertEqual(B.plan([job(last_run=OK)], [ev(description=body["description"], color_id="10")], synced()), [])
        mb = P.event_body(rjob(last_run=OK))
        self.assertEqual(B.plan([rjob(last_run=OK)], [master(description=mb["description"], color_id="10")], rstate()), [])

    def test_line_is_replaced_never_accumulated_rest_kept(self):
        old = "my own notes\nlast run: Fri 06 Nov 09:00 AEDT error\nmore notes"
        ops = B.plan([job(last_run=OK)], [ev(description=old, color_id="11")], synced())
        self.assertEqual(ops[0]["event"]["description"], "my own notes\nmore notes\nlast run: Sat 07 Nov 09:00 AEDT ok")
        self.assertEqual(ops[0]["event"]["description"].count("last run:"), 1)

    def test_title_never_touched(self):
        for j in (job(last_run=OK), job(last_run=ERR), job(enabled=False, last_run=ERR)):
            self.assertEqual(P.event_body(j)["summary"], P.title(j))
            self.assertNotIn("ok", P.title(j).split())
        op = B.plan([job(last_run=OK)], [ev()], synced())[0]
        self.assertEqual(set(op["event"]), {"description", "color_id"})
        g = G.to_google(op["event"])
        self.assertEqual(set(g), {"description", "colorId"})  # no summary, start, tags or recurrence

    def test_run_line_never_trips_patch_retitle_or_move(self):
        lined = ev(description=P.event_body(job(last_run=OK))["description"], color_id="10")
        edited = job(prompt="new prompt", last_run=OK)  # hash changed: only the digest line differs
        self.assertEqual(acts(B.plan([edited], [lined], synced())), ["patch_event"])
        same = synced(job(last_run=OK))
        self.assertEqual(B.plan([job(last_run=OK)], [lined], same), [])
        # first sync of an event that only differs by the run line: record, not a rewrite
        self.assertEqual(acts(P.plan([job()], [lined], {})), ["record"])
        m = master(description=P.event_body(rjob(last_run=OK))["description"], color_id="10")
        self.assertEqual(acts(B.plan([rjob(last_run=OK)], [m], {})), ["record"])

    def test_full_patch_carries_outcome_no_second_op(self):
        ops = B.plan([job(prompt="new", last_run=OK)], [ev()], synced())
        self.assertEqual(acts(ops), ["patch_event"])
        self.assertEqual(ops[0]["event"]["color_id"], "10")
        self.assertEqual(G.to_google(ops[0]["event"])["colorId"], "10")

    def test_from_google_reads_color(self):
        g = {"id": "e1", "summary": "x", "colorId": "10", "start": {"dateTime": T9}}
        self.assertEqual(G.from_google(g)["color_id"], "10")


class Runtime(unittest.TestCase):
    setUp = TR.RuntimeTest.setUp  # runtime fixtures without re-running RuntimeTest's own tests


class OutcomeRuntimeTest(Runtime):
    def test_view_reads_hermes_fields_never_last_error(self):
        raw = {"id": "j1", "name": "Daily brief", "prompt": "p", "enabled": True, "state": "scheduled",
               "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "next_run_at": "2026-11-11T09:00:00+11:00",
               "last_run_at": "2026-11-09T22:00:00+00:00", "last_status": "error",
               "last_error": "secret stack trace"}
        v = M._view(raw)
        self.assertEqual(v["last_run"], {"at": "Tue 10 Nov 09:00 AEDT", "status": "error"})
        self.assertNotIn("secret", json.dumps(P.event_body(v)))
        self.assertNotIn("last_run", M._view({**raw, "last_run_at": None, "last_status": None}))

    def test_apply_mark_run_patches_description_and_colour_only(self):
        ops = B.plan([job(last_run=OK)], [ev()], synced())
        self.assertEqual(M._apply(ops, "test-calendar"), (1, []))
        eid, body = self.cal.patch.call_args.args
        self.assertEqual((eid, set(body)), ("e1", {"description", "color_id"}))
        self.dispatch.assert_not_called()

    def test_dry_run_shows_mark_run_and_writes_nothing(self):
        self.jobs, self.events = [job(last_run=OK)], [ev()]
        res = M.sync()
        self.assertEqual((acts(res["ops"]), res["writes"]), (["mark_run"], 0))
        self.cal.patch.assert_not_called()


NOW = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)  # Tue 10 Nov 12:00 Sydney


def rawjob(**kw):
    return {"id": "j1", "name": "morning brief", "prompt": "p", "enabled": True, "state": "scheduled",
            "schedule": {"kind": "cron", "expr": "30 7 * * *"}, "next_run_at": "2026-11-11T07:30:00+11:00",
            "timezone": "Australia/Sydney", **kw}


class JobsCommandTest(Runtime):
    def setUp(self):
        super().setUp()
        self.raw = [rawjob(), rawjob(id="j2", name="probe-b", enabled=False, state="paused")]
        self.jobs = [M._view(r) for r in self.raw]
        for p in [patch.object(M, "_now", return_value=NOW)]:
            p.start()
            self.addCleanup(p.stop)
        # Every write path: any call fails the test.
        self.writes = {n: MagicMock(side_effect=AssertionError(n)) for n in
                       ("_save_state", "_apply", "_reconcile_skips", "_snapshot", "_install_skip_script")}
        for n, m in self.writes.items():
            p = patch.object(M, n, m)
            p.start()
            self.addCleanup(p.stop)
        self.registry_patch.stop()
        p = patch.object(M, "_cron_dispatcher", side_effect=AssertionError("cron dispatch"))
        p.start()
        self.addCleanup(p.stop)

    def assert_no_writes(self):
        for n, m in self.writes.items():
            m.assert_not_called()
        for meth in ("insert", "patch", "delete"):
            getattr(self.cal, meth).assert_not_called()
        self.dispatch.assert_not_called()

    def test_agenda_waiting_and_confirm_command_without_writes(self):
        self.events = []  # synced j1 lost its event (pending pause); paused j2 has none yet (pending insert)
        out = M.jobs_command("")
        lines = out.splitlines()
        self.assertEqual(lines[0], "Hermes jobs, next 7 days (Australia/Sydney):")
        self.assertEqual(lines[1], "  Wed 11 Nov 07:30  morning brief")
        self.assertEqual(sum("morning brief" in l and "Nov 07:30" in l for l in lines), 7)
        self.assertNotIn("probe-b", "\n".join(lines[1:8]))  # paused: not on the agenda
        self.assertIn("Waiting on you (dry run, nothing written):", out)
        self.assertIn("  - morning brief: pause_job - job j1 paused because its calendar event was deleted", out)
        self.assertIn("  - probe-b: insert_event - job has no event yet", out)
        self.assertEqual(lines[-1], "Apply the plan: hermes hermes-jobs sync --confirm")
        self.assert_no_writes()

    def test_overdue_slot_shown_once_then_from_now(self):
        self.raw = [rawjob(next_run_at="2026-11-10T10:00:00+11:00")]
        self.jobs = [M._view(r) for r in self.raw]
        out = M.jobs_command("")
        self.assertIn("Tue 10 Nov 10:00  morning brief  (due, not run yet)", out)
        self.assertEqual(out.count("(due, not run yet)"), 1)
        self.assertIn("Wed 11 Nov 07:30  morning brief\n", out)
        self.assert_no_writes()

    def test_auth_missing_degrades_to_cron_only(self):
        with patch.object(M, "_events", side_effect=RuntimeError("not signed in")):
            out = M.jobs_command("")
        self.assertIn("Wed 11 Nov 07:30  morning brief", out)
        self.assertIn("calendar not checked (RuntimeError: not signed in); cron only", out)
        self.assertIn("hermes auth add hermes-jobs:google", out)
        self.assert_no_writes()

    def test_unbound_calendar_skips_google_entirely(self):
        with patch.object(M, "_load_state", return_value={"calendar_id": None, "jobs": {}, "occurrences": {}}), \
                patch.object(M, "sync", side_effect=AssertionError("no plan without a calendar")):
            out = M.jobs_command("")
        self.assertIn("no calendar bound", out)
        self.assert_no_writes()

    def test_pending_state_is_listed(self):
        self.events = [ev(hermes_job_id="j1")]
        st = {"calendar_id": "test-calendar", "occurrences": {},
              "jobs": {"j1": {"event_id": "e1", "split_pending": {"cron": "0 8 * * *", "until": "2026-11-12T00:00:00+00:00"}}}}
        with patch.object(M, "_load_state", return_value=st):
            out = M.jobs_command("")
        self.assertIn("morning brief: 'this and following' -> cron 0 8 * * * waits until", out)
        self.assert_no_writes()

    def test_registered_as_slash_command(self):
        ctx = MagicMock()
        M.register(ctx)
        name, handler = ctx.register_command.call_args.args
        self.assertEqual((name, handler), ("jobs", M.jobs_command))
        self.assertEqual([c.kwargs["name"] for c in ctx.register_tool.call_args_list], ["hermes_jobs_discover"])


if __name__ == "__main__":
    unittest.main()
