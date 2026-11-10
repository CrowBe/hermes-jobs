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


class JobZoneSourceTest(unittest.TestCase):
    """_job_zone: job tz, then Hermes' profile-aware helper, else the old env/config read."""

    def test_job_timezone_wins(self):
        import types, sys
        fake = types.SimpleNamespace(get_timezone_name=lambda: "UTC")
        with patch.dict(sys.modules, {"hermes_time": fake}):
            self.assertEqual(M._job_zone({"timezone": "Asia/Tokyo"}).key, "Asia/Tokyo")

    def test_uses_profile_aware_helper_not_bare_env(self):
        import os, types, sys
        fake = types.SimpleNamespace(get_timezone_name=lambda: "Australia/Sydney")
        with patch.dict(sys.modules, {"hermes_time": fake}), patch.dict(os.environ, {"HERMES_TIMEZONE": "UTC"}):
            self.assertEqual(M._job_zone({}).key, "Australia/Sydney")

    def test_falls_back_to_env_when_helper_missing(self):
        import os, sys
        with patch.dict(sys.modules, {"hermes_time": None}), patch.dict(os.environ, {"HERMES_TIMEZONE": "Europe/Berlin"}):
            self.assertEqual(M._hermes_timezone_name(), "Europe/Berlin")
            self.assertEqual(M._job_zone({}).key, "Europe/Berlin")
