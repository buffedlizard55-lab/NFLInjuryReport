import unittest

from collectors.matching import PlayerIndex


class TestPlayerIndex(unittest.TestCase):
    def setUp(self):
        self.idx = PlayerIndex()
        self.idx.add("Ty Okada", "SEA", "S")
        self.idx.add("Jeremiyah Love", "ARI", "RB")
        self.idx.add("James Thompson Jr.", "SF", "DT")
        self.idx.add("Chris Jones", "KC", "DT")
        self.idx.add("Chris Jones", "NYJ", "CB")

    def test_full_name_match(self):
        self.assertEqual(self.idx.find_in_text("Ty Okada is out")["key"], "ty-okada")

    def test_initial_plus_surname(self):
        self.assertEqual(self.idx.find_in_text("J. Love limited today")["key"],
                         "jeremiyah-love")

    def test_unique_surname(self):
        self.assertEqual(self.idx.find_in_text("Okada ruled out")["key"], "ty-okada")

    def test_jr_suffix_matches(self):
        self.assertEqual(self.idx.find_in_text("James Thompson is limited")["key"],
                         "james-thompson")

    def test_ambiguous_surname_is_not_guessed(self):
        # Two Chris Jones in the league -> no match without a team hint.
        self.assertIsNone(self.idx.find_in_text("Chris Jones is out"))

    def test_ambiguous_surname_resolves_with_team_hint(self):
        rec = self.idx.find_in_text("Chris Jones is out", team_hint="KC")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["team"], "KC")

    def test_two_players_same_name_stay_separate(self):
        self.assertEqual(len(self.idx.candidates_for("Chris Jones")), 2)

    def test_no_match_returns_none(self):
        self.assertIsNone(self.idx.find_in_text("nothing relevant here"))
        self.assertIsNone(self.idx.find_in_text(""))

    def test_round_trip_serialisation(self):
        again = PlayerIndex.from_dict(self.idx.to_dict())
        self.assertEqual(again.find_in_text("Okada ruled out")["key"], "ty-okada")
        self.assertEqual(len(again.players), len(self.idx.players))

    def test_repeat_add_from_another_source_does_not_duplicate(self):
        self.idx.add("Ty Okada", "SEA", "S", source="nfl.com")
        self.idx.add("Ty Okada", "SEA", "S", source="espn")
        rec = self.idx.lookup("Ty Okada", "SEA")
        self.assertEqual(rec["sources"], ["nfl.com", "espn"])
        self.assertEqual(len(self.idx.candidates_for("Ty Okada")), 1)


if __name__ == "__main__":
    unittest.main()
