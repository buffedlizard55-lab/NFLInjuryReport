"""Tests for the verified reporter / source directory builder."""

import unittest
from unittest import mock

from collectors import directory as d
from collectors.http import FetchError


# --------------------------------------------------------- HTML parsing --

TEAM_PAGE_HTML = """
<html><body>
  <a href="https://www.nfl.com/">NFL Home</a>
  <a href="https://www.azcardinals.com/">Official Website</a>
  <a href="https://www.nfl.com/injuries/">Injury Report</a>
  <a href="https://twitter.com/AZCardinals">@AZCardinals</a>
  <a href="https://x.com/AZCardinals">X</a>
  <a href="https://www.facebook.com/AZCardinals/">f</a>
  <a href="https://www.instagram.com/azcardinals/">@azcardinals</a>
  <a href="https://www.snapchat.com/add/azcards?share_id=x">@azcards</a>
  <a href="https://www.tiktok.com/@azcardinals">TikTok</a>
</body></html>
"""


class TestTeamPageParser(unittest.TestCase):
    def test_parses_official_site_and_socials(self):
        parsed = d.parse_team_directory_html(TEAM_PAGE_HTML)
        self.assertEqual(parsed["official_site"], "https://www.azcardinals.com/")
        s = parsed["socials"]
        self.assertEqual(s["x"]["handle"], "@AZCardinals")
        self.assertIn("twitter.com/AZCardinals", s["x"]["url"])
        self.assertEqual(s["facebook"]["handle"], "@AZCardinals")
        self.assertEqual(s["instagram"]["handle"], "@azcardinals")
        # snapchat "add/" path and query string are stripped to the bare handle
        self.assertEqual(s["snapchat"]["handle"], "@azcards")
        self.assertEqual(s["tiktok"]["handle"], "@azcardinals")

    def test_nfl_links_never_become_official_site(self):
        html = ('<a href="https://www.nfl.com/teams/foo/">Official Website</a>'
                '<a href="https://exampleclub.com/">Example Club</a>'
                '<a href="https://x.com/foo">@foo</a>')
        parsed = d.parse_team_directory_html(html)
        self.assertEqual(parsed["official_site"], "https://exampleclub.com/")

    def test_blank_page_yields_no_guesses(self):
        parsed = d.parse_team_directory_html("<html></html>")
        self.assertEqual(parsed["official_site"], "")
        self.assertEqual(parsed["socials"], {})


# --------------------------------------------------- Bluesky classifier --

def actor(handle, display, **kw):
    a = {"handle": handle, "displayName": display, "did": "did:x:" + handle,
         "labels": [], "description": kw.get("description", "")}
    if "verification" in kw:
        a["verification"] = kw["verification"]
    if "labels" in kw:
        a["labels"] = [{"val": v} for v in kw["labels"]]
    return a


VALID_BSKY_VERIFICATION = {
    "verifiedStatus": "valid", "trustedVerifierStatus": "none",
    "verifications": [{"issuerDisplayName": "Bluesky", "issuerHandle": "bsky.app",
                       "isValid": True, "createdAt": "2025-04-21T10:45:09.683Z"}]}


class TestClassifier(unittest.TestCase):
    def test_verified_exact_name_with_badge(self):
        a = actor("rapsheet.bsky.social", "Ian Rapoport",
                  verification=VALID_BSKY_VERIFICATION)
        status, reason = d.classify_bsky_actor(a, "Ian Rapoport")
        self.assertEqual(status, "verified")
        self.assertIn("Bluesky", reason)

    def test_exact_name_without_badge_is_candidate_not_verified(self):
        a = actor("tompelissero.bsky.social", "Tom Pelissero",
                  description="The Ringer / Netflix Sports")
        status, _ = d.classify_bsky_actor(a, "Tom Pelissero")
        self.assertEqual(status, "candidate")

    def test_impersonation_label_is_non_official_even_with_exact_name(self):
        a = actor("adamschefter.bsky.social", "Adam Schefter", labels=["impersonation"])
        status, reason = d.classify_bsky_actor(a, "Adam Schefter")
        self.assertEqual(status, "non-official")
        self.assertIn("impersonation", reason)

    def test_mirror_handle_and_bio_are_non_official(self):
        a = actor("adamschefter-mirror.bluesky.bot", "Adam Schefter - Mirror",
                  description="Mirror of the original at nitter.net. DM to claim.")
        status, _ = d.classify_bsky_actor(a, "Adam Schefter")
        self.assertEqual(status, "non-official")

    def test_self_declared_parody_bio_is_non_official(self):
        a = actor("blackadamschefter.bsky.social", "Black Adam Schefter",
                  description="PARODY PAGE OF ADAM SCHEFTER!!")
        status, _ = d.classify_bsky_actor(a, "Adam Schefter")
        self.assertEqual(status, "non-official")

    def test_bot_label_is_non_official(self):
        a = actor("someone.bsky.social", "Ian Rapoport", labels=["bot"])
        status, _ = d.classify_bsky_actor(a, "Ian Rapoport")
        self.assertEqual(status, "non-official")

    def test_unverified_same_name_without_role_bio_is_collision(self):
        # Real person, same name, but a VFX artist rather than the NFL reporter.
        a = actor("mrmikeflorio.bsky.social", "Mike Florio",
                  description="Freelance VFX Supe/Mograph/Tech Director Guy, Die Hard Bills "
                              "Fan, Metalhead, GIF user and beer enthusiast.")
        status, reason = d.classify_bsky_actor(a, "Mike Florio")
        self.assertEqual(status, "non-official")
        self.assertIn("name collision", reason)

    def test_unverified_same_name_with_reporter_bio_stays_candidate(self):
        a = actor("kimberleymartin.bsky.social", "Kimberley A. Martin",
                  description="\U0001F3C8 on ESPN")
        status, _ = d.classify_bsky_actor(a, "Kimberley A. Martin")
        self.assertEqual(status, "candidate")

    def test_search_skips_collision_and_keeps_real_candidate(self):
        collision = actor("mrmikeflorio.bsky.social", "Mike Florio",
                          description="Freelance VFX supervisor, Bills fan.")
        real = actor("florionfl.bsky.social", "Mike Florio",
                     description="Pro Football Talk founder, NFL on NBC.")
        payload = {"actors": [collision, real]}
        with mock.patch.object(d, "fetch_json", return_value=payload):
            out = d.verify_bluesky("Mike Florio", "")
        self.assertEqual(out["status"], "candidate")
        self.assertEqual(out["handle"], "florionfl.bsky.social")
        self.assertTrue(any("name collision" in r["reason"] for r in out["rejected"]))

    def test_unrelated_name_is_weak(self):
        a = actor("adamkinzinger.substack.com", "Adam Kinzinger",
                  verification=VALID_BSKY_VERIFICATION)
        status, _ = d.classify_bsky_actor(a, "Adam Schefter")
        self.assertEqual(status, "weak")

    def test_name_normalisation(self):
        self.assertEqual(d._name_key("A.J. Brown"), d._name_key("AJ  Brown"))
        self.assertEqual(d._name_key("Kimberley A. Martin"), "kimberley a martin")


class TestVerifyBluesky(unittest.TestCase):
    def test_getprofile_verified(self):
        payload = actor("rapsheet.bsky.social", "Ian Rapoport",
                        verification=VALID_BSKY_VERIFICATION,
                        description="National Insider for ESPN and NFL Network")
        with mock.patch.object(d, "fetch_json", return_value=payload) as fj:
            out = d.verify_bluesky("Ian Rapoport", "rapsheet.bsky.social")
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["handle"], "rapsheet.bsky.social")
        self.assertTrue(fj.called)

    def test_search_returns_only_impostors(self):
        payload = {"actors": [
            actor("adamschefter-mirror.bluesky.bot", "Adam Schefter - Mirror",
                  description="Mirror of the original at nitter.net"),
            actor("adamschefter.bsky.social", "Adam Schefter", labels=["impersonation"]),
            actor("blackadamschefter.bsky.social", "Black Adam Schefter",
                  description="PARODY PAGE"),
            actor("adamserwer.bsky.social", "Adam Serwer",
                  verification=VALID_BSKY_VERIFICATION),
        ]}
        with mock.patch.object(d, "fetch_json", return_value=payload):
            out = d.verify_bluesky("Adam Schefter", "")
        self.assertEqual(out["status"], "not-found")
        self.assertEqual(out["handle"], "")
        rejected = {r["handle"]: r for r in out["rejected"]}
        self.assertIn("adamschefter.bsky.social", rejected)
        self.assertIn("impersonation", rejected["adamschefter.bsky.social"]["reason"])
        self.assertNotIn("adamserwer.bsky.social", rejected)
        self.assertIn("impersonation", out["detail"])

    def test_fetch_error_surfaces_as_probe_error(self):
        with mock.patch.object(d, "fetch_json",
                               side_effect=FetchError("u", "HTTP 500 boom", 500)):
            out = d.verify_bluesky("Nobody", "nobody.bsky.social")
        self.assertEqual(out["status"], "probe-error")
        self.assertEqual(out["http_status"], 500)


# ---------------------------------------------------------------- X ------

class TestVerifyX(unittest.TestCase):
    def test_oembed_success_marks_verified(self):
        with mock.patch.object(d, "fetch_json",
                               return_value={"author_name": "Adam Schefter"}):
            out = d.verify_x("AdamSchefter", "Adam Schefter")
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["url"], "https://x.com/AdamSchefter")

    def test_oembed_name_mismatch_is_manual_not_verified(self):
        with mock.patch.object(d, "fetch_json",
                               return_value={"author_name": "Some Impostor"}):
            out = d.verify_x("AdamSchefter", "Adam Schefter")
        self.assertEqual(out["status"], "manual")
        self.assertIn("does not match", out["detail"])

    def test_all_keyless_endpoints_fail_degrades_to_manual(self):
        with mock.patch.object(d, "fetch_json",
                               side_effect=FetchError("u", "HTTP 403 Forbidden", 403)), \
             mock.patch.object(d, "fetch_text",
                               side_effect=FetchError("u", "HTTP 404", 404)):
            out = d.verify_x("AdamSchefter")
        self.assertEqual(out["status"], "manual")
        self.assertIn("manual verification", out["detail"])
        self.assertEqual({a["endpoint"] for a in out["attempts"]},
                         {"oembed", "syndication"})

    def test_no_handle_is_none(self):
        out = d.verify_x("")
        self.assertEqual(out["status"], "none")


# --------------------------------------------------------- beat writers --

def espn_claim(author, team, url, posted="2026-09-10T18:00:00Z", tier="Local Paper"):
    return {"platform": "espn-attribution", "author": author, "tier": tier,
            "team": team, "url": url, "posted_at": posted}


class TestBeatAggregation(unittest.TestCase):
    def setUp(self):
        self.claims = {"claims": [
            espn_claim("Jane Reporter", "HOU", "https://www.espn.com/nfl/player/_/id/1/a"),
            espn_claim("Jane Reporter", "HOU", "https://www.espn.com/nfl/player/_/id/2/b"),
            espn_claim("Jane Reporter", "HOU", "https://www.espn.com/nfl/player/_/id/1/a"),
            espn_claim("One Off", "DET", "https://www.espn.com/nfl/player/_/id/3/c"),
            espn_claim("Wire Guy", "HOU", "https://www.espn.com/nfl/player/_/id/4/d"),
            espn_claim("Wire Guy", "BAL", "https://www.espn.com/nfl/player/_/id/5/e"),
        ]}

    def test_threshold_dedup_and_counts(self):
        rows = d.beat_from_claims(self.claims)
        names = {r["name"] for r in rows}
        self.assertIn("Jane Reporter", names)
        self.assertNotIn("One Off", names)  # single-update writers omitted
        jane = next(r for r in rows if r["name"] == "Jane Reporter")
        self.assertEqual(jane["updates"], 3)
        self.assertEqual(jane["teams"], ["HOU"])
        self.assertEqual(len(jane["evidence"]), 2)  # deduped URLs, capped at 4
        self.assertTrue(jane["x_search_url"].startswith("https://x.com/search?"))
        self.assertIn("news.google.com", jane["google_news_url"])
        # never an invented profile: only exact-name search links
        self.assertIn("/search?", jane["platforms"]["x"]["url"])

    def test_national_seed_names_excluded(self):
        rows = d.beat_from_claims(
            self.claims, exclude_keys={d._name_key("Jane Reporter")})
        self.assertNotIn("Jane Reporter", {r["name"] for r in rows})

    def test_cross_team_flag(self):
        rows = d.beat_from_claims(self.claims)
        flags = d.annotate_duplicate_variants(rows)
        cross = [f for f in flags if f.code == "DIRECTORY_CROSS_TEAM_BEAT"]
        self.assertEqual(len(cross), 1)
        self.assertIn("Wire Guy", cross[0].title)
        self.assertEqual({"HOU", "BAL"}, set(cross[0].title.split("(")[1].rstrip(")").split(", ")))

    def test_near_duplicate_names_flagged(self):
        claims = {"claims": [
            espn_claim("John Doe", "HOU", "u1"), espn_claim("John Doe", "HOU", "u2"),
            espn_claim("Jon Doe", "HOU", "u3"), espn_claim("Jon Doe", "HOU", "u4"),
        ]}
        rows = d.beat_from_claims(claims)
        flags = d.annotate_duplicate_variants(rows)
        variants = [f for f in flags if f.code == "DIRECTORY_NAME_VARIANT"]
        self.assertEqual(len(variants), 1)


# ----------------------------------------------------------- link helpers --

class TestLastGoodRetention(unittest.TestCase):
    def test_failed_team_probe_keeps_cached_row(self):
        prior = {
            "code": "ARI", "name": "Arizona Cardinals", "slug": "arizona-cardinals",
            "nfl_team_url": "u", "official_site": "https://www.azcardinals.com/",
            "socials": {"x": {"handle": "@AZCardinals", "url": "u", "as_published": "@x"}},
            "probe": {"status": 200, "fetched_at": "2026-09-10T12:00:00Z"},
        }
        cache = {"teams": {"ARI": dict(prior)}}
        failed = dict(prior, official_site="", socials={},
                      probe={"status": None, "error": "SSL EOF", "fetched_at": ""})
        with mock.patch.object(d, "fetch_team_directory", return_value=failed):
            rows, flags = d.build_team_rows(offline=False, force_refresh=False, cache=cache)
        row = next(r for r in rows if r["code"] == "ARI")
        self.assertEqual(row["official_site"], "https://www.azcardinals.com/")
        self.assertIn("x", row["socials"])  # good row retained, nothing blanked
        codes = [f.code for f in flags]
        self.assertIn("DIRECTORY_TEAM_PAGE_FAILED", codes)

    def test_failed_bluesky_probe_keeps_cached_verification(self):
        good = {"bluesky": {"platform": "bluesky", "status": "verified",
                            "handle": "rapsheet.bsky.social"},
                "x": {"platform": "x", "status": "manual", "handle": "RapSheet",
                      "url": "https://x.com/RapSheet"},
                "verified_at": "2000-01-01T00:00:00Z"}  # stale -> would re-probe
        cache = {"national": {d._name_key("Ian Rapoport"): good}}
        with mock.patch.object(d, "verify_bluesky",
                               return_value={"platform": "bluesky", "status": "probe-error",
                                             "handle": "", "rejected": []}), \
             mock.patch.object(d, "verify_x", return_value=good["x"]), \
             mock.patch.object(d.time, "sleep"):
            rows, _ = d.build_national_rows(offline=False, verification_cache=cache, observed={})
        rap = next(r for r in rows if r["name"] == "Ian Rapoport")
        self.assertEqual(rap["bluesky"]["status"], "verified")
        # stale timestamp kept so the next run retries immediately
        self.assertEqual(cache["national"][d._name_key("Ian Rapoport")]["verified_at"],
                         "2000-01-01T00:00:00Z")


    def test_first_probe_results_are_rendered_and_retried(self):
        """Regression: first-time probe results were written to state but the
        rendered entry kept the pre-probe 'not-probed' status."""
        from collectors.http import FetchError

        def boom(url, **kw):
            raise FetchError(url, "TLS EOF", None)

        cache = {"national": {}}
        with mock.patch.object(d, "fetch_json", side_effect=boom), \
             mock.patch.object(d, "fetch_text", side_effect=boom), \
             mock.patch.object(d.time, "sleep"):
            rows1, _ = d.build_national_rows(
                offline=False, verification_cache=cache, observed={})
        # every first-time failure must reach the rendered entry ...
        self.assertTrue(all(r["bluesky"]["status"] == "probe-error" for r in rows1))
        # ... and be persisted so the next build can retry it.
        self.assertTrue(all(v["bluesky"]["status"] == "probe-error"
                            for v in cache["national"].values()))

        ok = {"handle": "rapsheet.bsky.social", "did": "did:x",
              "displayName": "Ian Rapoport", "labels": [],
              "description": "National Insider",
              "verification": {"verifiedStatus": "valid",
                               "verifications": [{"issuerDisplayName": "Bluesky",
                                                  "isValid": True}]}}
        def fake_json(url, **kw):
            return ok if "getProfile" in url else {"actors": [ok]}

        with mock.patch.object(d, "fetch_json", side_effect=fake_json), \
             mock.patch.object(d, "fetch_text", side_effect=boom), \
             mock.patch.object(d.time, "sleep"):
            rows2, _ = d.build_national_rows(
                offline=False, verification_cache=cache, observed={})
        rap = next(r for r in rows2 if r["name"] == "Ian Rapoport")
        self.assertEqual(rap["bluesky"]["status"], "verified")

    def test_offline_skips_all_network(self):
        cache = {"national": {}}
        with mock.patch.object(d, "fetch_json") as fj, \
             mock.patch.object(d, "fetch_text") as ft:
            d.build_national_rows(offline=True, verification_cache=cache, observed={})
        fj.assert_not_called()
        ft.assert_not_called()


class TestLinkHelpers(unittest.TestCase):
    def test_x_profile_url(self):
        self.assertEqual(d.x_profile_url("@RapSheet"), "https://x.com/RapSheet")
        self.assertEqual(d.x_profile_url("RapSheet"), "https://x.com/RapSheet")

    def test_google_news_url_encodes_query(self):
        u = d.google_news_url("Jane Reporter")
        self.assertIn("q=%22Jane+Reporter%22", u.replace("%20", "+"))


if __name__ == "__main__":
    unittest.main()
