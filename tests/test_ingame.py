"""Tests for the in-game injury vocabulary.

Every string asserted on here was published on 2026-09-17/18 by a source this
project reads; the two failures that motivated the module are covered first.
"""

import unittest

from collectors.ingame import (
    availability_class,
    build_game_events,
    classify_game_event,
    event_severity,
    is_performance_blurb,
    parse_scoreboard,
)
from collectors.matching import PlayerIndex

from .helpers import fixture_json

#: Verbatim, Ian Rapoport on Bluesky (rapsheet.bsky.social), 2026-09-18T01:37:12Z.
MOORE_POST = ("Bills WR DJ Moore, who landed hard on his shoulder, is questionable to "
              "return with a shoulder injury and a stinger. He was taken into the locker "
              "room for X-rays.")
#: Verbatim, same account, 2026-09-17T23:10:58Z (warm-ups, before kickoff).
OLIVER_POST = ("A surprise: Bills standout DT Ed Oliver (hip) was injured during "
               "warm-ups and is out for the game")
#: Verbatim ESPN injuries-feed comment, observed 2026-09-18T02:12:00Z.
MOORE_ESPN_COMMENT = ("Moore (shoulder) has been ruled out for the remainder of "
                      "Thursday night's game against the Lions.")
#: Verbatim ESPN injuries-feed comment for Keon Coleman, observed 2026-09-18T05:02:00Z.
COLEMAN_BLURB = ("Coleman brought in all six targets for 63 yards in the Bills' 41-31 "
                 "win over the Lions on Thursday.")


class TestGameEventClassification(unittest.TestCase):
    def test_rapoport_moore_post_is_questionable_to_return(self):
        ev = classify_game_event(MOORE_POST)
        self.assertEqual(ev["status"], "RETURN_QUESTIONABLE")
        self.assertEqual(ev["event"], "questionable-to-return")
        self.assertEqual(ev["injury"], "shoulder")

    def test_espn_comment_is_out_for_the_game(self):
        ev = classify_game_event(MOORE_ESPN_COMMENT)
        self.assertEqual(ev["status"], "OUT_FOR_GAME")
        self.assertEqual(ev["event"], "ruled-out-for-game")
        # The evidence must be the sentence itself, verbatim.
        self.assertIn("remainder of Thursday night's game", ev["sentences"][0])

    def test_warm_up_injury_is_out_for_the_game(self):
        self.assertEqual(classify_game_event(OLIVER_POST)["status"], "OUT_FOR_GAME")

    def test_functional_play_is_unavailable_not_return_questionable(self):
        self.assertEqual(classify_game_event("Hamlin (foot) has been ruled out")["status"],
                         "OUT_FOR_GAME")
        self.assertEqual(
            classify_game_event("Won't return after a knee injury")["status"],
            "OUT_FOR_GAME")
        self.assertEqual(
            classify_game_event("Carted off with a head injury")["status"], "EVALUATED")

    def test_returned_to_the_game(self):
        ev = classify_game_event("Keon Coleman returned to the game after a shoulder injury")
        self.assertEqual(ev["status"], "RETURNED")
        self.assertEqual(ev["event"], "returned-to-game")

    def test_stat_recap_is_not_an_injury(self):
        ev = classify_game_event(COLEMAN_BLURB)
        self.assertEqual(ev["status"], "NONE")
        self.assertEqual(ev["event"], "")
        self.assertTrue(ev["blurb"])
        self.assertTrue(is_performance_blurb(COLEMAN_BLURB))

    def test_blurb_detector_does_not_swallow_injury_text(self):
        self.assertFalse(is_performance_blurb(MOORE_POST))
        self.assertFalse(is_performance_blurb(MOORE_ESPN_COMMENT))

    def test_plain_play_by_play_is_not_an_event(self):
        self.assertEqual(
            classify_game_event("Allen completes to Shakir for 12 yards.")["status"], "NONE")

    def test_game_day_inactive_is_out_for_the_game(self):
        # Verbatim shape of ESPN's injuries-feed comment for a game-day inactive.
        ev = classify_game_event("Bills' Ty Johnson is listed as inactive Thursday "
                                 "against the Lions.")
        self.assertEqual(ev["status"], "OUT_FOR_GAME")
        self.assertEqual(classify_game_event("Sanders (knee) won't play tonight")["status"],
                         "OUT_FOR_GAME")

    def test_practice_report_line_is_not_an_in_game_event(self):
        # Verbatim ESPN injuries-feed practice line (2026-09-18, Isaiah Adams).
        ev = classify_game_event("Adams (knee) was a limited participant at the "
                                 "Cardinals' practice Thursday, Josh Weinfuss of "
                                 "ESPN.com reports.")
        self.assertEqual(ev["status"], "NONE")

    def test_severity_mapping(self):
        self.assertEqual(event_severity("OUT_FOR_GAME"), "critical")
        self.assertEqual(event_severity("RETURN_QUESTIONABLE"), "medium")
        self.assertEqual(event_severity("RETURN_QUESTIONABLE", severe=True), "high")
        self.assertEqual(event_severity("INJURY_REPORTED"), "medium")
        self.assertEqual(availability_class("OUT_FOR_GAME"), "UNAVAILABLE")
        self.assertEqual(availability_class("RETURNED"), "AVAILABLE")
        self.assertEqual(availability_class("EVALUATED"), "LIMITED")


class TestGameEventBuilding(unittest.TestCase):
    def _index(self):
        index = PlayerIndex()
        index.add("DJ Moore", "BUF", "WR", "https://www.espn.com/nfl/player/_/id/3915416/dj-moore", "espn")
        index.add("Keon Coleman", "BUF", "WR", "https://www.espn.com/nfl/player/_/id/4635008/keon-coleman", "espn")
        index.add("Ed Oliver", "BUF", "DT", "", "espn")
        return index

    def test_verified_post_becomes_a_labelled_event(self):
        posts = [{
            "platform": "bluesky", "author": "rapsheet.bsky.social",
            "author_name": "Ian Rapoport", "url": "https://bsky.app/profile/rapsheet.bsky.social/post/3mvqzscipm225",
            "text": MOORE_POST, "posted_at": "2026-09-18T01:37:12Z",
            "verified": True, "verification_detail": "Bluesky verification status=valid (issuer: Bluesky)",
        }]
        events = build_game_events(posts=posts, player_index=self._index(),
                                   now="2026-09-18T01:40:00Z", hot_teams=["BUF", "DET"])
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["team"], "BUF")
        self.assertEqual(ev["player"], "DJ Moore")
        self.assertEqual(ev["in_game_status"], "RETURN_QUESTIONABLE")
        self.assertTrue(ev["source_verified"])
        self.assertTrue(ev["in_game_window"])
        # 2m48s between the post and the run that saw it: reported, not invented.
        self.assertEqual(ev["detection_latency_seconds"], 168)

    def test_espn_comment_and_post_merge_with_both_sources_kept(self):
        posts = [{
            "platform": "bluesky", "author": "rapsheet.bsky.social", "author_name": "Ian Rapoport",
            "url": "https://bsky.app/profile/rapsheet.bsky.social/post/3mvqzscipm225",
            "text": MOORE_POST, "posted_at": "2026-09-18T01:37:12Z", "verified": True,
            "verification_detail": "valid",
        }]

        class Rec:
            team = "BUF"
            player = "DJ Moore"
            player_key = "dj-moore"
            position = "WR"
            comment = MOORE_ESPN_COMMENT
            observed_at = "2026-09-18T02:12:00Z"
            url = "https://www.espn.com/nfl/player/_/id/3915416/dj-moore"
            attribution = ""

        events = build_game_events(posts=posts, espn={"injuries": [Rec()]},
                                   player_index=self._index(),
                                   now="2026-09-18T05:51:44Z", hot_teams=["BUF"])
        self.assertEqual(len(events), 1)
        ev = events[0]
        # Newest information wins: ESPN's later "ruled out for the remainder" beats
        # the earlier "questionable to return", and both sources stay attached.
        self.assertEqual(ev["in_game_status"], "OUT_FOR_GAME")
        self.assertEqual(len(ev["sources"]), 2)
        self.assertTrue(ev["source_verified"])
        self.assertGreater(ev["detection_latency_seconds"], 0)

    def test_blurb_produces_no_event(self):
        class Rec:
            team = "BUF"
            player = "Keon Coleman"
            player_key = "keon-coleman"
            position = "WR"
            comment = COLEMAN_BLURB
            observed_at = "2026-09-18T05:02:00Z"
            url = ""
            attribution = ""

        events = build_game_events(espn={"injuries": [Rec()]}, player_index=self._index(),
                                   now="2026-09-18T05:51:44Z", hot_teams=["BUF"])
        self.assertEqual(events, [])

    def test_headline_for_a_roster_player_in_a_live_game_is_recorded(self):
        posts = [{
            "platform": "google-news", "author": "aol.com", "author_name": "aol.com",
            "url": "https://news.google.com/rss/articles/x",
            "text": "Keon Coleman injury update: Bills WR hurt vs. Lions",
            "posted_at": "Fri, 18 Sep 2026 01:39:00 GMT", "source_kind": "news",
        }]
        events = build_game_events(posts=posts, player_index=self._index(),
                                   now="2026-09-18T05:51:44Z", hot_teams=["BUF", "DET"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["player"], "Keon Coleman")
        self.assertEqual(events[0]["team"], "BUF")

    def test_article_titles_never_become_players(self):
        # The first live run (2026-09-18) turned headlines like these into
        # "players" because the headline fallback accepted any leading phrase
        # before a colon. They are article titles, not people.
        posts = [{
            "platform": "google-news", "author": "example.com", "author_name": "example.com",
            "url": "https://news.google.com/rss/articles/vikings",
            "text": "Vikings Injury Report: Thursday practice updates for Minnesota",
            "posted_at": "Fri, 18 Sep 2026 22:00:00 GMT", "source_kind": "news",
        }, {
            "platform": "mastodon", "author": "someone",
            "url": "https://example.invalid/concussion",
            "text": "The NFL Concussion Protocol returned a player to action",
            "posted_at": "2026-09-17T17:55:00Z",
        }]
        events = build_game_events(posts=posts, player_index=self._index(),
                                   now="2026-09-18T07:20:00Z", hot_teams=["BUF", "DET"])
        self.assertEqual(events, [])

    def test_a_vague_newer_headline_does_not_erase_a_stated_status(self):
        # Live 2026-09-18: a 23:46Z "Ed Oliver injury update" headline was ranked
        # above the 23:10:58Z verified post "injured during warm-ups and is out
        # for the game", downgrading OUT_FOR_GAME to INJURY_REPORTED. A report
        # that states no availability cannot overwrite one that does.
        posts = [{
            "platform": "bluesky", "author": "rapsheet.bsky.social",
            "author_name": "Ian Rapoport", "url": "https://bsky.app/profile/x/post/1",
            "text": OLIVER_POST, "posted_at": "2026-09-17T23:10:58Z", "verified": True,
        }, {
            "platform": "google-news", "author": "example.com", "author_name": "example.com",
            "url": "https://news.google.com/rss/articles/oliver",
            "text": "Ed Oliver injury update: latest on the Bills defensive tackle",
            "posted_at": "Thu, 17 Sep 2026 23:46:00 GMT", "source_kind": "news",
        }]
        events = build_game_events(posts=posts, player_index=self._index(),
                                   now="2026-09-18T02:17:00Z", hot_teams=["BUF"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["in_game_status"], "OUT_FOR_GAME")
        # Both reports stay attached: the merge is auditable from the event.
        self.assertEqual(len(events[0]["sources"]), 2)

    def test_iso_post_wins_over_an_earlier_rfc822_headline(self):
        # Live 2026-09-18: "Thu, 17 Sep 2026 23:10:00 GMT" sorted after the
        # later ISO post "2026-09-18T01:37:12.807Z" as a plain string, so a
        # pre-kickoff headline beat Rapoport's in-game report. Instants decide.
        posts = [{
            "platform": "google-news", "author": "example.com", "author_name": "example.com",
            "url": "https://news.google.com/rss/articles/moore",
            "text": "DJ Moore injury update: Bills WR banged up in practice",
            "posted_at": "Thu, 17 Sep 2026 23:10:00 GMT", "source_kind": "news",
        }, {
            "platform": "bluesky", "author": "rapsheet.bsky.social",
            "author_name": "Ian Rapoport", "url": "https://bsky.app/profile/x/post/2",
            "text": MOORE_POST, "posted_at": "2026-09-18T01:37:12.807Z", "verified": True,
        }]
        events = build_game_events(posts=posts, player_index=self._index(),
                                   now="2026-09-18T02:17:00Z", hot_teams=["BUF"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["in_game_status"], "RETURN_QUESTIONABLE")
        # ...and the detection latency is computed from the post, not the headline.
        self.assertEqual(events[0]["detection_latency_seconds"], 2387)

    def test_club_outside_the_game_window_is_not_an_in_game_event(self):
        # A real player, a real injury word -- but his club is not playing, so
        # this is a roster item and must not appear as an in-game event.
        index = PlayerIndex()
        index.add("Justin Jefferson", "MIN", "WR",
                  "https://www.espn.com/nfl/player/_/id/4262921/justin-jefferson", "espn")
        posts = [{
            "platform": "google-news", "author": "example.com", "author_name": "example.com",
            "url": "https://news.google.com/rss/articles/min",
            "text": "Justin Jefferson injury update: Vikings WR hurt vs. Eagles",
            "posted_at": "Fri, 18 Sep 2026 22:00:00 GMT", "source_kind": "news",
        }]
        events = build_game_events(posts=posts, player_index=index,
                                   now="2026-09-18T07:20:00Z", hot_teams=["BUF", "DET"])
        self.assertEqual(events, [])

    def test_post_with_no_player_and_no_club_is_dropped(self):
        posts = [{"platform": "mastodon", "text": "Someone got hurt out there tonight",
                  "posted_at": "2026-09-18T01:00:00Z", "url": "https://example.invalid/1"}]
        self.assertEqual(build_game_events(posts=posts, player_index=self._index()), [])


class TestScoreboard(unittest.TestCase):
    def test_live_shape_from_the_recorded_bills_lions_game(self):
        parsed = parse_scoreboard(fixture_json("espn_scoreboard.json"))
        self.assertEqual(parsed["count"], 1)
        game = parsed["games"][0]
        self.assertEqual(game["id"], "401872932")
        self.assertEqual(game["short_name"], "DET @ BUF")
        self.assertEqual(game["state"], "post")
        self.assertEqual({t["code"] for t in game["teams"]}, {"BUF", "DET"})
        self.assertEqual(parsed["live"], [])
        self.assertEqual(set(parsed["hot_teams"]), {"BUF", "DET"})

    def test_malformed_payload_is_empty_not_an_exception(self):
        for payload in (None, [], {"events": [{"id": "1"}]}, {"events": "nope"}):
            parsed = parse_scoreboard(payload)
            self.assertIn("games", parsed)
            self.assertIsInstance(parsed["hot_teams"], list)


class TestEventTimeGuards(unittest.TestCase):
    """A live event is something a source said *recently*.

    These are the two cases caught while replaying the recorded payloads: a
    month-old preseason headline (Google News returns old items when the query
    window is widened) and a dated-in-UTC-but-not-ISO headline, whose RFC-822
    clock previously made its age unmeasurable.
    """

    def _index(self):
        index = PlayerIndex()
        index.add("Keon Coleman", "BUF", "WR",
                  "https://www.espn.com/nfl/player/_/id/4635008/keon-coleman", "espn")
        return index

    def _post(self, text, posted_at):
        return {"platform": "google-news", "author": "aol.com", "author_name": "aol.com",
                "url": "https://news.google.com/rss/articles/x", "text": text,
                "posted_at": posted_at, "verified": False,
                "verification_detail": "", "source_kind": "news"}

    def test_rfc822_headline_gets_a_real_detection_latency(self):
        events = build_game_events(
            posts=[self._post("Keon Coleman injury update: Bills WR hurt vs. Lions",
                              "Fri, 18 Sep 2026 01:39:00 GMT")],
            player_index=self._index(), now="2026-09-18T02:17:00Z", hot_teams=["BUF"])
        self.assertEqual(len(events), 1)
        # 01:39:00Z headline, seen by the 02:17:00Z run: 38 minutes, measured.
        self.assertEqual(events[0]["detection_latency_seconds"], 2280)

    def test_month_old_headline_is_not_an_in_game_event(self):
        events = build_game_events(
            posts=[self._post("NFL Network: WR Keon Coleman suffered sprained foot/toe "
                              "in Bills' preseason opener",
                              "Tue, 18 Aug 2026 07:00:00 GMT")],
            player_index=self._index(), now="2026-09-18T02:17:00Z", hot_teams=["BUF"])
        self.assertEqual(events, [])

    def test_undated_text_never_becomes_a_live_event(self):
        events = build_game_events(
            posts=[self._post("Bills WR Keon Coleman hurt vs. Lions", "")],
            player_index=self._index(), now="2026-09-18T02:17:00Z", hot_teams=["BUF"])
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
