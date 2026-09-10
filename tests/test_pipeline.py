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

from collectors import espn as espn_mod
from collectors import nfl_com as nfl_mod
from collectors import pipeline

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
        with mock.patch.object(nfl_mod, "collect", return_value=self.official), \
             mock.patch.object(espn_mod, "collect", return_value=self.espn), \
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
             mock.patch.object(pipeline, "verify", return_value={"sources": []}):
            args = argparse.Namespace(with_rotowire=False, no_social=True)
            self.assertEqual(pipeline.collect(args), 2)


if __name__ == "__main__":
    unittest.main()
