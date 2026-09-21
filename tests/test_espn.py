import unittest

from collectors.espn import (
    _normalise_date,
    extract_attribution,
    extract_body_part,
    parse_injuries,
)

from .helpers import fixture_json


class TestAttributionExtraction(unittest.TestCase):
    def test_of_reports_pattern_from_live_payload(self):
        text = ("Love (ankle) is warming up ahead of Thursday's practice, "
                "Dani Sureck of the Cardinals' official site reports.")
        name, outlet, _ = extract_attribution(text)
        self.assertEqual(name, "Dani Sureck")
        self.assertEqual(outlet, "Cardinals' official site")

    def test_espn_dot_com_outlet(self):
        name, outlet, _ = extract_attribution(
            "Okada (hamstring) has been ruled out for Thursday, "
            "Brady Henderson of ESPN.com reports."
        )
        self.assertEqual(name, "Brady Henderson")
        self.assertEqual(outlet, "ESPN.com")

    def test_per_pattern(self):
        name, _, _ = extract_attribution("Mahomes (toe) is limited, per Adam Schefter.")
        self.assertEqual(name, "Adam Schefter")

    def test_no_attribution_returns_empty_not_a_guess(self):
        self.assertEqual(extract_attribution("Love (ankle) is warming up."), ("", "", ""))
        self.assertEqual(extract_attribution(""), ("", "", ""))

    def test_handle_capture(self):
        _, _, handle = extract_attribution(
            "Smith (knee) is out, John Smith of the Beat @johnsmithbeat reports."
        )
        self.assertEqual(handle, "johnsmithbeat")


class TestBodyPart(unittest.TestCase):
    def test_parenthetical_after_name(self):
        self.assertEqual(extract_body_part("Love (ankle) is warming up"), "ankle")
        self.assertEqual(extract_body_part("Okada (hamstring) has been ruled out"), "hamstring")

    def test_no_parenthetical(self):
        self.assertEqual(extract_body_part("He will not play."), "")


class TestDateNormalisation(unittest.TestCase):
    def test_espn_minute_precision_gets_seconds(self):
        # Live payload used this exact format.
        self.assertEqual(_normalise_date("2026-09-10T20:48Z"), "2026-09-10T20:48:00Z")

    def test_already_complete_is_untouched(self):
        self.assertEqual(_normalise_date("2026-09-10T22:03:05Z"), "2026-09-10T22:03:05Z")

    def test_empty(self):
        self.assertEqual(_normalise_date(""), "")


class TestParseInjuries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = parse_injuries(fixture_json("espn_injuries.json"),
                                    fetched_at="2026-09-10T22:05:00Z")
        cls.by_name = {i.player: i for i in cls.result["injuries"]}

    def test_source_timestamp_and_season(self):
        self.assertEqual(self.result["source_timestamp"], "2026-09-10T22:03:05Z")
        self.assertEqual(self.result["season"], 2026)

    def test_unmappable_club_is_skipped_and_flagged_not_guessed(self):
        # "MYS"/"Mystery Squad" is not an NFL club code.
        self.assertNotIn("No Body", self.by_name)
        codes = [i.code for i in self.result["irregularities"]]
        self.assertIn("ESPN_TEAM_UNRESOLVED", codes)

    def test_valid_rows_parsed(self):
        self.assertEqual(len(self.result["injuries"]), 2)
        love = self.by_name["Jeremiyah Love"]
        self.assertEqual(love.team, "ARI")
        self.assertEqual(love.position, "RB")
        self.assertEqual(love.game_status, "QUESTIONABLE")
        self.assertEqual(love.injury, "ankle")
        self.assertEqual(love.attribution, "Dani Sureck")
        self.assertEqual(love.observed_at, "2026-09-10T20:48:00Z")

    def test_espn_ids_and_player_link_captured(self):
        love = self.by_name["Jeremiyah Love"]
        self.assertEqual(love.source_ids["espn_athlete_id"], "4870808")
        self.assertEqual(love.source_ids["espn_injury_id"], "636710")
        self.assertEqual(love.url, "https://www.espn.com/nfl/player/_/id/4870808/jeremiyah-love")
        self.assertIn("headshot", love.raw)

    def test_out_status(self):
        self.assertEqual(self.by_name["Ty Okada"].game_status, "OUT")
        self.assertEqual(self.by_name["Ty Okada"].team, "SEA")


class TestEmptyPayloadIsFlagged(unittest.TestCase):
    def test_empty_object(self):
        result = parse_injuries({})
        self.assertEqual(result["injuries"], [])
        self.assertIn("ESPN_EMPTY", [i.code for i in result["irregularities"]])

    def test_non_object_payload(self):
        result = parse_injuries([])
        self.assertIn("ESPN_BAD_SHAPE", [i.code for i in result["irregularities"]])


class TestParseRoster(unittest.TestCase):
    """Game-day roster extraction from ESPN's game summary.

    PROVENANCE: tests/fixtures/espn_summary.json was captured from the live
    endpoint on 2026-09-21 (game 401872945, IND@KC) — the IND block is
    verbatim. The live payload's players live under boxscore.players[].
    statistics[].athletes[] (NOT under boxscore.teams[], which carries only
    team statistics); the parser's teams[].athletes[] branch is a defensive
    fallback, kept and tested below. A shape the parser does not recognise
    degrades to an ESPN_ROSTER_EMPTY flag, never to invented players.
    """

    GAME = {
        "id": "401872945", "short_name": "IND @ KC", "state": "in",
        "teams": [{"code": "KC"}, {"code": "IND"}],
    }

    PAYLOAD = {
        "boxscore": {
            "teams": [
                {"team": {"abbreviation": "KC"},
                 "athletes": [
                     {"displayName": "Patrick Mahomes",
                      "position": {"abbreviation": "QB"}},
                     {"displayName": "Travis Kelce",
                      "position": {"abbreviation": "TE"}},
                     {"displayName": "Some Player", "position": {"id": 9}},
                 ]},
                {"team": {"abbreviation": "IND"},
                 "athletes": [
                     {"displayName": "Anthony Richardson Sr.",
                      "position": {"abbreviation": "QB"}},
                     {"displayName": "", "position": {"abbreviation": "WR"}},
                 ]},
            ]
        }
    }

    def test_rows_carry_team_name_and_position(self):
        from collectors.espn import parse_roster
        rows = parse_roster(self.PAYLOAD, game=self.GAME)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["Patrick Mahomes"]["team"], "KC")
        self.assertEqual(by_name["Patrick Mahomes"]["position"], "QB")
        self.assertEqual(by_name["Anthony Richardson Sr."]["team"], "IND")
        # A position without a readable abbreviation is kept as "" — the player
        # is real, only the position is unknown. Never invented.
        self.assertEqual(by_name["Some Player"]["position"], "")
        # An athlete without a displayName is skipped, never invented.
        self.assertNotIn("", by_name)
        self.assertEqual(len(rows), 4)

    def test_team_falls_back_to_scoreboard_ordering(self):
        from collectors.espn import parse_roster
        payload = {"boxscore": {"teams": [
            {"athletes": [{"displayName": "Player One",
                           "position": {"abbreviation": "WR"}}]},
            {"athletes": [{"displayName": "Player Two",
                           "position": {"abbreviation": "WR"}}]},
        ]}}
        rows = parse_roster(payload, game=self.GAME)
        self.assertEqual([(r["name"], r["team"]) for r in rows],
                         [("Player One", "KC"), ("Player Two", "IND")])

    def test_malformed_payload_is_empty_not_an_exception(self):
        from collectors.espn import parse_roster
        self.assertEqual(parse_roster(None, game=self.GAME), [])
        self.assertEqual(parse_roster({}, game=self.GAME), [])
        self.assertEqual(parse_roster({"boxscore": "garbage"}, game=self.GAME), [])


class TestParseRosterLiveShape(unittest.TestCase):
    """The shape the live payload ACTUALLY carries (2026-09-21 capture).

    boxscore.teams[] has NO athletes; the players are in boxscore.players[]
    (one block per team, each with its own team.abbreviation) inside
    statistics[] groups. Daniel Jones appears in both the passing and the
    rushing groups of the capture and must yield exactly one row.
    """

    GAME = {
        "id": "401872945", "short_name": "IND @ KC", "state": "in",
        "teams": [{"code": "IND"}, {"code": "KC"}],
    }

    def test_live_payload_parses_every_boxscore_player(self):
        from collectors.espn import parse_roster
        rows = parse_roster(fixture_json("espn_summary.json"), game=self.GAME)
        by_team = {}
        for r in rows:
            by_team.setdefault(r["team"], {})[r["name"]] = r
        # IND block (verbatim from the capture): all four boxscore players.
        self.assertEqual(
            set(by_team["IND"]),
            {"Daniel Jones", "Jonathan Taylor", "Deion Burks", "Tyler Warren"})
        # KC block: same structure.
        self.assertEqual(set(by_team["KC"]), {"Patrick Mahomes", "Isaiah Poe"})
        # The live shape carries no position objects -> "", never invented.
        self.assertEqual(by_team["IND"]["Daniel Jones"]["position"], "")
        self.assertEqual(len(rows), 6)

    def test_duplicate_stat_lines_dedupe_to_one_row(self):
        from collectors.espn import parse_roster
        rows = parse_roster(fixture_json("espn_summary.json"), game=self.GAME)
        jones = [r for r in rows if r["name"] == "Daniel Jones"]
        self.assertEqual(len(jones), 1,
                         "a player with passing AND rushing lines appears once")


if __name__ == "__main__":
    unittest.main()
