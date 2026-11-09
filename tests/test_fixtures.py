"""The four recurring cases on REAL Google payload shapes.

captured_*.json: sanitised read-only GETs of the live probe-a master, its deleted Tue 10 Nov
instance and its moved Thu 12 Nov instance (`hermes hermes-jobs capture-fixtures <job>`).
documented_*.json: 'all events' and 'this and following' built from Google's documented shapes
(not captured live). Every payload goes through gcal.from_google exactly like a live sync."""
import importlib
import json
import pathlib
import unittest

import test_runtime as TR
from test_planner import job

M = TR.M
P = M.planner
B = importlib.import_module(M.__name__ + ".board")
G = importlib.import_module(M.__name__ + ".gcal")
FIX = pathlib.Path(__file__).parent / "fixtures"
JID, MID, SYD = "probejob0001", "probeamaster0001", "Australia/Sydney"
TUE, WED, THU = "2026-11-10T09:00:00+11:00", "2026-11-11T09:00:00+11:00", "2026-11-12T09:00:00+11:00"


def load(name):
    doc = json.loads((FIX / name).read_text())
    return doc["_source"], doc["data"]


def pjob(**kw):
    base = dict(id=JID, name="hermes-jobs-probe-a renamed", schedule="0 9 * * *", cron_expr="0 9 * * *",
                tz=SYD, rrule="RRULE:FREQ=DAILY", rrule_refusal=None, start=TUE, next_after=WED)
    base.update(kw)
    return job(**base)


def pstate(j=None, **kw):
    j = j or pjob()
    return {JID: {"event_id": MID, "job_hash": P.job_hash(j), "start": TUE, "summary": "hermes-jobs-probe-a renamed",
                  "kind": "recurring", **kw}}


def merged(listed, instances):
    """What sync._events builds: list items, plus per-master moved/cancelled instances not already listed."""
    events = [G.from_google(g) for g in listed]
    seen = {e["id"] for e in events}
    for g in instances:
        e = G.from_google(g)
        e["recurring_event_id"] = e["recurring_event_id"] or MID
        moved = e["original_start"] and e["start"] and e["original_start"] != e["start"]
        if e["id"] not in seen and (e["status"] == "cancelled" or moved):
            events.append(e)
    return events


@unittest.skipUnless((FIX / "captured_master.json").exists(), "captured fixtures not present")
class CapturedPayloadTest(unittest.TestCase):
    def setUp(self):
        self.src, self.master = load("captured_master.json")
        _, self.listed = load("captured_list.json")
        _, self.instances = load("captured_instances.json")
        self.events = merged(self.listed, self.instances)

    def test_provenance_and_no_secrets(self):
        self.assertTrue(self.src.startswith("captured"))
        blob = "".join((FIX / n).read_text() for n in ("captured_master.json", "captured_list.json",
                                                       "captured_instances.json"))
        for word in ("@", "htmlLink", "iCalUID", "etag", "organizer", "creator", "ya29.", "Bearer", "28c2169b1b51"):
            self.assertNotIn(word, blob)

    def test_master_shape(self):
        m = G.from_google(self.master)
        self.assertTrue(B.is_master(m))
        self.assertFalse(B.is_history(m))
        self.assertEqual((m["id"], m["hermes_job_id"], m["tz"]), (MID, JID, SYD))
        self.assertEqual(m["recurrence"], ["RRULE:FREQ=DAILY"])

    def test_deleted_tuesday_and_moved_thursday_are_found(self):
        exc = {e["original_start"]: e for e in self.events if e.get("recurring_event_id") == MID}
        self.assertEqual(exc[TUE]["status"], "cancelled")
        self.assertEqual(exc[THU]["status"], "confirmed")
        self.assertEqual(exc[THU]["start"], "2026-11-12T11:00:00+11:00")

    def test_case_4_deleted_instance_and_case_1_moved_instance(self):
        ops = B.plan([pjob()], self.events, pstate())
        by = {o["action"]: o for o in ops}
        self.assertEqual(sorted(by), ["create_once", "skip_instance"])
        self.assertEqual(by["skip_instance"]["original_slot"], TUE)
        once = by["create_once"]
        self.assertTrue(once["instance_backed"])
        self.assertEqual((once["original_slot"], once["moved_start"]), (THU, "2026-11-12T11:00:00+11:00"))
        self.assertEqual(once["warning"], P.INSTANCE_SKIP_WARNING)

    def test_live_state_moved_copy_recorded_only_notice_remains(self):
        key = P.occurrence_key(JID, THU)
        recs = {key: {"source_job_id": JID, "child_id": "dbaa8859b012", "status": "pending",
                      "fingerprint": P.fingerprint(pjob()), "moved_start": "2026-11-12T11:00:00+11:00",
                      "instance_backed": True}}
        child = job(id="dbaa8859b012", schedule_kind="once", start="2026-11-12T11:00:00+11:00")
        ops = B.plan([pjob(), child], self.events, pstate(), recs)
        self.assertEqual([o["action"] for o in ops], ["skip_instance"])  # sync turns it into info, once

    def test_after_tuesday_everything_is_quiet(self):
        key = P.occurrence_key(JID, THU)
        recs = {key: {"source_job_id": JID, "child_id": "c", "status": "pending", "instance_backed": True,
                      "fingerprint": P.fingerprint(pjob(start=WED)), "moved_start": "2026-11-12T11:00:00+11:00"}}
        self.assertEqual(B.plan([pjob(start=WED)], self.events, pstate(pjob(start=WED)), recs), [])


class DocumentedPayloadTest(unittest.TestCase):
    def test_case_2_all_events_time_change(self):
        src, data = load("documented_all_events.json")
        self.assertTrue(src.startswith("documented, NOT captured"))
        ops = B.plan([pjob()], [G.from_google(g) for g in data], pstate())
        self.assertEqual([(o["action"], o.get("updates")) for o in ops], [("update_job", {"schedule": "0 10 * * *"})])

    def _split(self, variant, start):
        src, data = load("documented_this_and_following.json")
        self.assertTrue(src.startswith("documented, NOT captured"))
        evs = [G.from_google(data["old_master"]), G.from_google(data[variant])]
        return B.plan([pjob(start=start)], evs, pstate(pjob(start=start)))

    def test_case_3_this_and_following_both_variants(self):
        for variant in ("new_series_tagged", "new_series_untagged"):
            with self.subTest(variant=variant):
                ops = self._split(variant, WED)  # Wed still runs on the old rule -> deferred
                self.assertEqual([o["action"] for o in ops], ["adopt_split"])
                op = ops[0]
                self.assertEqual((op["series_id"], op["old_id"], op["cron"], op["cron_now"]),
                                 (MID + "_R20261111T213000", MID, "30 8 * * *", False))
                later = self._split(variant, "2026-11-12T09:00:00+11:00")  # old series over -> now
                self.assertTrue(later[0]["cron_now"])

    def test_case_3_no_duplicate_no_orphan_after_adoption(self):
        _, data = load("documented_this_and_following.json")
        evs = [G.from_google(data["old_master"]), G.from_google(data["new_series_tagged"])]
        moved = pjob(schedule="30 8 * * *", cron_expr="30 8 * * *", start="2026-11-12T08:30:00+11:00")
        st = pstate(moved, event_id=MID + "_R20261111T213000", history_ids=[MID])
        self.assertEqual(B.plan([moved], evs, st), [])


if __name__ == "__main__":
    unittest.main()
