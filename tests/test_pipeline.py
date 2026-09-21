"""End-to-end test of the pipeline with the network stubbed to local fixtures.

This is the check that the whole orchestration path runs: collect_sources ->
reconcile -> build_claims -> resolve_claims -> apply_to_registry -> the JSON files
the GitHub Pages site consumes.
"""

import argparse
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from collectors import directory as directory_mod
from collectors import espn as espn_mod
from collectors import nfl_com as nfl_mod
from collectors import pipeline
from collectors import rotowire as rotowire_mod
from collectors import social as social_mod
from collectors.matching import PlayerIndex

from .helpers import fixture_json, fixture_text


class TestPipelineEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ("LATEST_DIR", "STATE_DIR", "ARCHIVE_DIR"):
            self.addCleanup(setattr, pipeline, name, getattr(pipeline, name))
        pipeline.DATA_DIR = self.tmp
        pipeline.LATEST_DIR = os.path.join(self.tmp, "latest")
        pipeline.STATE_DIR = os.path.join(self.tmp, "state")
        pipeline.ARCHIVE_DIR = os.path.join(self.tmp, "archive")

        self.official = nfl_mod.parse_injuries_html(
            fixture_text("nfl_injuries.html"), fetched_at="2026-09-10T22:00:00Z")
        self.espn = espn_mod.parse_injuries(
            fixture_json("espn_injuries.json"), fetched_at="2026-09-10T22:00:00Z")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *, with_social=False):
        # Every network call is stubbed: the sandbox and CI both run these tests
        # offline, and a live call would make the suite depend on upstream uptime.
        empty_news = {"items": [], "irregularities": []}
        with mock.patch.object(nfl_mod, "collect", return_value=self.official), \
             mock.patch.object(espn_mod, "collect", return_value=self.espn), \
             mock.patch.object(espn_mod, "collect_scoreboard",
                               return_value={"games": [], "live": [], "hot_teams": [],
                                             "count": 0}), \
             mock.patch.object(espn_mod, "collect_news", return_value=dict(empty_news)), \
             mock.patch.object(rotowire_mod, "collect_news",
                               return_value=dict(empty_news)), \
             mock.patch.object(directory_mod, "build",
                               return_value={"irregularities": []}), \
             mock.patch.object(pipeline, "verify", return_value={"sources": []}):
            args = argparse.Namespace(with_rotowire=False, no_social=not with_social)
            return pipeline.collect(args)

    def test_exit_code_zero_and_files_written(self):
        self.assertEqual(self._run(), 0)
        for name in ("report.json", "alerts.json", "flags.json", "social.json",
                     "scorecard.json", "claims.json", "meta.json"):
            path = os.path.join(pipeline.LATEST_DIR, name)
            self.assertTrue(os.path.exists(path), f"{name} was not written")
            with open(path, "r", encoding="utf-8") as fh:
                json.load(fh)  # must be valid JSON

    def test_report_contents(self):
        self._run()
        with open(os.path.join(pipeline.LATEST_DIR, "report.json"), encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["season"], 2026)
        self.assertEqual(report["week"], 1)
        self.assertEqual(len(report["teams"]), 32)
        names = {p["name"] for p in report["players"]}
        self.assertIn("Ty Okada", names)
        self.assertIn("TreVeyon Henderson", names)
        self.assertIn("Aaron Donald", names)
        # Official designation must win over ESPN's differing one.
        okada = next(p for p in report["players"] if p["name"] == "Ty Okada")
        self.assertEqual(okada["game_status"], "OUT")
        self.assertEqual(okada["team"], "SEA")

    def test_espn_attribution_survives_to_the_report(self):
        self._run()
        with open(os.path.join(pipeline.LATEST_DIR, "report.json"), encoding="utf-8") as fh:
            report = json.load(fh)
        love = next(p for p in report["players"] if p["name"] == "Jeremiyah Love")
        self.assertEqual(love["attribution"], "Dani Sureck")
        self.assertEqual(love["outlet"], "Cardinals' official site")
        self.assertEqual(love["team"], "ARI")

    def test_state_grows_so_the_next_run_has_a_roster(self):
        self._run()
        with open(os.path.join(pipeline.STATE_DIR, "players.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertGreater(state["count"], 0)
        keys = list(state["players"])
        self.assertIn("SEA:ty-okada", keys)

    def test_second_run_diffs_against_the_first_and_emits_alerts(self):
        self._run()
        # Simulate the official report upgrading a player from OUT to ACTIVE.
        for rec in self.official["injuries"]:
            if rec.player == "Ty Okada":
                rec.game_status = "ACTIVE"
                rec.practice_status = "FULL"
        self._run()
        with open(os.path.join(pipeline.LATEST_DIR, "alerts.json"), encoding="utf-8") as fh:
            alerts = json.load(fh)["alerts"]
        kinds = {(a["player"], a["kind"], a["to_status"]) for a in alerts}
        self.assertIn(("Ty Okada", "cleared", "ACTIVE"), kinds)

    def test_archive_snapshot_written(self):
        self._run()
        found = []
        for root, _dirs, files in os.walk(pipeline.ARCHIVE_DIR):
            found.extend(files)
        self.assertTrue(any(f.startswith("report-") for f in found))

    def test_scorecard_is_created_even_with_no_social_posts(self):
        self._run()
        with open(os.path.join(pipeline.LATEST_DIR, "scorecard.json"), encoding="utf-8") as fh:
            card = json.load(fh)
        # ESPN attributions alone must populate the registry.
        self.assertGreaterEqual(card["count"], 1)
        self.assertIn("summary", card)

    def test_source_failure_is_recorded_not_fatal(self):
        from collectors.http import FetchError

        with mock.patch.object(nfl_mod, "collect",
                               side_effect=FetchError("https://www.nfl.com/injuries/",
                                                      "HTTP 503 Service Unavailable", 503)), \
             mock.patch.object(espn_mod, "collect", return_value=self.espn), \
             mock.patch.object(directory_mod, "build",
                               return_value={"irregularities": []}), \
             mock.patch.object(pipeline, "verify", return_value={"sources": []}):
            args = argparse.Namespace(with_rotowire=False, no_social=True)
            code = pipeline.collect(args)
        self.assertEqual(code, 0, "one source outage must not fail the whole run")
        with open(os.path.join(pipeline.LATEST_DIR, "flags.json"), encoding="utf-8") as fh:
            flags = json.load(fh)
        self.assertEqual(flags["source_errors"][0]["source"], "nfl.com")
        self.assertIn("OFFICIAL_SOURCE_MISSING",
                      [i["code"] for i in flags["irregularities"]])

    def test_total_failure_returns_exit_code_2(self):
        from collectors.http import FetchError

        with mock.patch.object(nfl_mod, "collect", side_effect=FetchError("u", "down")), \
             mock.patch.object(espn_mod, "collect", side_effect=FetchError("u", "down")), \
             mock.patch.object(directory_mod, "build",
                               return_value={"irregularities": []}), \
             mock.patch.object(pipeline, "verify", return_value={"sources": []}):
            args = argparse.Namespace(with_rotowire=False, no_social=True)
            self.assertEqual(pipeline.collect(args), 2)


if __name__ == "__main__":
    unittest.main()

class TestInGameWiring(unittest.TestCase):
    """End-to-end: a verified insider post must reach the alert log and ingame.json.

    This is the 2026-09-17 regression test. The fixture is Ian Rapoport's actual
    Bluesky post about DJ Moore's shoulder ("questionable to return ... X-ray"),
    fetched live on 2026-09-18; the network is stubbed, the parsers are real.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(setattr, pipeline, "DATA_DIR", pipeline.DATA_DIR)
        self.addCleanup(setattr, pipeline, "LATEST_DIR", pipeline.LATEST_DIR)
        self.addCleanup(setattr, pipeline, "STATE_DIR", pipeline.STATE_DIR)
        self.addCleanup(setattr, pipeline, "ARCHIVE_DIR", pipeline.ARCHIVE_DIR)
        pipeline.DATA_DIR = self.tmp
        pipeline.LATEST_DIR = os.path.join(self.tmp, "latest")
        pipeline.STATE_DIR = os.path.join(self.tmp, "state")
        pipeline.ARCHIVE_DIR = os.path.join(self.tmp, "archive")
        os.makedirs(pipeline.STATE_DIR, exist_ok=True)
        os.makedirs(pipeline.LATEST_DIR, exist_ok=True)

        self.official = nfl_mod.parse_injuries_html(
            fixture_text("nfl_injuries.html"), fetched_at="2026-09-18T02:17:00Z")
        self.espn = espn_mod.parse_injuries(
            fixture_json("espn_injuries.json"), fetched_at="2026-09-18T02:17:00Z")

        # Roster state: the club index the run starts from.
        index = PlayerIndex()
        index.add("DJ Moore", "BUF", "WR",
                  "https://www.espn.com/nfl/player/_/id/3915416/dj-moore", "espn")
        index.add("Keon Coleman", "BUF", "WR",
                  "https://www.espn.com/nfl/player/_/id/4635008/keon-coleman", "espn")
        pipeline._write(os.path.join(pipeline.STATE_DIR, "players.json"), index.to_dict())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        probes = {
            "bluesky": {"url": "u", "reachable": False, "status": 403, "error": "HTTP 403",
                        "latency_ms": 10},
            "bluesky-author-feed": {"url": "u", "reachable": True, "status": 200,
                                    "error": "", "latency_ms": 10},
            "mastodon": {"url": "u", "reachable": False, "status": None, "error": "down",
                         "latency_ms": 10},
            "google-news": {"url": "u", "reachable": True, "status": 200, "error": "",
                            "latency_ms": 10},
            "reddit": {"url": "u", "reachable": False, "status": 403, "error": "blocked",
                       "latency_ms": 10},
        }
        empty_rss = "<rss><channel></channel></rss>"
        with mock.patch.object(pipeline, "_now", return_value="2026-09-18T02:17:00Z"), \
             mock.patch.object(nfl_mod, "collect", return_value=self.official), \
             mock.patch.object(espn_mod, "collect", return_value=self.espn), \
             mock.patch.object(espn_mod, "collect_scoreboard",
                               return_value={"games": [{"id": "401872932", "state": "in",
                                                        "teams": []}],
                                             "live": [{"id": "401872932"}],
                                             "hot_teams": ["BUF", "DET"], "count": 1}), \
             mock.patch.object(espn_mod, "collect_news",
                               return_value=espn_mod.parse_news(
                                   fixture_json("espn_news.json"),
                                   fetched_at="2026-09-18T02:17:00Z")), \
             mock.patch.object(rotowire_mod, "collect_news",
                               return_value=rotowire_mod.parse_news_rss(
                                   fixture_text("rotowire_news.xml"),
                                   fetched_at="2026-09-18T02:17:00Z")), \
             mock.patch.object(directory_mod, "build",
                               return_value={"irregularities": []}), \
             mock.patch.object(pipeline, "verify", return_value={"sources": []}), \
             mock.patch.object(social_mod, "probe_platforms", return_value=probes), \
             mock.patch.object(social_mod, "fetch_json",
                               return_value=fixture_json("bsky_author_feed.json")), \
             mock.patch.object(social_mod, "fetch_text", return_value=empty_rss):
            args = argparse.Namespace(with_rotowire=False, no_social=False, no_news=False)
            return pipeline.collect(args)

    def test_ingame_event_and_alert_are_published(self):
        self.assertEqual(self._run(), 0)
        with open(os.path.join(pipeline.LATEST_DIR, "ingame.json"), encoding="utf-8") as fh:
            ingame = json.load(fh)
        events = {e["player_key"]: e for e in ingame["events"]}
        self.assertIn("dj-moore", events)
        moore = events["dj-moore"]
        self.assertEqual(moore["team"], "BUF")
        self.assertEqual(moore["in_game_status"], "RETURN_QUESTIONABLE")
        self.assertTrue(moore["source_verified"])
        self.assertIn("questionable to return", " ".join(moore["evidence"]))
        # The seatbelt: the account is only "verified" because the payload said so,
        # and the provenance class is carried through to the event.
        self.assertEqual(moore["sources"][0]["source_kind"], "verified-insider")
        self.assertEqual(moore["sources"][0]["author"], "Ian Rapoport")
        self.assertTrue(moore["sources"][0]["verified"])

        with open(os.path.join(pipeline.LATEST_DIR, "alerts.json"), encoding="utf-8") as fh:
            alerts = json.load(fh)
        self.assertTrue(alerts["log"], "the alert log must survive the run")
        ingame_alerts = [a for a in alerts["log"] if a["kind"] == "in-game"]
        self.assertTrue(any(a["player"] == "DJ Moore" for a in ingame_alerts))

    def test_stat_recaps_do_not_become_injury_events(self):
        # The RotoWire wire is real (five verbatim items, all performance
        # recaps). None of them may reach the in-game feed: a 2-catch night is
        # not an injury, and turning one into an alert would be exactly the
        # fabrication this project is built to avoid.
        self._run()
        with open(os.path.join(pipeline.LATEST_DIR, "ingame.json"), encoding="utf-8") as fh:
            ingame = json.load(fh)
        recap_players = {"Jameson Williams", "Sam LaPorta", "Dalton Kincaid",
                         "Jared Goff", "Amon-Ra St. Brown"}
        named = {ev["player"] for ev in ingame["events"]}
        self.assertFalse(named & recap_players,
                         "a stat recap must never become an in-game injury event")
        for ev in ingame["events"]:
            self.assertTrue(ev.get("evidence"), "every event carries its sentence")

    def test_meta_publishes_measured_cadence_and_live_window(self):
        self._run()
        with open(os.path.join(pipeline.LATEST_DIR, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertIn("cadence", meta)
        self.assertEqual(meta["live"]["hot_teams"], ["BUF", "DET"])
        self.assertTrue(meta["live"]["live_games"] >= 1)

    def test_social_payload_records_which_handles_were_read(self):
        self._run()
        with open(os.path.join(pipeline.LATEST_DIR, "social.json"), encoding="utf-8") as fh:
            social = json.load(fh)
        self.assertIn("watched_handles", social)
        self.assertIn("fetches", social)
        labels = [f["label"] for f in social["fetches"]]
        self.assertTrue(any(label.startswith("author:") for label in labels))


if __name__ == "__main__":
    unittest.main()
