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

    def test_team_hint_rejects_a_name_from_another_club(self):
        # A club named in the source must constrain matching. It must not fall
        # back to the only same-surname player from another club.
        self.assertIsNone(self.idx.find_in_text("Patriots injury: Jones is out",
                                               team_hint="NE"))

    def test_full_name_requires_token_boundaries(self):
        self.assertEqual(self.idx.full_name_hits("Chris Jones is out"),
                         ["KC:chris-jones", "NYJ:chris-jones"])
        self.assertEqual(self.idx.full_name_hits("Chris Joneson is out"), [])

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


class TestFullNameSuppressesSurnameMisread(unittest.TestCase):
    """The 2026-09-21 incident: 'Tyson Bagent Enters' used to match the FIRST
    name "Tyson" to a player surnamed Tyson, mis-attributing a Bears/Vikings
    headline to a Saint. With a known full name present, the surname step must
    not run at all."""

    def setUp(self):
        self.idx = PlayerIndex()
        self.idx.add("Caleb Williams", "CHI", "QB")
        self.idx.add("Tyson Bagent", "CHI", "QB")
        self.idx.add("Jordyn Tyson", "NO", "WR")

    def test_first_name_is_not_a_surname_match(self):
        text = ("BREAKING: Caleb Williams Leaves Bears-Vikings Game With Injury; "
                "Tyson Bagent Enters")
        self.assertIsNone(self.idx.find_in_text(text, team_hint=""))

    def test_team_hint_still_resolves_a_single_full_name(self):
        # One full name in the text: the hint resolves it as before.
        rec = self.idx.find_in_text("Caleb Williams Leaves Game With Injury",
                                    team_hint="CHI")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["team"], "CHI")
        self.assertEqual(rec["key"], "caleb-williams")

    def test_two_full_names_stay_ambiguous_even_with_a_hint(self):
        # Both names are CHI; the hint cannot pick one, so it must not guess.
        text = ("BREAKING: Caleb Williams Leaves Bears-Vikings Game With Injury; "
                "Tyson Bagent Enters")
        self.assertIsNone(self.idx.find_in_text(text, team_hint="CHI"))

    def test_unique_surname_still_works_when_no_full_name_present(self):
        self.idx.add("Ty Okada", "SEA", "S")
        rec = self.idx.find_in_text("Okada ruled out")
        self.assertEqual(rec["key"], "ty-okada")


class TestPruning(unittest.TestCase):
    """Stale (club, player) records must not persist forever. The motivating
    incident (archive report-2131.json, 2026-09-16): the official report listed
    Aaron Banks under BOTH HOU and GB for one run; every later run listed him
    under GB only, but the index kept the HOU record and kept emitting
    HOU:aaron-banks in-game events until the index was pruned."""

    NOW = "2026-09-21T02:24:00Z"

    def setUp(self):
        self.idx = PlayerIndex()
        self.idx.add("Aaron Banks", "GB", "G", seen_at="2026-09-21T01:00:00Z")
        self.idx.add("Aaron Banks", "HOU", "G", seen_at="2026-09-16T21:31:00Z")
        self.idx.add("Kyler Murray", "MIN", "QB", seen_at="2026-09-21T01:00:00Z")
        self.idx.add("Kyler Murray", "PHI", "QB", seen_at="2026-09-16T21:31:00Z")
        self.idx.add("Justin Jefferson", "CLE", "WR", seen_at="2026-09-21T01:00:00Z")
        self.idx.add("Justin Jefferson", "MIN", "WR", seen_at="2026-09-21T01:00:00Z")

    def test_contradicted_club_dropped_when_no_source_asserts_it(self):
        # Official says Aaron Banks is GB; nothing still asserts HOU -> HOU gone.
        dropped = self.idx.prune(
            now=self.NOW,
            official_pairs=[("GB", "aaron-banks"), ("MIN", "kyler-murray"),
                            ("CLE", "justin-jefferson")],
            asserted_pairs={("GB", "aaron-banks"), ("MIN", "kyler-murray"),
                            ("CLE", "justin-jefferson")},
        )
        self.assertIn("HOU:aaron-banks", [d.split(" ")[0] for d in dropped])
        self.assertNotIn("HOU:aaron-banks", self.idx.players)
        self.assertIn("GB:aaron-banks", self.idx.players)
        # Kyler Murray: official says MIN, nothing asserts PHI -> PHI gone.
        self.assertNotIn("PHI:kyler-murray", self.idx.players)

    def test_genuine_cross_source_conflict_keeps_both(self):
        # Justin Jefferson: official says CLE, ESPN still asserts MIN -> keep.
        dropped = self.idx.prune(
            now=self.NOW,
            official_pairs=[("CLE", "justin-jefferson")],
            asserted_pairs={("CLE", "justin-jefferson"),
                            ("MIN", "justin-jefferson")},
        )
        self.assertNotIn("MIN:justin-jefferson", [d.split(" ")[0] for d in dropped])
        self.assertIn("MIN:justin-jefferson", self.idx.players)

    def test_stale_record_pruned_after_14_days(self):
        self.idx.add("Old Player", "NE", "WR", seen_at="2026-09-01T00:00:00Z")
        dropped = self.idx.prune(now=self.NOW, official_pairs=[],
                                 asserted_pairs=set())
        self.assertIn("NE:old-player", [d.split(" ")[0] for d in dropped])

    def test_recently_asserted_record_survives(self):
        dropped = self.idx.prune(now=self.NOW, official_pairs=[],
                                 asserted_pairs=set())
        self.assertNotIn("GB:aaron-banks", [d.split(" ")[0] for d in dropped])

    def test_record_without_last_seen_is_backfilled_not_dropped(self):
        self.idx.add("New Player", "NE", "WR")  # no seen_at (legacy record)
        dropped = self.idx.prune(now=self.NOW, official_pairs=[],
                                 asserted_pairs=set())
        self.assertNotIn("NE:new-player", [d.split(" ")[0] for d in dropped])
        self.assertEqual(self.idx.players["NE:new-player"]["last_seen"], self.NOW)

    def test_no_official_report_means_no_contradiction_prune(self):
        dropped = self.idx.prune(now=self.NOW, official_pairs=[],
                                 asserted_pairs=set())
        # Only the 14-day-stale rule can fire; all records here are recent.
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()
