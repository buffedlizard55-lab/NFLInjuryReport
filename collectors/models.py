"""Canonical data model + status normalization shared by every collector.

Keeping one vocabulary here is what makes cross-source reconciliation possible:
nfl.com writes "Did Not Participate In Practice", ESPN writes
"Questionable", RotoWire writes "Q". All collapse to the same enum.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- statuses ---

#: NFL official game-status vocabulary, as published on the league's own report
#: (https://www.nfl.com/injuries/). The Personnel (Injury) Report Policy that
#: defines it is listed in NFL_POLICY_URL_CANDIDATES below -- those PDFs returned
#: HTTP 404 to this project's fetcher, so they are probe-only.
GAME_STATUSES = {
    "OUT",           # will not play
    "DOUBTFUL",      # unlikely to play
    "QUESTIONABLE",  # uncertain to play
    "ACTIVE",        # cleared / will play
    "IR",            # injured reserve
    "PUP",           # physically unable to perform
    "SUSPENDED",
    "NFI",           # non-football injury
    "UNKNOWN",
}

STATUS_ALIASES = {
    "o": "OUT",
    "out": "OUT",
    "d": "DOUBTFUL",
    "doubtful": "DOUBTFUL",
    "q": "QUESTIONABLE",
    "questionable": "QUESTIONABLE",
    "probable": "ACTIVE",   # NFL retired "Probable" in 2016; treat as active
    "p": "ACTIVE",
    "active": "ACTIVE",
    "plays": "ACTIVE",
    "cleared": "ACTIVE",
    "full-go": "ACTIVE",
    "ir": "IR",
    "ir-r": "IR",
    "injured reserve": "IR",
    "reserve-ir": "IR",
    "pup": "PUP",
    "pup-r": "PUP",
    "reserve/pup": "PUP",
    "sus": "SUSPENDED",
    "suspension": "SUSPENDED",
    "reserve-sus": "SUSPENDED",
    "nfi": "NFI",
    "reserve-nfi": "NFI",
    "dnr": "UNKNOWN",       # "did not report"
    "ret": "UNKNOWN",       # reserve-retired
    "cel": "UNKNOWN",       # commissioner exempt list
}

PRACTICE_ALIASES = {
    "full": "FULL",
    "full participation in practice": "FULL",
    "full participation": "FULL",
    "limited": "LIMITED",
    "limited participation in practice": "LIMITED",
    "limited participation": "LIMITED",
    "dnp": "DNP",
    "did not participate in practice": "DNP",
    "did not participate": "DNP",
    "not participating": "DNP",
    "": "NONE",
}

#: Ranking used to decide whether a change is an escalation or a de-escalation.
#: Lower = healthier.
SEVERITY_ORDER = ["ACTIVE", "QUESTIONABLE", "DOUBTFUL", "OUT", "IR", "PUP", "NFI", "SUSPENDED"]


def norm_status(value: Optional[str]) -> str:
    if not value:
        return "UNKNOWN"
    key = value.strip().lower()
    if key in STATUS_ALIASES:
        return STATUS_ALIASES[key]
    compact = re.sub(r"[^a-z]", "", key)
    if compact in STATUS_ALIASES:
        return STATUS_ALIASES[compact]
    for needle, canon in STATUS_ALIASES.items():
        if len(needle) > 3 and needle in compact:
            return canon
    return "UNKNOWN"


def norm_practice(value: Optional[str]) -> str:
    if value is None:
        return "NONE"
    key = value.strip().lower()
    if key in PRACTICE_ALIASES:
        return PRACTICE_ALIASES[key]
    if "did not" in key or key.startswith("dnp"):
        return "DNP"
    if "limited" in key:
        return "LIMITED"
    if "full" in key:
        return "FULL"
    return "NONE"


def severity_rank(status: str) -> int:
    try:
        return SEVERITY_ORDER.index(status)
    except ValueError:
        return -1


def is_escalation(old: str, new: str) -> bool:
    """True when the new status is worse (less available) than the old one."""
    o, n = severity_rank(old), severity_rank(new)
    if o < 0 or n < 0:
        return old != new
    return n > o


# ------------------------------------------------------------ identifiers ---

def slugify(name: str) -> str:
    """Stable player key: 'A.J. Brown' -> 'aj-brown'.

    NFL.com, ESPN and RotoWire all use different player IDs, and none of them is
    published as a crosswalk, so the roster key is a normalized name. That is a
    known limitation and it is what the reconciler's fuzzy matcher compensates
    for (see reconcile.py).
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"\b(jr|sr|ii|iii|iv)\.?\b", "", text)
    # Drop periods BEFORE hyphenating so initials collapse: both "A.J. Brown" and
    # "AJ Brown" must produce the same key or the matcher will miss one spelling.
    text = text.replace(".", "")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def short_id(*parts: str) -> str:
    joined = "|".join(p for p in parts if p)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------- records ---

@dataclass
class PlayerInjury:
    """One (player, team, observed_at) injury row from one source."""

    source: str                 # "nfl.com" | "espn" | "rotowire"
    team: str                   # NFL club code, e.g. "SEA"
    player: str                 # display name as published
    position: str = ""
    injury: str = ""
    game_status: str = "UNKNOWN"
    practice_status: str = "NONE"
    #: "published" when the source actually printed a game designation,
    #: "inferred" when we derived one (nfl.com leaves Game Status blank for a
    #: player who practised without a designation). Never conflated downstream.
    designation_source: str = ""
    est_return: str = ""
    comment: str = ""
    attribution: str = ""       # reporter/beat writer named in the comment
    observed_at: str = ""       # UTC ISO8601, from the source when available
    url: str = ""               # deep link for manual verification
    player_key: str = ""
    source_ids: Dict[str, str] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.player_key:
            self.player_key = slugify(self.player)
        self.game_status = norm_status(self.game_status)
        self.practice_status = norm_practice(self.practice_status)

    @property
    def identity(self) -> str:
        return short_id(self.source, self.team, self.player_key)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["id"] = self.identity
        return d


@dataclass
class Claim:
    """A reporter/social assertion that a player has a given injury status."""

    claim_id: str
    platform: str               # bluesky | mastodon | reddit | google-news | espn-attribution
    author: str                 # handle or outlet name
    author_url: str
    player_key: str
    player: str
    team: str
    predicted_status: str
    injury: str
    text: str
    posted_at: str
    url: str
    resolution: str = "PENDING"  # PENDING | CORRECT | WRONG | UNVERIFIABLE
    resolved_at: str = ""
    evidence_url: str = ""
    lead_minutes: Optional[int] = None
    tier: str = ""               # reporter tier from reporters.py

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Alert:
    ts: str
    kind: str                    # status-change | new-injury | cleared | removed |
                                 # in-game
    severity: str                # low | medium | high | critical
    player: str
    player_key: str
    team: str
    position: str
    injury: str
    from_status: str
    to_status: str
    detail: str
    sources: List[Dict[str, str]] = field(default_factory=list)
    #: Stable id for the append-only alert log. Two runs that see the same event
    #: produce the same id, so a user who opens the site hours later still sees the
    #: alert (and its original detection time) exactly once.
    alert_id: str = ""
    #: In-game event key (see collectors/ingame.py) when kind == "in-game".
    event_key: str = ""
    #: When THIS pipeline first saw the alert, as opposed to `ts`, which is what
    #: the source said. Both are shown, because the difference is the honesty gap.
    first_seen_at: str = ""
    reported_at: str = ""
    detection_latency_seconds: Optional[int] = None
    source_verified: bool = False
    in_game: bool = False
    #: Verbatim sentence(s) the alert is based on.
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Irregularity:
    """Anything the reconciler could not reconcile, or that looks wrong upstream."""

    code: str
    severity: str
    title: str
    detail: str
    evidence: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


OFFICIAL_NFL_INJURY_URL = "https://www.nfl.com/injuries/"
#: Candidate URLs for the league's Personnel (Injury) Report Policy.
#:
#: HONEST STATUS: neither of these resolved for this project's fetcher. The path
#: operations.nfl.com/gameday/injury-report/ returned HTTP 404 and
#: operations.nfl.com/media/2683/2017-nfl-injury-report-policy.pdf returned HTTP
#: 404 as well when probed from a GitHub Actions runner on 2026-09-10, even
#: though search engines index the PDF. They are therefore PROBE-ONLY: they are
#: reported in the source ledger so a human can check them, and they are NOT
#: used as evidence links anywhere in the product.
#:
#: The policy still matters for correctness, because it is what justifies
#: rendering a blank Game Status as available: it instructs clubs that a player
#: who is "not injured but has been rested in practice should not be listed on
#: the Game Status Report with an injury status designation (Out, Doubtful, or
#: Questionable)" while still appearing on the Practice Report.
NFL_POLICY_URL_CANDIDATES = [
    "https://operations.nfl.com/media/2683/2017-nfl-injury-report-policy.pdf",
    "https://operations.nfl.com/media/2235/06-07-16-2016-injury-report-policy.pdf",
]

