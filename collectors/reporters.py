"""Reporter registry and scorecard bookkeeping.

WHY THERE IS NO CURATED "TOP REPORTERS" LIST SHIPPED HERE
----------------------------------------------------------
The brief requires no hallucinations. A hand-written list of "trusted verified
insiders" with their social handles is exactly the kind of artefact that rots and
that nobody can audit, and any handle written here without being observed live
would be an unverified claim. So the registry is BUILT FROM OBSERVED EVIDENCE:

  1. ESPN's injury payload names the beat writer inside `shortComment`
     (e.g. "...Dani Sureck of the Cardinals' official site reports."). Verified
     live 2026-09-10. That is a real, per-update attribution string from a
     mainstream source, and it is the primary way reporters enter the registry.
  2. Social adapters add any author who posts an injury-relevant claim.
  3. Every entry records `first_seen` / `last_seen` / `evidence_urls` so a human
     can click through and decide whether the account is genuine.

Tiering is therefore earned, not asserted: an author starts at tier "observed"
and is promoted to "established" once the scorecard has enough resolved claims.
A human-review flag is emitted for any author whose claims keep being wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import Claim

#: Minimum resolved claims before an accuracy figure is published at all.
MIN_RESOLVED_FOR_RATE = 5
#: Below this accuracy (with enough volume) the author is flagged for review.
FLAG_BELOW = 0.60


@dataclass
class Reporter:
    key: str
    name: str
    platform: str
    handle: str = ""
    outlet: str = ""
    url: str = ""
    tier: str = "observed"          # observed | established | flagged
    first_seen: str = ""
    last_seen: str = ""
    claims_total: int = 0
    claims_correct: int = 0
    claims_wrong: int = 0
    claims_unverifiable: int = 0
    lead_minutes_sum: int = 0
    lead_minutes_n: int = 0
    evidence_urls: List[str] = field(default_factory=list)

    # -- derived ------------------------------------------------------------
    @property
    def resolved(self) -> int:
        return self.claims_correct + self.claims_wrong

    @property
    def accuracy(self) -> Optional[float]:
        if self.resolved == 0:
            return None
        return self.claims_correct / self.resolved

    @property
    def avg_lead_minutes(self) -> Optional[float]:
        if not self.lead_minutes_n:
            return None
        return self.lead_minutes_sum / self.lead_minutes_n

    @property
    def score(self) -> Optional[float]:
        """0-100 composite. Accuracy dominates; lead time is a bounded bonus.

        Published only once MIN_RESOLVED_FOR_RATE claims have resolved, so a
        reporter who got one lucky early call is not shown as 100.
        """

        acc = self.accuracy
        if acc is None or self.resolved < MIN_RESOLVED_FOR_RATE:
            return None
        lead = self.avg_lead_minutes or 0.0
        # 0..10 bonus that saturates at a 4-hour lead.
        lead_bonus = 10.0 * min(1.0, max(0.0, lead) / 240.0)
        volume_confidence = min(1.0, self.resolved / 20.0)
        raw = 100.0 * acc * volume_confidence + lead_bonus * volume_confidence
        return round(min(100.0, raw), 1)

    @property
    def wilson_lower(self) -> Optional[float]:
        """Lower bound of a 95% Wilson interval -- penalises small samples.

        Used as the honest headline figure, because with 2 claims "100% accurate"
        is meaningless.
        """

        n = self.resolved
        if n == 0:
            return None
        p = self.claims_correct / n
        z = 1.959963985
        denom = 1 + z * z / n
        centre = p + z * z / (2 * n)
        margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
        return round((centre - margin) / denom, 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "platform": self.platform,
            "handle": self.handle, "outlet": self.outlet, "url": self.url,
            "tier": self.tier, "first_seen": self.first_seen, "last_seen": self.last_seen,
            "claims_total": self.claims_total, "claims_correct": self.claims_correct,
            "claims_wrong": self.claims_wrong,
            "claims_unverifiable": self.claims_unverifiable,
            "resolved": self.resolved,
            "accuracy": round(self.accuracy, 4) if self.accuracy is not None else None,
            "wilson_lower": self.wilson_lower,
            "avg_lead_minutes": round(self.avg_lead_minutes, 1)
            if self.avg_lead_minutes is not None else None,
            "score": self.score,
            "evidence_urls": self.evidence_urls[:8],
        }


def reporter_key(platform: str, handle: str, name: str = "") -> str:
    base = handle.strip().lower().lstrip("@") or re.sub(r"\W+", "-", name.strip().lower())
    return f"{platform}:{base}"


class ReporterRegistry:
    def __init__(self) -> None:
        self.reporters: Dict[str, Reporter] = {}

    # -- mutation -----------------------------------------------------------
    def observe(
        self,
        *,
        platform: str,
        handle: str = "",
        name: str = "",
        outlet: str = "",
        url: str = "",
        ts: str = "",
    ) -> Reporter:
        key = reporter_key(platform, handle, name)
        rec = self.reporters.get(key)
        if rec is None:
            rec = Reporter(
                key=key, name=name or handle, platform=platform, handle=handle,
                outlet=outlet, url=url, first_seen=ts, last_seen=ts,
            )
            self.reporters[key] = rec
        else:
            if not rec.first_seen or (ts and ts < rec.first_seen):
                rec.first_seen = ts
            if name and (not rec.name or rec.name == rec.handle):
                rec.name = name
            if outlet:
                rec.outlet = outlet
            if url:
                rec.url = url
        if ts and (not rec.last_seen or ts > rec.last_seen):
            rec.last_seen = ts
        return rec

    def record_claim(self, claim: Claim) -> Reporter:
        rec = self.observe(
            platform=claim.platform, handle=claim.author, name=claim.author,
            outlet=claim.tier, url=claim.author_url, ts=claim.posted_at,
        )
        rec.claims_total += 1
        if claim.url and claim.url not in rec.evidence_urls:
            rec.evidence_urls.append(claim.url)
        return rec

    def resolve(self, claim: Claim) -> None:
        rec = self.reporters.get(reporter_key(claim.platform, claim.author, claim.author))
        if rec is None:
            return
        if claim.resolution == "CORRECT":
            rec.claims_correct += 1
        elif claim.resolution == "WRONG":
            rec.claims_wrong += 1
        elif claim.resolution == "UNVERIFIABLE":
            rec.claims_unverifiable += 1
        if claim.lead_minutes is not None and claim.lead_minutes >= 0:
            rec.lead_minutes_sum += int(claim.lead_minutes)
            rec.lead_minutes_n += 1

    def retier(self) -> List[Dict[str, str]]:
        """Promote or flag reporters based on evidence. Returns the changes."""

        changes: List[Dict[str, str]] = []
        for rec in self.reporters.values():
            previous = rec.tier
            if rec.resolved >= 10 and (rec.accuracy or 0) < FLAG_BELOW:
                rec.tier = "flagged"
            elif rec.resolved >= MIN_RESOLVED_FOR_RATE and (rec.accuracy or 0) >= 0.8:
                rec.tier = "established"
            else:
                rec.tier = "observed"
            if rec.tier != previous:
                changes.append({"key": rec.key, "from": previous, "to": rec.tier})
        return changes

    # -- serialisation ------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        rows = sorted(
            (r.to_dict() for r in self.reporters.values()),
            key=lambda r: (
                -(r["score"] if r["score"] is not None else -1),
                -(r["resolved"] or 0),
                r["name"],
            ),
        )
        return {
            "count": len(rows),
            "min_resolved_for_rate": MIN_RESOLVED_FOR_RATE,
            "flag_below_accuracy": FLAG_BELOW,
            "reporters": rows,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReporterRegistry":
        reg = cls()
        for row in (data or {}).get("reporters", []):
            rec = Reporter(
                key=row["key"], name=row.get("name", ""), platform=row.get("platform", ""),
                handle=row.get("handle", ""), outlet=row.get("outlet", ""),
                url=row.get("url", ""), tier=row.get("tier", "observed"),
                first_seen=row.get("first_seen", ""), last_seen=row.get("last_seen", ""),
                claims_total=row.get("claims_total", 0),
                claims_correct=row.get("claims_correct", 0),
                claims_wrong=row.get("claims_wrong", 0),
                claims_unverifiable=row.get("claims_unverifiable", 0),
                evidence_urls=list(row.get("evidence_urls", [])),
            )
            lead = row.get("avg_lead_minutes")
            n = row.get("resolved") or 0
            if lead is not None:
                rec.lead_minutes_sum = int(round(lead * n))
                rec.lead_minutes_n = n
            reg.reporters[rec.key] = rec
        return reg
