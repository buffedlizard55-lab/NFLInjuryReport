import unittest

from collectors.models import Claim
from collectors.reporters import ReporterRegistry
from collectors.scoring import (
    availability_class,
    build_claims,
    resolve_claims,
    summarise,
)


def claim(**kw):
    base = dict(
        claim_id="c1", platform="bluesky", author="insider.bsky.social",
        author_url="https://bsky.app/profile/insider.bsky.social",
        player_key="ty-okada", player="Ty Okada", team="SEA",
        predicted_status="OUT", injury="hamstring", text="Ty Okada ruled out",
        posted_at="2026-09-10T18:00:00Z",
        url="https://bsky.app/profile/insider.bsky.social/post/abc",
    )
    base.update(kw)
    return Claim(**base)


CANONICAL = {
    "SEA:ty-okada": {
        "team": "SEA", "key": "ty-okada", "name": "Ty Okada",
        "game_status": "OUT", "observed_at": "2026-09-10T19:05:00Z",
        "url": "https://www.nfl.com/players/ty-okada/",
    }
}


class TestAvailabilityClasses(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(availability_class("OUT"), "UNAVAILABLE")
        self.assertEqual(availability_class("IR"), "UNAVAILABLE")
        self.assertEqual(availability_class("SUSPENDED"), "UNAVAILABLE")
        self.assertEqual(availability_class("DOUBTFUL"), "LIMITED")
        self.assertEqual(availability_class("QUESTIONABLE"), "LIMITED")
        self.assertEqual(availability_class("ACTIVE"), "AVAILABLE")
        self.assertEqual(availability_class("UNKNOWN"), "UNKNOWN")


class TestResolution(unittest.TestCase):
    def test_matching_class_is_correct(self):
        claims, _ = resolve_claims([claim()], CANONICAL, now=_epoch("2026-09-11T00:00:00Z"))
        c = claims[0]
        self.assertEqual(c.resolution, "CORRECT")
        self.assertEqual(c.evidence_url, "https://www.nfl.com/players/ty-okada/")

    def test_lead_time_is_null_without_first_seen_history(self):
        # nfl.com rows carry the game date at 00:00:00Z, not a publication time.
        # Measuring lead against that produced a mean of 909 minutes and a max of
        # 19 days on live data, so it must be null until real history exists.
        claims, _ = resolve_claims([claim()], CANONICAL, now=_epoch("2026-09-11T00:00:00Z"))
        self.assertIsNone(claims[0].lead_minutes)

    def test_lead_time_uses_first_seen_not_the_source_timestamp(self):
        gt = {"SEA:ty-okada": "2026-09-10T20:00:00Z"}  # first run that saw OUT
        claims, _ = resolve_claims([claim()], CANONICAL,
                                   now=_epoch("2026-09-11T00:00:00Z"), ground_truth_ts=gt)
        # reporter posted 18:00, pipeline first observed the designation at 20:00
        self.assertEqual(claims[0].lead_minutes, 120)

    def test_negative_lead_is_preserved_not_clamped(self):
        gt = {"SEA:ty-okada": "2026-09-10T17:00:00Z"}  # official before the post
        claims, _ = resolve_claims([claim()], CANONICAL,
                                   now=_epoch("2026-09-11T00:00:00Z"), ground_truth_ts=gt)
        self.assertEqual(claims[0].lead_minutes, -60)

    def test_wrong_class_is_wrong(self):
        claims, flags = resolve_claims(
            [claim(predicted_status="QUESTIONABLE")], CANONICAL,
            now=_epoch("2026-09-11T00:00:00Z"),
        )
        # "questionable" -> player was ruled OUT: the availability call was wrong.
        self.assertEqual(claims[0].resolution, "WRONG")
        self.assertIn("CLAIMS_CONTRADICTED", [i.code for i in flags])

    def test_questionable_vs_doubtful_is_same_class(self):
        canonical = {"SEA:ty-okada": dict(CANONICAL["SEA:ty-okada"], game_status="DOUBTFUL")}
        claims, _ = resolve_claims([claim(predicted_status="QUESTIONABLE")], canonical,
                                   now=_epoch("2026-09-11T00:00:00Z"))
        self.assertEqual(claims[0].resolution, "CORRECT")

    def test_unknown_player_is_unverifiable_not_wrong(self):
        claims, _ = resolve_claims([claim(player_key="nobody", player="No Body", team="ARI")],
                                   CANONICAL, now=_epoch("2026-09-11T00:00:00Z"))
        self.assertEqual(claims[0].resolution, "UNVERIFIABLE")

    def test_recent_claim_stays_pending(self):
        canonical = {"SEA:ty-okada": dict(CANONICAL["SEA:ty-okada"], game_status="UNKNOWN")}
        claims, _ = resolve_claims([claim(posted_at="2026-09-10T23:00:00Z")], canonical,
                                   now=_epoch("2026-09-11T00:00:00Z"))
        self.assertEqual(claims[0].resolution, "PENDING")


class TestRegistryScoring(unittest.TestCase):
    def test_no_score_until_enough_claims_resolve(self):
        reg = ReporterRegistry()
        for i in range(3):
            c = claim(claim_id=f"c{i}")
            c.resolution = "CORRECT"
            reg.record_claim(c)
            reg.resolve(c)
        row = reg.to_dict()["reporters"][0]
        self.assertEqual(row["resolved"], 3)
        self.assertIsNone(row["score"], "score published before MIN_RESOLVED_FOR_RATE")

    def test_wilson_lower_bound_penalises_small_samples(self):
        reg = ReporterRegistry()
        for i in range(6):
            c = claim(claim_id=f"c{i}")
            c.resolution = "CORRECT"
            reg.record_claim(c)
            reg.resolve(c)
        row = reg.to_dict()["reporters"][0]
        self.assertEqual(row["accuracy"], 1.0)
        # 6/6 must NOT read as a certain 1.0
        self.assertLess(row["wilson_lower"], 1.0)
        self.assertIsNotNone(row["score"])

    def test_low_accuracy_gets_flagged(self):
        reg = ReporterRegistry()
        for i in range(12):
            c = claim(claim_id=f"c{i}")
            c.resolution = "WRONG" if i % 2 == 0 else "CORRECT"
            reg.record_claim(c)
            reg.resolve(c)
        reg.retier()
        row = reg.to_dict()["reporters"][0]
        self.assertEqual(row["accuracy"], 0.5)
        self.assertEqual(row["tier"], "flagged")

    def test_round_trip_preserves_counters(self):
        reg = ReporterRegistry()
        c = claim()
        c.resolution = "CORRECT"
        c.lead_minutes = 65
        reg.record_claim(c)
        reg.resolve(c)
        again = ReporterRegistry.from_dict(reg.to_dict())
        row = again.to_dict()["reporters"][0]
        self.assertEqual(row["claims_correct"], 1)
        self.assertEqual(row["avg_lead_minutes"], 65.0)


class TestBuildClaims(unittest.TestCase):
    def test_unmatched_text_produces_no_claim(self):
        # No player match => no claim. Guessing a player would poison scores.
        from collectors.matching import PlayerIndex

        idx = PlayerIndex()
        idx.add("Ty Okada", "SEA", "S")
        social = {"posts": [{"platform": "bluesky", "post_id": "x", "author": "a",
                             "author_url": "", "text": "Somebody (knee) is ruled out",
                             "posted_at": "2026-09-10T18:00:00Z", "url": "u",
                             "predicted_status": "OUT", "injury": "knee"}]}
        self.assertEqual(build_claims(social, player_index=idx), [])

    def test_matched_text_produces_a_claim(self):
        from collectors.matching import PlayerIndex

        idx = PlayerIndex()
        idx.add("Ty Okada", "SEA", "S")
        social = {"posts": [{"platform": "bluesky", "post_id": "x", "author": "a",
                             "author_url": "", "text": "Ty Okada (hamstring) has been ruled out",
                             "posted_at": "2026-09-10T18:00:00Z", "url": "u",
                             "predicted_status": "OUT", "injury": "hamstring"}]}
        claims = build_claims(social, player_index=idx)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].player_key, "ty-okada")
        self.assertEqual(claims[0].predicted_status, "OUT")

    def test_espn_attribution_becomes_a_scoreable_claim(self):
        from collectors.matching import PlayerIndex
        from collectors.models import PlayerInjury

        idx = PlayerIndex()
        idx.add("Ty Okada", "SEA", "S")
        espn = {"injuries": [PlayerInjury(
            source="espn", team="SEA", player="Ty Okada", game_status="OUT",
            attribution="Brady Henderson", injury="hamstring",
            comment="ruled out", observed_at="2026-09-10T19:05:00Z",
            url="https://www.espn.com/nfl/player/_/id/4361234/ty-okada",
            source_ids={"espn_injury_id": "700001"},
        )]}
        claims = build_claims({"posts": []}, player_index=idx, espn=espn)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].platform, "espn-attribution")
        self.assertEqual(claims[0].author, "Brady Henderson")


class TestSummary(unittest.TestCase):
    def test_summary_counts(self):
        claims = [claim(claim_id="a"), claim(claim_id="b"), claim(claim_id="c")]
        claims[0].resolution = "CORRECT"
        claims[1].resolution = "WRONG"
        claims[2].resolution = "PENDING"
        claims[0].lead_minutes = 65
        s = summarise(claims)
        self.assertEqual(s["claims_total"], 3)
        self.assertEqual(s["resolved"], 2)
        self.assertEqual(s["accuracy"], 0.5)
        self.assertEqual(s["median_lead_minutes"], 65)


def _epoch(value: str) -> float:
    from collectors.scoring import _parse_epoch

    return _parse_epoch(value)


if __name__ == "__main__":
    unittest.main()
