import unittest

from collectors.models import PlayerInjury
from collectors.reconcile import reconcile


def rec(**kw):
    base = dict(source="nfl.com", team="SEA", player="Ty Okada", observed_at="2026-09-10T18:00:00Z")
    base.update(kw)
    return PlayerInjury(**base)


class TestReconcile(unittest.TestCase):
    def test_official_designation_beats_espn(self):
        official = {"injuries": [rec(game_status="QUESTIONABLE", practice_status="LIMITED")]}
        espn = {"injuries": [rec(source="espn", game_status="OUT",
                                 comment="Okada (hamstring) is out.",
                                 attribution="Brady Henderson",
                                 observed_at="2026-09-10T19:05:00Z")]}
        out = reconcile(official, espn, now="2026-09-10T22:00:00Z")
        p = out["players"][0]
        self.assertEqual(p["game_status"], "QUESTIONABLE")
        # ...but the ESPN detail and attribution are retained for review.
        self.assertEqual(p["attribution"], "Brady Henderson")
        self.assertIn("hamstring", p["comment"])
        self.assertIn("STATUS_CONFLICT", p["discrepancies"])
        self.assertIn("STATUS_CONFLICT", [i["code"] for i in out["irregularities"]])

    def test_missing_official_source_is_a_critical_flag(self):
        espn = {"injuries": [rec(source="espn", game_status="OUT")]}
        out = reconcile(None, espn, now="2026-09-10T22:00:00Z")
        codes = [i["code"] for i in out["irregularities"]]
        self.assertIn("OFFICIAL_SOURCE_MISSING", codes)
        sev = [i["severity"] for i in out["irregularities"] if i["code"] == "OFFICIAL_SOURCE_MISSING"]
        self.assertEqual(sev, ["critical"])

    def test_conflicting_clubs_are_flagged_not_resolved(self):
        official = {"injuries": [rec(team="LAR", player="Aaron Donald", game_status="OUT")]}
        espn = {"injuries": [rec(source="espn", team="CLE", player="Aaron Donald",
                                 game_status="OUT")]}
        out = reconcile(official, espn, now="2026-09-10T22:00:00Z")
        conflicts = [i for i in out["irregularities"] if i["code"] == "PLAYER_TEAM_CONFLICT"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["severity"], "high")
        self.assertIn("CLE", conflicts[0]["detail"])
        self.assertIn("LAR", conflicts[0]["detail"])

    def test_all_32_clubs_appear_even_with_no_injuries(self):
        out = reconcile({"injuries": [rec(game_status="OUT")]}, None,
                        now="2026-09-10T22:00:00Z")
        self.assertEqual(len(out["teams"]), 32)
        sea = next(t for t in out["teams"] if t["code"] == "SEA")
        self.assertEqual(sea["count"], 1)
        self.assertEqual(sea["out"], 1)

    def test_status_change_generates_alert(self):
        previous = {
            "generated_at": "2026-09-10T12:00:00Z",
            "players": [{"team": "SEA", "key": "ty-okada", "name": "Ty Okada",
                         "game_status": "QUESTIONABLE", "position": "S",
                         "injury": "Hamstring"}],
        }
        official = {"injuries": [rec(game_status="OUT", practice_status="DNP",
                                     injury="Hamstring")]}
        out = reconcile(official, None, previous=previous, now="2026-09-10T22:00:00Z")
        self.assertEqual(len(out["alerts"]), 1)
        alert = out["alerts"][0]
        self.assertEqual(alert["kind"], "status-change")
        self.assertEqual(alert["severity"], "critical")
        self.assertEqual(alert["from_status"], "QUESTIONABLE")
        self.assertEqual(alert["to_status"], "OUT")

    def test_clearing_a_player_is_a_cleared_alert(self):
        previous = {
            "generated_at": "2026-09-10T12:00:00Z",
            "players": [{"team": "SEA", "key": "ty-okada", "name": "Ty Okada",
                         "game_status": "OUT", "position": "S", "injury": ""}],
        }
        official = {"injuries": [rec(game_status="ACTIVE", practice_status="FULL")]}
        out = reconcile(official, None, previous=previous, now="2026-09-10T22:00:00Z")
        self.assertEqual(out["alerts"][0]["kind"], "cleared")
        self.assertEqual(out["alerts"][0]["severity"], "high")

    def test_unchanged_status_produces_no_alert(self):
        previous = {
            "generated_at": "2026-09-10T12:00:00Z",
            "players": [{"team": "SEA", "key": "ty-okada", "name": "Ty Okada",
                         "game_status": "OUT", "position": "S", "injury": ""}],
        }
        official = {"injuries": [rec(game_status="OUT")]}
        out = reconcile(official, None, previous=previous, now="2026-09-10T22:00:00Z")
        self.assertEqual(out["alerts"], [])

    def test_rotowire_row_without_team_attaches_by_name(self):
        official = {"injuries": [rec(game_status="QUESTIONABLE")]}
        rotowire = {"injuries": [rec(source="rotowire", team="", game_status="OUT")]}
        out = reconcile(official, None, rotowire, now="2026-09-10T22:00:00Z")
        # Official still wins; the RotoWire row must not create a phantom player.
        self.assertEqual(len(out["players"]), 1)
        self.assertEqual(out["players"][0]["game_status"], "QUESTIONABLE")

    def test_counts_are_computed(self):
        official = {"injuries": [
            rec(player="A One", game_status="OUT"),
            rec(player="B Two", game_status="QUESTIONABLE"),
            rec(player="C Three", game_status="DOUBTFUL"),
        ]}
        out = reconcile(official, None, now="2026-09-10T22:00:00Z")
        self.assertEqual(out["counts"]["players"], 3)
        self.assertEqual(out["counts"]["out"], 1)
        self.assertEqual(out["counts"]["questionable"], 1)
        self.assertEqual(out["counts"]["doubtful"], 1)


if __name__ == "__main__":
    unittest.main()
