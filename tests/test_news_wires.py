"""Tests for the fast news wires added on 2026-09-18.

Both fixtures were captured live from the endpoints themselves, so these tests
check the parsers against real payloads rather than invented shapes.
"""

import unittest

from collectors import espn as espn_mod
from collectors import rotowire as rotowire_mod

from .helpers import fixture_json, fixture_text


class TestEspnNews(unittest.TestCase):
    def setUp(self):
        self.payload = espn_mod.parse_news(fixture_json("espn_news.json"),
                                           fetched_at="2026-09-18T07:00:00Z")

    def test_every_article_is_parsed_with_its_timestamp(self):
        items = self.payload["items"]
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual(first.platform, "espn-news")
        self.assertEqual(first.post_id, "49970421")
        self.assertEqual(first.posted_at, "2026-09-18T06:35:37Z")
        self.assertEqual(first.author, "Eric Woodyard")
        self.assertTrue(first.url.startswith("https://www.espn.com/nfl/story/_/id/49970421"))

    def test_single_club_article_carries_the_club_code(self):
        # The stadium story is tagged BUF only -> the club is unambiguous.
        story = next(i for i in self.payload["items"] if i.post_id == "49970295")
        self.assertEqual(story.team, "BUF")

    def test_two_club_article_is_not_assigned_to_either_club(self):
        # The recap is tagged BUF *and* DET; assigning it to one club would be a
        # guess, so the team is left empty and both codes stay in raw["teams"].
        recap = next(i for i in self.payload["items"] if i.post_id == "49969896")
        self.assertEqual(recap.team, "")
        self.assertEqual(recap.raw["teams"], ["BUF", "DET"])

    def test_headlines_are_not_turned_into_injury_rows(self):
        # A headline about a loss contains no injury statement at all.
        for item in self.payload["items"]:
            self.assertEqual(item.in_game_status, "NONE")

    def test_bad_shape_raises_a_flag_instead_of_crashing(self):
        payload = espn_mod.parse_news("not-an-object")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["irregularities"][0].code, "ESPN_NEWS_BAD_SHAPE")

    def test_athlete_category_is_recorded_but_not_used_as_a_filter(self):
        # The endpoint ignores ?athlete= (measured 2026-09-18), so the parser keeps
        # the mention in raw and never claims the article is about that player.
        campbell = self.payload["items"][0]
        self.assertIn("Jack Campbell", campbell.raw["athletes"])
        self.assertEqual(campbell.matched_player, "")


class TestRotowireNews(unittest.TestCase):
    def setUp(self):
        self.payload = rotowire_mod.parse_news_rss(fixture_text("rotowire_news.xml"),
                                                   fetched_at="2026-09-18T07:00:00Z")

    def test_items_are_parsed_with_timestamps(self):
        items = self.payload["items"]
        self.assertEqual(len(items), 5)
        first = items[0]
        self.assertEqual(first.platform, "rotowire-news")
        self.assertEqual(first.text.split(".")[0], "Jameson Williams: Retains modest role in loss")
        self.assertEqual(first.source_kind, "wire")
        self.assertTrue(first.url.startswith("https://www.rotowire.com/"))

    def test_timezone_aware_pubdate_is_converted_to_utc(self):
        # "Thu, 17 Sep 2026 9:54:00 PM PDT" is 2026-09-18T04:54:00Z.
        self.assertEqual(self.payload["items"][0].posted_at, "2026-09-18T04:54:00Z")
        self.assertEqual(rotowire_mod.rss_time_to_iso("Thu, 17 Sep 2026 9:54:00 PM PDT"),
                         "2026-09-18T04:54:00Z")
        self.assertEqual(rotowire_mod.rss_time_to_iso("Fri, 18 Sep 2026 01:47:00 GMT"),
                         "2026-09-18T01:47:00Z")

    def test_unparseable_date_is_empty_not_fabricated(self):
        self.assertEqual(rotowire_mod.rss_time_to_iso("sometime last night"), "")
        self.assertEqual(rotowire_mod.rss_time_to_iso(""), "")

    def test_stat_recaps_do_not_become_in_game_events(self):
        for item in self.payload["items"]:
            self.assertEqual(item.in_game_status, "NONE")

    def test_injury_wire_title_is_classified(self):
        xml = ("<rss><channel><item>"
               "<title>D.J. Moore: Ruled out for remainder of game</title>"
               "<link>https://www.rotowire.com//football/player/dj-moore-1</link>"
               "<description>Moore (shoulder) has been ruled out for the remainder of "
               "Thursday's game.</description>"
               "<pubDate>Thu, 17 Sep 2026 6:40:00 PM PDT</pubDate><guid>1</guid>"
               "</item></channel></rss>")
        payload = rotowire_mod.parse_news_rss(xml)
        item = payload["items"][0]
        self.assertEqual(item.in_game_status, "OUT_FOR_GAME")
        self.assertEqual(item.raw["player_hint"], "D.J. Moore")
        self.assertEqual(item.posted_at, "2026-09-18T01:40:00Z")

    def test_malformed_xml_returns_a_flag(self):
        payload = rotowire_mod.parse_news_rss("<rss><channel><item>")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["irregularities"][0].code, "ROTOWIRE_NEWS_PARSE_ERROR")


if __name__ == "__main__":
    unittest.main()
