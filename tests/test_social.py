import unittest

from collectors.social import bsky_post_url, classify, is_injury_post, x_search_url


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


if __name__ == "__main__":
    unittest.main()
