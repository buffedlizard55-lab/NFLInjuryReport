"""In-game alerts and the append-only alert log.

The behaviour under test is the fix for the 2026-09-17 miss: an in-game injury must
(a) raise an alert on its own axis, (b) survive in the log so someone who opens the
page hours later still sees it, and (c) not be re-raised as a fresh alert every run.
"""

import unittest

from collectors import espn as espn_mod
from collectors import nfl_com as nfl_mod
from collectors.reconcile import (
    ALERT_LOG_MAX,
    IN_GAME_ALERT_WINDOW_HOURS,
    reconcile,
)

from .helpers import fixture_json, fixture_text

MOORE_EVENT = {
    "event_key": "BUF:dj-moore:OUT_FOR_GAME",
    "team": "BUF", "player": "DJ Moore", "player_key": "dj-moore", "position": "WR",
    "in_game_status": "OUT_FOR_GAME", "event": "ruled-out-for-game", "injury": "shoulder",
    "severity": "critical", "availability_class": "UNAVAILABLE",
    "evidence": ["Moore (shoulder) has been ruled out for the remainder of Thursday "
                 "night's game against the Lions."],
    "reported_at": "2026-09-18T02:12:00Z",
    "first_seen_at": "2026-09-18T02:17:00Z",
    "detection_latency_seconds": 300,
    "sources": [{"source": "espn", "platform": "espn-injuries", "author": "ESPN",
                 "url": "https://www.espn.com/nfl/player/_/id/3915416/dj-moore",
                 "posted_at": "2026-09-18T02:12:00Z", "verified": False,
                 "verification_detail": "", "stated_status": "OUT_FOR_GAME"}],
    "source_verified": False,
    "in_game_window": True,
}


class TestInGameAlerts(unittest.TestCase):
    def setUp(self):
        self.official = nfl_mod.parse_injuries_html(fixture_text("nfl_injuries.html"),
                                                    fetched_at="2026-09-18T02:17:00Z")
        self.espn = espn_mod.parse_injuries(fixture_json("espn_injuries.json"),
                                            fetched_at="2026-09-18T02:17:00Z")

    def test_event_raises_an_in_game_alert_with_its_evidence(self):
        report = reconcile(self.official, self.espn, previous=None,
                           now="2026-09-18T02:17:00Z", game_events=[MOORE_EVENT])
        ingame = [a for a in report["alerts"] if a["kind"] == "in-game"]
        self.assertEqual(len(ingame), 1)
        alert = ingame[0]
        self.assertEqual(alert["severity"], "critical")
        self.assertEqual(alert["player"], "DJ Moore")
        self.assertEqual(alert["to_status"], "OUT_FOR_GAME")
        self.assertEqual(alert["alert_id"], "ingame:BUF:dj-moore:OUT_FOR_GAME")
        self.assertEqual(alert["first_seen_at"], "2026-09-18T02:17:00Z")
        self.assertEqual(alert["detection_latency_seconds"], 300)
        self.assertIn("remainder of Thursday night's game", alert["detail"])

    def test_report_carries_events_and_games_for_the_site(self):
        report = reconcile(self.official, self.espn, previous=None,
                           now="2026-09-18T02:17:00Z", game_events=[MOORE_EVENT],
                           live_games=[{"id": "401872932", "state": "post"}])
        self.assertEqual(len(report["game_events"]), 1)
        self.assertEqual(report["live_games"][0]["id"], "401872932")
        self.assertEqual(report["counts"]["in_game_events"], 1)
        self.assertEqual(report["counts"]["in_game_alerts"], 1)

    def test_event_older_than_the_window_is_not_re_alerted(self):
        old = dict(MOORE_EVENT, reported_at="2026-09-10T02:12:00Z",
                   first_seen_at="2026-09-10T02:17:00Z")
        report = reconcile(self.official, self.espn, previous=None,
                           now="2026-09-18T02:17:00Z", game_events=[old])
        self.assertEqual([a for a in report["alerts"] if a["kind"] == "in-game"], [])
        # ...but it is still in the events feed, just not an alert.
        self.assertEqual(len(report["game_events"]), 1)
        self.assertLess(IN_GAME_ALERT_WINDOW_HOURS, 24)

    def test_alert_log_persists_and_does_not_duplicate(self):
        first = reconcile(self.official, self.espn, previous=None,
                          now="2026-09-18T02:17:00Z", game_events=[MOORE_EVENT])
        log = first["alert_log"]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["alert_id"], "ingame:BUF:dj-moore:OUT_FOR_GAME")

        # Second run, 4 minutes later, same event (ESPN updated the same row).
        later_event = dict(MOORE_EVENT, first_seen_at="2026-09-18T02:21:00Z",
                           detection_latency_seconds=540)
        second = reconcile(self.official, self.espn, previous=None,
                           now="2026-09-18T02:21:00Z", game_events=[later_event],
                           alert_log=log)
        matching = [row for row in second["alert_log"]
                    if row["alert_id"] == "ingame:BUF:dj-moore:OUT_FOR_GAME"]
        self.assertEqual(len(matching), 1)
        # The FIRST sighting is kept: that is the honest latency, not the refresh.
        self.assertEqual(matching[0]["first_seen_at"], "2026-09-18T02:17:00Z")
        self.assertEqual(matching[0]["detection_latency_seconds"], 540)

    def test_log_survives_a_run_where_the_event_is_no_longer_reported(self):
        first = reconcile(self.official, self.espn, previous=None,
                          now="2026-09-18T02:17:00Z", game_events=[MOORE_EVENT])
        quiet = reconcile(self.official, self.espn, previous=None,
                         now="2026-09-18T09:00:00Z", game_events=[],
                         alert_log=first["alert_log"])
        self.assertEqual(len(quiet["alert_log"]), 1)
        self.assertEqual(quiet["alert_log"][0]["player"], "DJ Moore")

    def test_log_is_pruned_after_the_retention_window(self):
        first = reconcile(self.official, self.espn, previous=None,
                          now="2026-09-18T02:17:00Z", game_events=[MOORE_EVENT])
        stale = reconcile(self.official, self.espn, previous=None,
                          now="2026-09-25T02:17:00Z", game_events=[],
                          alert_log=first["alert_log"])
        self.assertEqual(stale["alert_log"], [])

    def test_log_is_capped(self):
        rows = [dict(MOORE_EVENT, event_key=f"BUF:p{i}:OUT_FOR_GAME",
                     alert_id=f"ingame:BUF:p{i}:OUT_FOR_GAME", player=f"Player {i}",
                     ts="2026-09-18T02:12:00Z")
                for i in range(ALERT_LOG_MAX + 25)]
        report = reconcile(self.official, self.espn, previous=None,
                           now="2026-09-18T02:17:00Z", game_events=[], alert_log=rows)
        self.assertEqual(len(report["alert_log"]), ALERT_LOG_MAX)


if __name__ == "__main__":
    unittest.main()
