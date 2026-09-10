import unittest

from collectors.nfl_com import (
    _bucket_markers,
    _club_code_from_url,
    parse_injuries_html,
)

from .helpers import fixture_text


class TestOfficialReportParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = parse_injuries_html(
            fixture_text("nfl_injuries.html"), fetched_at="2026-09-10T22:00:00Z"
        )
        cls.by_name = {i.player: i for i in cls.result["injuries"]}

    def test_season_and_week_come_from_the_page_title(self):
        self.assertEqual(self.result["season"], 2026)
        self.assertEqual(self.result["week"], 1)

    def test_all_rows_parsed(self):
        # 3 NE + 3 SEA + 2 SF + 1 LAR
        self.assertEqual(len(self.result["injuries"]), 9)
        self.assertEqual(self.result["injury_tables"], 4)

    def test_tables_attributed_to_the_correct_club(self):
        self.assertEqual(self.by_name["Ben Brown"].team, "NE")
        self.assertEqual(self.by_name["Ty Okada"].team, "SEA")
        self.assertEqual(self.by_name["Alfred Collins"].team, "SF")

    def test_legacy_la_code_is_normalised_to_lar(self):
        self.assertEqual(_club_code_from_url(
            "https://static.www.nfl.com/x/league/api/clubs/logos/LA"), "LAR")
        self.assertEqual(self.by_name["Aaron Donald"].team, "LAR")

    def test_designations_and_bodies_are_read(self):
        rec = self.by_name["TreVeyon Henderson"]
        self.assertEqual(rec.game_status, "OUT")
        self.assertEqual(rec.practice_status, "DNP")
        self.assertEqual(rec.injury, "Ankle")
        self.assertEqual(rec.position, "RB")

    def test_blank_designation_with_practice_means_active(self):
        # Christian Barmore practised fully and has no Game Status cell. nfl.com
        # means "available"; leaving it UNKNOWN would under-report health.
        rec = self.by_name["Christian Barmore"]
        self.assertEqual(rec.practice_status, "FULL")
        self.assertEqual(rec.game_status, "ACTIVE")

    def test_designation_provenance_is_recorded(self):
        # Ty Okada has a printed "Out" -> published. Christian Barmore has a blank
        # Game Status and only a practice line -> our inference, tagged as such.
        self.assertEqual(self.by_name["Ty Okada"].designation_source, "published")
        self.assertEqual(self.by_name["Christian Barmore"].designation_source, "inferred")

    def test_player_deep_links_are_captured_for_manual_review(self):
        self.assertEqual(
            self.by_name["Ty Okada"].url,
            "https://www.nfl.com/players/ty-okada/",
        )
        self.assertEqual(self.by_name["Ty Okada"].source_ids["nfl_slug"], "ty-okada")

    def test_game_date_is_used_as_observed_at(self):
        self.assertEqual(self.by_name["Ben Brown"].observed_at, "2026-09-09T00:00:00Z")
        self.assertEqual(self.by_name["Alfred Collins"].observed_at, "2026-09-10T00:00:00Z")

    def test_script_contents_are_not_treated_as_rows(self):
        self.assertNotIn("Player", self.by_name)
        for rec in self.result["injuries"]:
            self.assertTrue(rec.player, "empty player name parsed")

    def test_no_irregularities_on_a_well_formed_page(self):
        self.assertEqual(self.result["irregularities"], [])


class TestBucketing(unittest.TestCase):
    def test_markers_land_in_the_bucket_of_the_table_they_precede(self):
        context = ["NE", "\x00TABLE\x00", "SEA", "\x00TABLE\x00", "SF", "\x00TABLE\x00"]
        buckets = _bucket_markers(context, 3)
        self.assertEqual(buckets, [[("team", "NE")], [("team", "SEA")], [("team", "SF")]])

    def test_date_marker_is_captured(self):
        buckets = _bucket_markers(["WEDNESDAY, SEPTEMBER 9TH", "\x00TABLE\x00", "NE"], 1)
        self.assertEqual(buckets, [[("date", "09-09"), ("team", "NE")]])


if __name__ == "__main__":
    unittest.main()
