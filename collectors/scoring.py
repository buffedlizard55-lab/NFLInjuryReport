"""Reporter scoring: resolve social/attributed claims against the official report.

GROUND TRUTH
------------
Only the nfl.com designation is used as ground truth, because it is the league's
own Game Status Report. A claim is resolved against the canonical record for the
same (club, player) in the newest snapshot that contains it.

RESOLUTION RULES (documented so every score is auditable)
--------------------------------------------------------
Availability classes:
    UNAVAILABLE = OUT, IR, PUP, NFI, SUSPENDED
    LIMITED     = QUESTIONABLE, DOUBTFUL
    AVAILABLE   = ACTIVE

  CORRECT       claim's class == official class
  WRONG         claim's class != official class
  UNVERIFIABLE  no official record exists for that player, or the claim is not
                yet old enough to be judged (see PENDING_WINDOW_HOURS)

A reporter who says "questionable" about a player who is later ruled OUT scores
WRONG: the player did not play, so the availability call was wrong. This is
deliberate and strict -- it is the behaviour that makes the scorecard mean
something. Scores are only published once a reporter has
ReporterRegistry.MIN_RESOLVED_FOR_RATE resolved claims, and the headline figure is
the Wilson lower bound rather than the raw ratio, so two lucky calls do not read
as 100%.

HISTORICAL BACKFILL -- honest answer
------------------------------------
A historical scorecard for past reporter posts is NOT possible from free sources.
The official side can be backfilled (nfl.com exposes a season selector back to
1965), but there is no free archive of historical X/Bluesky/Reddit posts with
trustworthy timestamps, and X's API has had no free read tier since Feb 2026.
Therefore this system FORWARD-collects: every run appends claims, and the
scorecard grows from the moment the pipeline is enabled. `backfill_official`
below seeds the official side of history so the first resolutions are not blind.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from .models import Claim, Irregularity, slugify
from .reporters import ReporterRegistry
from .social import SocialPost, classify

UNAVAILABLE = {"OUT", "IR", "PUP", "NFI", "SUSPENDED"}
LIMITED = {"QUESTIONABLE", "DOUBTFUL"}
AVAILABLE = {"ACTIVE"}

#: A claim younger than this is left PENDING; the official report may not have
#: caught up yet and judging it immediately would be unfair and noisy.
PENDING_WINDOW_HOURS = 6.0


def availability_class(status: str) -> str:
    if status in UNAVAILABLE:
        return "UNAVAILABLE"
    if status in LIMITED:
        return "LIMITED"
    if status in AVAILABLE:
        return "AVAILABLE"
    return "UNKNOWN"


def _parse_epoch(value: str) -> Optional[float]:
    import re
    from datetime import datetime, timezone

    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        v += "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        # RSS uses RFC 822: "Thu, 10 Sep 2026 20:48:00 GMT"
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(value)
        except Exception:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def build_claims(
    social: Dict[str, Any],
    *,
    player_index: Any,
    espn: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> List[Claim]:
    """Turn social posts and ESPN attributions into scored Claims."""

    now = now if now is not None else time.time()
    claims: List[Claim] = []

    for raw in social.get("posts", []) or []:
        post = raw if isinstance(raw, dict) else raw.to_dict()
        text = post.get("text") or ""
        cls = classify(text)
        if cls["signal"] < 0.3:
            continue
        match = player_index.find_in_text(text) if player_index else None
        if match is None:
            continue  # no player match -> not scoreable, never guessed
        status = post.get("predicted_status") or cls["status"]
        if status in ("", "UNKNOWN"):
            continue
        posted = post.get("posted_at") or ""
        claim_id = f"{post.get('platform')}:{post.get('post_id') or post.get('url')}"
        claims.append(
            Claim(
                claim_id=claim_id,
                platform=post.get("platform", ""),
                author=post.get("author", ""),
                author_url=post.get("author_url", ""),
                player_key=match["key"],
                player=match["name"],
                team=(match.get("teams") or [""])[0],
                predicted_status=status,
                injury=post.get("injury") or cls["injury"],
                text=text[:500],
                posted_at=posted,
                url=post.get("url", ""),
            )
        )

    # ESPN names the beat writer on each update; that is a first-party
    # attribution and is scored exactly like a social post.
    for rec in (espn or {}).get("injuries", []) or []:
        if not rec.attribution:
            continue
        claims.append(
            Claim(
                claim_id=f"espn:{rec.source_ids.get('espn_injury_id') or rec.identity}",
                platform="espn-attribution",
                author=rec.attribution,
                author_url=rec.url,
                player_key=rec.player_key,
                player=rec.player,
                team=rec.team,
                predicted_status=rec.game_status,
                injury=rec.injury,
                text=rec.comment[:500],
                posted_at=rec.observed_at,
                url=rec.url,
                tier=rec.raw.get("outlet", ""),
            )
        )

    return claims


def resolve_claims(
    claims: List[Claim],
    canonical: Dict[str, Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> Tuple[List[Claim], List[Irregularity]]:
    """Score claims against the canonical (official-first) report."""

    now = now if now is not None else time.time()
    irregularities: List[Irregularity] = []
    pending_cutoff = now - PENDING_WINDOW_HOURS * 3600

    for claim in claims:
        rec = canonical.get(f"{claim.team}:{claim.player_key}")
        if rec is None:
            # Try any team, in case the claim named a player whose club we could
            # not resolve from the post text.
            candidates = [
                v for k, v in canonical.items() if k.endswith(":" + claim.player_key)
            ]
            if len(candidates) == 1:
                rec = candidates[0]
        if rec is None:
            claim.resolution = "UNVERIFIABLE"
            claim.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            claim.evidence_url = "https://www.nfl.com/injuries/"
            continue

        official_status = rec.get("game_status", "UNKNOWN")
        official_ts = _parse_epoch(rec.get("observed_at", ""))
        claim_ts = _parse_epoch(claim.posted_at)

        if official_status in ("", "UNKNOWN"):
            if claim_ts is not None and claim_ts > pending_cutoff:
                continue  # stay PENDING
            claim.resolution = "UNVERIFIABLE"
            claim.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            claim.evidence_url = rec.get("url", "https://www.nfl.com/injuries/")
            continue

        if claim_ts is not None and official_ts is not None:
            claim.lead_minutes = int((official_ts - claim_ts) // 60)
        claim.evidence_url = rec.get("url") or "https://www.nfl.com/injuries/"

        pred_class = availability_class(claim.predicted_status)
        off_class = availability_class(official_status)
        if pred_class == "UNKNOWN" or off_class == "UNKNOWN":
            claim.resolution = "UNVERIFIABLE"
        elif pred_class == off_class:
            claim.resolution = "CORRECT"
        else:
            claim.resolution = "WRONG"
        claim.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    wrong = [c for c in claims if c.resolution == "WRONG"]
    if wrong:
        irregularities.append(
            Irregularity(
                code="CLAIMS_CONTRADICTED",
                severity="low",
                title=f"{len(wrong)} reporter claim(s) contradicted by the official report",
                detail=(
                    "These claims are retained (not deleted) because a contradicted claim is "
                    "the evidence that drives a reporter's score down. Examples: "
                    + "; ".join(
                        f"{c.author} said {c.predicted_status} for {c.player} "
                        f"(official: see link)" for c in wrong[:5]
                    )
                ),
                evidence=[
                    {"label": f"{c.author}: {c.player} {c.predicted_status}",
                     "url": c.evidence_url or c.url}
                    for c in wrong[:5]
                ],
            )
        )
    return claims, irregularities


def apply_to_registry(
    registry: ReporterRegistry, claims: List[Claim]
) -> List[Dict[str, str]]:
    """Record/resolve every claim and return tier changes."""

    for claim in claims:
        registry.record_claim(claim)
        if claim.resolution in ("CORRECT", "WRONG", "UNVERIFIABLE"):
            registry.resolve(claim)
    return registry.retier()


def summarise(claims: List[Claim]) -> Dict[str, Any]:
    total = len(claims)
    by_res = {"CORRECT": 0, "WRONG": 0, "UNVERIFIABLE": 0, "PENDING": 0}
    leads: List[int] = []
    for c in claims:
        by_res[c.resolution] = by_res.get(c.resolution, 0) + 1
        if c.lead_minutes is not None and c.lead_minutes >= 0:
            leads.append(c.lead_minutes)
    leads.sort()
    median_lead = leads[len(leads) // 2] if leads else None
    resolved = by_res["CORRECT"] + by_res["WRONG"]
    return {
        "claims_total": total,
        "by_resolution": by_res,
        "resolved": resolved,
        "accuracy": round(by_res["CORRECT"] / resolved, 4) if resolved else None,
        "median_lead_minutes": median_lead,
        "first_report_credit": _first_report_credit(claims),
    }


def _first_report_credit(claims: List[Claim]) -> Dict[str, int]:
    """Who was earliest per (player, predicted_status) -- i.e. who 'broke' it."""

    earliest: Dict[Tuple[str, str], Claim] = {}
    for c in claims:
        if c.resolution not in ("CORRECT", "PENDING"):
            continue
        k = (c.player_key, c.predicted_status)
        ts = _parse_epoch(c.posted_at)
        if ts is None:
            continue
        cur = earliest.get(k)
        if cur is None or ts < (_parse_epoch(cur.posted_at) or float("inf")):
            earliest[k] = c
    credit: Dict[str, int] = {}
    for c in earliest.values():
        if c.author:
            credit[c.author] = credit.get(c.author, 0) + 1
    return dict(sorted(credit.items(), key=lambda kv: -kv[1])[:25])
