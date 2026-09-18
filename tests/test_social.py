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


if __name__ == "__main__":
    unittest.main()
