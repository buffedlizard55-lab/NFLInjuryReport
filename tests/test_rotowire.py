import unittest

from collectors.rotowire import parse_lineups_html

from .helpers import fixture_text


class TestLineupsParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = parse_lineups_html(fixture_text("rotowire_lineups.html"),
                                        fetched_at="2026-09-10T22:00:00Z")
        cls.by_name = {e.player: e for e in cls.result["injuries"]}

    def test_games_detected_from_logos(self):
        self.assertEqual(len(self.result["games"]), 1)
        self.assertEqual(self.result["games"][0]["teams"], ["NE", "SEA"])

    def test_unflagged_starters_are_not_carried_as_injuries(self):
        # Drake Maye, Hunter Henry and Jadarian Price had no status letter, so
        # they must NOT appear in the injury list.
        for name in ("Drake Maye", "Hunter Henry", "Jadarian Price"):
            self.assertNotIn(name, self.by_name)

    def test_status_letters_are_read(self):
        self.assertEqual(self.by_name["A.J. Brown"].game_status, "QUESTIONABLE")
        self.assertEqual(self.by_name["Sam Darnold"].game_status, "DOUBTFUL")

    def test_inactives_section_implies_out(self):
        self.assertEqual(self.by_name["TreVeyon Henderson"].game_status, "OUT")
        self.assertEqual(self.by_name["Ty Okada"].game_status, "OUT")
        self.assertEqual(self.by_name["Ty Okada"].raw["section"], "inactives")

    def test_rotowire_ids_and_urls(self):
        rec = self.by_name["Ty Okada"]
        self.assertEqual(rec.source_ids["rotowire_id"], "17352")
        self.assertEqual(rec.url,
                         "https://www.rotowire.com/football/player/ty-okada-17352")

    def test_team_is_left_empty_and_flagged_when_ambiguous(self):
        # Two-team game block with no per-row club marker: the parser must not
        # guess a club. Candidates are stored instead.
        rec = self.by_name["TreVeyon Henderson"]
        self.assertEqual(rec.team, "")
        self.assertEqual(rec.raw["team_candidates"], ["NE", "SEA"])
        self.assertIn("RW_TEAM_AMBIGUOUS",
                      [i.code for i in self.result["irregularities"]])

    def test_positions_captured(self):
        self.assertEqual(self.by_name["A.J. Brown"].position, "WR")
        self.assertEqual(self.by_name["Ty Okada"].position, "S")


if __name__ == "__main__":
    unittest.main()
