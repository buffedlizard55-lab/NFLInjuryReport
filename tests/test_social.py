import unittest
from unittest import mock

from collectors import social as social_mod
from collectors.social import (
    bsky_post_url,
    classify,
    fetch_bluesky_author,
    fetch_google_news,
    google_news_injury_query,
    is_injury_post,
    rotation_slice,
    x_search_url,
)

from .helpers import fixture_json, fixture_text


class TestClassifier(unittest.TestCase):
    def test_ruled_out(self):
        self.assertEqual(classify("Ty Okada (hamstring) has been ruled out")["status"], "OUT")

    def test_injured_reserve(self):
        self.assertEqual(classify("Placed on injured reserve with a knee injury")["status"], "IR")

    def test_questionable(self):
        self.assertEqual(classify("Listed as questionable with an ankle")["status"], "QUESTIONABLE")

    def test_doubtful(self):
        self.assertEqual(classify("Now doubtful to play")["status"], "DOUBTFUL")

    def test_active(self):
        self.assertEqual(classify("Cleared, full go, will play Sunday")["status"], "ACTIVE")

    def test_body_part_extraction(self):
        self.assertEqual(classify("Dealing with a hamstring strain")["injury"], "hamstring")
        self.assertEqual(classify("High-ankle sprain")["injury"], "ankle")

    def test_unrelated_text_has_low_signal(self):
        c = classify("Great weather for football today")
        self.assertEqual(c["status"], "UNKNOWN")
        self.assertFalse(is_injury_post("Great weather for football today"))

    def test_injury_text_passes_threshold(self):
        self.assertTrue(is_injury_post("Ty Okada (hamstring) has been ruled out for Thursday"))


class TestBlueskyUrl(unittest.TestCase):
    def test_at_uri_to_web_url(self):
        uri = "at://did:plc:abc123/app.bsky.feed.post/3kxyz"
        self.assertEqual(bsky_post_url(uri, "insider.bsky.social"),
                         "https://bsky.app/profile/insider.bsky.social/post/3kxyz")

    def test_bad_uri_returns_empty(self):
        self.assertEqual(bsky_post_url("at://did:plc:abc/app.bsky.actor.profile", "h"), "")
        self.assertEqual(bsky_post_url("", "h"), "")


class TestXDeepLinks(unittest.TestCase):
    def test_manual_verification_link_is_well_formed(self):
        url = x_search_url("Ty Okada", "SEA")
        self.assertTrue(url.startswith("https://x.com/search?q="))
        self.assertIn("f=live", url)
        self.assertIn("SEA", url)
        self.assertNotIn(" ", url)

    def test_no_handle_injection(self):
        url = x_search_url('Bad"Name')
        self.assertNotIn('"', url)


class TestBlueskyAuthorFeed(unittest.TestCase):
    """The path that works: getAuthorFeed carries the platform's own verdict."""

    def _fetch(self, *, insider=True):
        payload = fixture_json("bsky_author_feed.json")
        with mock.patch.object(social_mod, "fetch_json", return_value=payload):
            return fetch_bluesky_author("rapsheet.bsky.social", limit=3, insider=insider)

    def test_posts_are_parsed_with_the_platform_verification(self):
        res = self._fetch()
        moore = res["posts"][0]
        self.assertEqual(moore.posted_at, "2026-09-18T01:37:12.807Z")
        self.assertTrue(moore.verified)
        self.assertEqual(moore.source_kind, "verified-insider")
        self.assertIn("Bluesky verification status=valid", moore.verification_detail)
        self.assertEqual(moore.author_did, "did:plc:hlvr2omhnmvdmfnvmcuhyevz")
        self.assertEqual(moore.url,
                         "https://bsky.app/profile/rapsheet.bsky.social/post/3mvqzscipm225")

    def test_the_missed_moore_post_is_classified_on_both_axes(self):
        moore = self._fetch()["posts"][0]
        # roster axis: "questionable" + a body part
        self.assertEqual(moore.predicted_status, "QUESTIONABLE")
        self.assertEqual(moore.injury, "shoulder")
        # in-game axis: this is what the old pipeline could not express at all
        self.assertEqual(moore.in_game_status, "RETURN_QUESTIONABLE")
        self.assertEqual(moore.game_event, "questionable-to-return")

    def test_warm_up_injury_is_out_for_the_game(self):
        oliver = self._fetch()["posts"][1]
        self.assertEqual(oliver.in_game_status, "OUT_FOR_GAME")
        self.assertIn("Ed Oliver", oliver.text)

    def test_account_without_a_badge_is_not_labelled_verified(self):
        payload = fixture_json("bsky_author_feed.json")
        for entry in payload["feed"]:
            entry["post"]["author"].pop("verification", None)
        with mock.patch.object(social_mod, "fetch_json", return_value=payload):
            res = fetch_bluesky_author("rapsheet.bsky.social", limit=3, insider=True)
        post = res["posts"][0]
        self.assertFalse(post.verified)
        self.assertEqual(post.source_kind, "insider-candidate")
        self.assertEqual(post.verification_detail, "")


class TestGoogleNewsQueries(unittest.TestCase):
    def test_query_is_quoted_and_windowed(self):
        q = google_news_injury_query("Keon Coleman")
        self.assertEqual(q, '"Keon Coleman" injury when:1d')

    def test_player_rss_items_are_parsed_from_headlines(self):
        # Shape captured 2026-09-18 for the exact query in the ledger.
        xml = fixture_text("google_news_coleman.xml")
        with mock.patch.object(social_mod, "fetch_text", return_value=xml):
            res = fetch_google_news(google_news_injury_query("Keon Coleman"), limit=25)
        titles = [p.text for p in res["posts"]]
        self.assertTrue(any("hurt vs. Lions" in t for t in titles))
        hurt = next(p for p in res["posts"]
                    if p.text.startswith("Keon Coleman injury update: Bills WR hurt"))
        self.assertEqual(hurt.author, "aol.com")
        self.assertEqual(hurt.posted_at, "Fri, 18 Sep 2026 01:39:00 GMT")
        # A headline with no roster status word and no body part still yields an
        # in-game event -- this is the Keon Coleman case.
        self.assertEqual(hurt.predicted_status, "UNKNOWN")
        self.assertEqual(hurt.in_game_status, "INJURY_REPORTED")

    def test_rotation_slice_covers_every_club_over_time(self):
        teams = [f"T{i:02d}" for i in range(32)]
        seen = set()
        for step in range(4):
            slice_ = rotation_slice(teams, now=step * 600, size=8, period_seconds=600)
            self.assertEqual(len(slice_), 8)
            seen.update(slice_)
        self.assertEqual(seen, set(teams))


class TestCollectSocialBudgetAndOrder(unittest.TestCase):
    """collect_social orchestration, with every adapter stubbed.

    The user-reported gap (2026-09-21): while a game is in progress the
    pipeline flags EVERY player on the live teams, and each of them needs a
    targeted headline query — the old hard-coded cap of 6 silently starved
    most of them.
    """

    def _run_collect_social(self, *, hot_teams=("KC", "IND"),
                            hot_players=(), live_teams=(), team_names=None):
        calls = []

        def fake_fetch_google_news(query, *, limit=40, source_kind="news"):
            calls.append(query)
            return {"platform": "google-news", "query": query, "url": "u",
                    "posts": [], "error": ""}

        probes = {
            "bluesky": {"reachable": False, "url": "u", "status": 403,
                        "latency_ms": 1, "error": "HTTP 403"},
            "bluesky-author-feed": {"reachable": False, "url": "u", "status": 403,
                                    "latency_ms": 1, "error": "HTTP 403"},
            "mastodon": {"reachable": False, "url": "u", "status": None,
                         "latency_ms": 1, "error": "down"},
            "google-news": {"reachable": True, "url": "u", "status": 200,
                            "latency_ms": 1, "error": ""},
            "reddit": {"reachable": False, "url": "u", "status": 403,
                       "latency_ms": 1, "error": "HTTP 403"},
        }
        with mock.patch.object(social_mod, "probe_platforms", return_value=probes), \
             mock.patch.object(social_mod, "fetch_google_news",
                               side_effect=fake_fetch_google_news):
            res = social_mod.collect_social(
                watched_handles=[], candidate_handles=[],
                hot_teams=list(hot_teams), hot_players=list(hot_players),
                live_teams=list(live_teams),
                team_names=team_names or {
                    "KC": "Kansas City Chiefs", "IND": "Indianapolis Colts",
                    "LAR": "Los Angeles Rams", "NYG": "New York Giants",
                },
                now=1_000_000.0,
            )
        return calls, res

    def test_every_live_game_player_gets_a_targeted_query(self):
        players = [f"Player {i}" for i in range(30)]  # e.g. both live teams' rosters
        calls, _ = self._run_collect_social(hot_players=players)
        for name in players:
            self.assertIn(f'"{name}" injury when:1d', calls,
                          f"no targeted query for {name}")
        # Club + league queries are still there.
        self.assertIn('"Kansas City Chiefs" injury when:1d', calls)
        self.assertIn('"Indianapolis Colts" injury when:1d', calls)
        self.assertIn("NFL injury report", calls)

    def test_duplicate_names_are_queried_once(self):
        calls, _ = self._run_collect_social(hot_players=["A B", "A B", "C D"])
        self.assertEqual(calls.count('"A B" injury when:1d'), 1)
        self.assertIn('"C D" injury when:1d', calls)

    def test_live_players_are_not_truncated_by_fallback_cap(self):
        players = [f"Live Player {i}" for i in range(301)]
        calls, _ = self._run_collect_social(hot_players=players, live_teams=["KC"])
        player_calls = [q for q in calls if '"Live Player ' in q]
        self.assertEqual(len(player_calls), len(players))

    def test_posts_sort_by_parsed_instant_not_string(self):
        # Before the fix this order was impossible: every RFC-822 string
        # ("Mon, ...") sorts AFTER every ISO string ("2026-..."), so the
        # newest insider post (ISO) would be dropped by the max_posts cap.
        posts = [
            social_mod.SocialPost(
                platform="bluesky", post_id="iso-newest", author="h", author_name="h",
                author_url="", text="Player A (ankle) has been ruled out",
                posted_at="2026-09-21T02:00:00Z", url="u1"),
            social_mod.SocialPost(
                platform="google-news", post_id="rfc822-mid", author="o",
                author_name="o", author_url="",
                text="Player B (knee) has been ruled out",
                posted_at="Mon, 21 Sep 2026 01:00:00 GMT", url="u2"),
            social_mod.SocialPost(
                platform="google-news", post_id="rfc822-old", author="o",
                author_name="o", author_url="",
                text="Player C (hamstring) has been ruled out",
                posted_at="Sun, 20 Sep 2026 23:00:00 GMT", url="u3"),
            social_mod.SocialPost(
                platform="mastodon", post_id="undated", author="m", author_name="m",
                author_url="", text="Player D (shoulder) has been ruled out",
                posted_at="", url="u4"),
        ]

        def fake_fetch(query, *, limit=40, source_kind="news"):
            return {"platform": "google-news", "query": query, "url": "u",
                    "posts": list(posts), "error": ""}

        probes = {name: {"reachable": (name == "google-news"), "url": "u",
                         "status": 200, "latency_ms": 1, "error": ""}
                  for name in ("bluesky", "bluesky-author-feed", "mastodon",
                               "google-news", "reddit")}
        with mock.patch.object(social_mod, "probe_platforms", return_value=probes), \
             mock.patch.object(social_mod, "fetch_google_news",
                               side_effect=fake_fetch):
            res = social_mod.collect_social(
                hot_teams=["KC"], hot_players=[],
                team_names={"KC": "Kansas City Chiefs"}, now=1_000_000.0)
        order = [p["post_id"] for p in res["posts"]]
        self.assertEqual(order, ["iso-newest", "rfc822-mid", "rfc822-old", "undated"])


if __name__ == "__main__":
    unittest.main()
