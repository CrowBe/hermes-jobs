import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, MagicMock

ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("hermes_jobs_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)
from test_planner import job, ev, synced, T9, T10, T11


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "state.json"
        self.patcher = patch.object(M, "_state_file", return_value=self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.jobs = [job()]
        self.raw = []
        self.events = []
        self.cal = MagicMock()
        self.dispatch = MagicMock(return_value=json.dumps({"success": True}))
        M._CTX["ctx"] = types.SimpleNamespace(dispatch_tool=lambda name, args: self.dispatch(name, args))
        self.registry_patch = patch.object(M, "_cron_dispatcher", side_effect=lambda: (lambda args: M._CTX["ctx"].dispatch_tool(M.CRON_TOOL, args)))
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.patches = [patch.object(M, "_crons", side_effect=lambda: self.jobs),
                        patch.object(M, "_raw_jobs", side_effect=lambda: self.raw),
                        patch.object(M, "_events", side_effect=lambda *_: self.events),
                        patch(M.__name__ + ".gcal.Calendar", return_value=self.cal)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        M._save_state({"calendar_id": "test-calendar", "jobs": synced()})

    def test_pause_tombstone_survives_snapshot_and_no_recreate(self):
        writes, errors = M._apply(M.planner.plan(self.jobs, [], synced()), "test-calendar")
        self.assertEqual((writes, errors), (1, []))
        self.jobs = [job(enabled=False)]
        M._snapshot("test-calendar")
        state = M._load_state()["jobs"]
        self.assertTrue(state["j1"]["paused_by_delete"])
        self.assertEqual(M.planner.plan(self.jobs, [], state)[0]["action"], "notice")
        self.assertIn("hermes cron resume j1", state["j1"]["pause_reason"])

    def test_resumed_insert_clears_tombstone_preserves_source_link(self):
        M._save_state({"calendar_id": "test-calendar", "jobs": synced(paused_by_delete=True, source_job_id="source", event_id=None)})
        self.cal.insert.return_value = ev()
        M._apply(M.planner.plan(self.jobs, [], M._load_state()["jobs"]), "test-calendar")
        state = M._load_state()["jobs"]["j1"]
        self.assertNotIn("paused_by_delete", state)
        self.assertEqual(state["source_job_id"], "source")

    def test_one_shot_create_links_and_retags_event(self):
        child = job(id="child", schedule_kind="once", start=T10)
        self.dispatch.side_effect = lambda *_: (self.jobs.append(child) or json.dumps({"success": True, "job_id": "child"}))
        ops = M.planner.plan(self.jobs, [ev(start=T10)], synced())
        writes, errors = M._apply(ops, "test-calendar")
        self.assertFalse(errors)
        self.assertEqual(writes, 2)
        call = self.dispatch.call_args.args
        self.assertEqual(call[0], "cronjob_manage")
        self.assertEqual(call[1]["action"], "create")
        self.assertEqual(call[1]["repeat"], 1)
        self.assertEqual(self.cal.patch.call_args.args[1]["hermes_job_id"], "child")
        state = M._load_state()["jobs"]
        self.assertEqual(state["child"]["source_job_id"], "j1")
        self.assertEqual(state["j1"]["board_start"], T11)
        self.assertNotIn("pending_move", state["j1"])

    def test_failed_retag_retries_without_new_create(self):
        child = job(id="child", schedule_kind="once", start=T10)
        self.dispatch.side_effect = lambda *_: (self.jobs.append(child) or json.dumps({"success": True, "job_id": "child"}))
        self.cal.patch.side_effect = RuntimeError("network")
        ops = M.planner.plan(self.jobs, [ev(start=T10)], synced())
        self.assertTrue(M._apply(ops, "test-calendar")[1])
        state = M._load_state()["jobs"]
        retry = M.planner.plan(self.jobs, [ev(start=T10)], state)
        self.cal.patch.side_effect = None
        M._apply(retry, "test-calendar")
        self.assertEqual(self.dispatch.call_count, 1)

    def test_unknown_create_result_retry_adopts_existing_copy(self):
        self.dispatch.return_value = "not JSON"  # create landed but its result was lost
        ops = M.planner.plan(self.jobs, [ev(start=T10)], synced())
        self.assertTrue(M._apply(ops, "test-calendar")[1])
        self.raw = [{"id": "landed", "name": ops[0]["create_args"]["name"], "state": "scheduled",
                     "schedule": {"kind": "once", "run_at": T10}}]
        self.jobs.append(job(id="landed", name=ops[0]["create_args"]["name"], schedule_kind="once", start=T10))
        self.dispatch.reset_mock()
        retry = M.planner.plan(self.jobs, [ev(start=T10)], M._load_state()["jobs"])
        self.assertEqual([o["action"] for o in retry], ["create_once"])  # no duplicate event for the copy
        self.assertFalse(M._apply(retry, "test-calendar")[1])
        self.dispatch.assert_not_called()  # adopted, no duplicate
        self.assertEqual(M._load_state()["occurrences"][retry[0]["occurrence"]]["child_id"], "landed")

    def test_confirm_requires_explicit_additional_run_opt_in(self):
        self.events = [ev(start=T10)]
        result = M.sync(confirm=True)
        self.assertTrue(result["refused"])
        self.assertEqual(result["writes"], 0)
        self.dispatch.assert_not_called()
        self.cal.patch.assert_not_called()

    def test_offline_does_not_infer_calendar_deletions(self):
        result = M.sync(offline=True)
        self.assertEqual([op["action"] for op in result["ops"]], ["insert_event"])
        self.assertEqual(result["writes"], 0)
        self.dispatch.assert_not_called()

    def test_completed_child_cleanup_clears_event_link(self):
        self.jobs = [job(terminal=True)]
        M._save_state({"calendar_id": "test-calendar", "jobs": synced(source_job_id="parent")})
        M._apply(M.planner.plan(self.jobs, [ev()], M._load_state()["jobs"]), "test-calendar")
        state = M._load_state()["jobs"]["j1"]
        self.assertIsNone(state["event_id"])
        self.assertEqual(state["source_job_id"], "parent")

    def test_copy_preserves_empty_allowlist_and_resolves_source_origin(self):
        args = M._copy_args({"id": "j1", "prompt": "trusted", "skills": ["brief"],
                             "enabled_toolsets": [], "deliver": "origin", "context_from": ["self"],
                             "origin": {"platform": "telegram", "chat_id": "123", "thread_id": "4"}})
        self.assertEqual(args["deliver"], "telegram:123:4")
        self.assertEqual(args["enabled_toolsets"], [])
        self.assertEqual(args["context_from"], ["j1"])

    def test_uncopyable_model_is_not_silently_dropped(self):
        with self.assertRaises(ValueError):
            M._copy_args({"id": "j1", "model": "pinned"})

    def test_malformed_tool_result_is_not_success(self):
        with self.assertRaises((RuntimeError, ValueError)):
            M._check("oops")

    def test_source_preview_is_created_on_following_sync(self):
        child = job(id="child", schedule_kind="once", start=T10)
        self.dispatch.side_effect = lambda *_: (self.jobs.append(child) or json.dumps({"success": True, "job_id": "child"}))
        M._apply(M.planner.plan(self.jobs, [ev(start=T10)], synced()), "test-calendar")
        self.events = [ev(hermes_job_id="child", start=T10)]
        M._snapshot("test-calendar")
        plan = M.planner.plan(self.jobs, self.events, M._load_state()["jobs"])
        source = [o for o in plan if o["job_id"] == "j1"]
        self.assertEqual(source[0]["action"], "insert_event")
        self.assertEqual(source[0]["event"]["start"], T11)

    def test_deleted_copy_does_not_pause_source(self):
        child = job(id="child", schedule_kind="once", start=T10)
        state = {**synced(), "child": {"event_id": "e2", "source_job_id": "j1", "job_hash": M.planner.job_hash(child)}}
        plan = M.planner.plan([job(), child], [ev()], state)
        self.assertEqual([(o["job_id"], o["action"]) for o in plan], [("child", "pause_job")])

    def test_offline_real_event_fetch_is_guarded(self):
        # Exercise actual helper, not the _events test mock: no Calendar object is built.
        self.patches[2].stop()
        self.assertEqual(M._events("test-calendar", True), [])
        self.cal.list_tagged.assert_not_called()


class DispatchPathTest(unittest.TestCase):
    """Real _cron_dispatcher: empty CLI registry is populated, then cronjob_manage is dispatched."""

    def test_cli_registry_discovery_then_named_dispatch(self):
        entries = {}
        fake_registry = types.SimpleNamespace(get_entry=lambda name, **_: entries.get(name))
        discover = MagicMock(side_effect=lambda: entries.update(cronjob_manage=object()))
        fake_mod = types.SimpleNamespace(registry=fake_registry, discover_builtin_tools=discover)
        calls = []
        M._CTX["ctx"] = types.SimpleNamespace(dispatch_tool=lambda name, args: calls.append((name, args)) or "{}")
        with patch.dict(sys.modules, {"tools.registry": fake_mod}):
            cron = M._cron_dispatcher()
            cron({"action": "pause", "job_id": "j1", "reason": "r"})
        discover.assert_called_once()
        self.assertEqual(calls, [("cronjob_manage", {"action": "pause", "job_id": "j1", "reason": "r"})])

    def test_missing_tool_fails_before_any_calendar_write(self):
        fake_registry = types.SimpleNamespace(get_entry=lambda name, **_: None)
        fake_mod = types.SimpleNamespace(registry=fake_registry, discover_builtin_tools=lambda: None)
        cal = MagicMock()
        M._CTX["ctx"] = types.SimpleNamespace(dispatch_tool=MagicMock())
        ops = [{"direction": "cron->calendar", "action": "insert_event", "job_id": "x", "event": {}},
               {"direction": "calendar->cron", "action": "pause_job", "job_id": "j1", "reason": "r"}]
        with patch.dict(sys.modules, {"tools.registry": fake_mod}), \
                patch(M.__name__ + ".gcal.Calendar", return_value=cal):
            with self.assertRaises(RuntimeError):
                M._apply(ops, "cal")
        cal.insert.assert_not_called()
