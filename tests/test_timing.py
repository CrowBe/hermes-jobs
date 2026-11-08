import importlib.util
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from test_runtime import M


@unittest.skipUnless(importlib.util.find_spec("croniter"), "Hermes runtime supplies croniter")
class TimingTest(unittest.TestCase):
    def test_daily_wall_clock_stays_at_nine_across_sydney_dst(self):
        raw = {"schedule": {"kind": "cron", "expr": "0 9 * * *"}}
        with patch.object(M, "_job_zone", return_value=ZoneInfo("Australia/Sydney")):
            self.assertEqual(M._following_start(raw, "2026-04-04T09:00:00+11:00"), "2026-04-05T09:00:00+10:00")

    def test_interval_keeps_elapsed_duration_across_dst(self):
        raw = {"schedule": {"kind": "interval", "minutes": 120}}
        with patch.object(M, "_job_zone", return_value=ZoneInfo("Australia/Sydney")):
            self.assertEqual(M._following_start(raw, "2026-04-05T01:00:00+11:00"), "2026-04-05T02:00:00+10:00")

    def test_utc_stored_start_matches_pinned_wall_clock(self):
        raw = {"schedule": {"kind": "cron", "expr": "0 9 * * *"}}
        with patch.object(M, "_job_zone", return_value=ZoneInfo("Australia/Sydney")):
            self.assertEqual(M._following_start(raw, "2026-11-08T22:00:00+00:00"), "2026-11-10T09:00:00+11:00")
