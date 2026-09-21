"""Social / secondary verification layer.

Design rule: every adapter is PROBED before it is trusted, and its probe result is
written to data/latest/health.json. Nothing in this file asserts that a platform
is reachable -- the pipeline records what actually happened on each run.

Source ledger (verified 2026-09-10 unless noted)
------------------------------------------------
Bluesky      SPLIT RESULT, RE-MEASURED 2026-09-18. The two endpoints behave
             differently and must not be probed as if they were one platform:
               * app.bsky.feed.searchPosts  -> HTTP 403. Re-probed 2026-09-18 from
                 the CI runner AND from an independent client IP: still 403.
                 Unauthenticated full-text search is NOT available to this project.
               * app.bsky.feed.getAuthorFeed -> HTTP 200, keyless, and it carries
                 the author's own verification state
                 (author.verification.verifiedStatus == "valid" for a
                 Bluesky-verified account). This is the path that actually works.
             Consequence (this is the bug that was fixed on 2026-09-18): the old
             orchestrator only called getAuthorFeed for handles in
             data/state/watched.json, which was EMPTY, and it only used
             searchPosts for discovery, which is 403. So no insider post was ever
             read. Meanwhile Ian Rapoport's verified Bluesky account posted the
             DJ Moore shoulder injury at 2026-09-18T01:37:12Z -- 35 minutes
             before ESPN's injuries feed carried the same update (02:12Z) -- and
             this pipeline never saw it. Verified handles are now fetched
             directly (see WATCHED_INSIDER_HANDLES / collect_social).
Mastodon     Public timelines/tag timelines are keyless by protocol design.
             Instance reachability is instance-specific, so it is probed.
Google News  Keyless RSS. Probed.
Reddit       CONFLICTING EVIDENCE -- documented as still open (~60 req/min
             unauthenticated) by one source and as "broadly blocked, usually 403,
             since May 30 2026" by another. Therefore: probe first, and if the
             probe fails the adapter is disabled for the run instead of retrying
             into a block. Flagged in the README.
X / Twitter  NOT AVAILABLE FOR FREE. X discontinued its free tier for new
             developers and moved to pay-per-use (roughly $0.005 per post read);
             the old Basic/Pro tiers are closed to new signups. This project
             therefore does NOT scrape X. Instead it emits deep X search links per
             player so a human can verify a claim in one click, and it accepts an
             optional paid adapter via X_BEARER_TOKEN if one is ever supplied.
             Refs: https://api.sorsa.io/blog/is-twitter-api-free
                   https://www.socialcrawl.dev/blog/x-twitter-api-2026
Instagram /  No keyless public read API exists. Excluded on purpose rather than
Facebook     replaced with a fragile scraper.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from .http import FetchError, fetch_json, fetch_text, probe, utc_now_iso
from .ingame import _epoch as _parse_instant
from .ingame import classify_game_event, find_injury_word
from .models import Irregularity, norm_status

# ------------------------------------------------------------------ queries --

#: Terms that, combined with a player name, indicate an injury report.
INJURY_TERMS = [
    "injury", "injured", "injure", "hurt", "ankle", "knee", "hamstring", "quad",
    "groin", "shoulder", "concussion", "back", "foot", "wrist", "hand", "hip",
    "calf", "elbow", "neck", "achilles", "acl", "mcl", "pec", "bicep", "rib",
    "illness", "ill", "calf strain", "ruled out", "doubtful", "questionable",
    "out for", "week", "ir", "injured reserve", "practice", "limited",
    "did not participate", "dnp", "cart", "x-ray", "mri", "surgery", "placed on",
]

STATUS_PATTERNS: List[tuple] = [
    (re.compile(r"\bruled\s+out\b|\bout\s+for\s+(?:the\s+)?(?:season|week|game)\b|\bwill\s+not\s+play\b|\bout\s+vs\.?\b", re.I), "OUT"),
    (re.compile(r"\bplaced\s+on\s+(?:injured\s+)?\s*ir\b|\binjured\s+reserve\b", re.I), "IR"),
    (re.compile(r"\bdoubtful\b|\bunlikely\s+to\s+play\b", re.I), "DOUBTFUL"),
    (re.compile(r"\bquestionable\b|\bgame[\s-]?time\s+decision\b|\buncertain\b|\b50/50\b", re.I), "QUESTIONABLE"),
    (re.compile(r"\bactive\b|\bfull\s+go\b|\bcleared\b|\bwill\s+play\b|\bplays\b|\bno\s+longer\s+on\b", re.I), "ACTIVE"),
    (re.compile(r"\bsuspend(?:ed|ion)\b", re.I), "SUSPENDED"),
]

INJURY_WORD_RE = re.compile(
    r"\b(ankle|knee|hamstring|quadriceps|quad|groin|shoulder|concussion|back|foot|"
    r"wrist|hand|hip|calf|elbow|neck|achilles|acl|mcl|lcl|pec|bicep|rib|ribs|"
    r"toe|thumb|finger|illness|abdomen|oblique|core|thumb|wrist)\b",
    re.I,
)


@dataclass
class SocialPost:
    platform: str
    post_id: str
    author: str
    author_name: str
    author_url: str
    text: str
    posted_at: str
    url: str
    matched_player: str = ""
    player_key: str = ""
    team: str = ""
    predicted_status: str = "UNKNOWN"
    injury: str = ""
    signal: float = 0.0
    engagement: int = 0
    #: platform-level identity verification, taken from the platform's own API
    #: response at fetch time (Bluesky: author.verification.verifiedStatus ==
    #: "valid"). Never asserted from a name alone.
    verified: bool = False
    verification_detail: str = ""
    author_did: str = ""
    #: provenance class so the UI can separate a Bluesky-verified insider from a
    #: news wire, a club site and an open social timeline:
    #: verified-insider | insider-candidate | wire | news | club | social | unknown
    source_kind: str = "unknown"
    #: in-game availability stated by the text (see collectors/ingame.py). This is
    #: deliberately separate from `predicted_status`, which is the roster axis.
    in_game_status: str = "NONE"
    game_event: str = ""
    raw: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.raw is None:
            self.raw = {}
        self.predicted_status = norm_status(self.predicted_status)
        if self.in_game_status == "NONE" and self.text:
            ev = classify_game_event(self.text)
            self.in_game_status = ev["status"]
            self.game_event = self.game_event or ev["event"]
        if not self.injury and self.text:
            self.injury = find_injury_word(self.text)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform, "post_id": self.post_id, "author": self.author,
            "author_name": self.author_name, "author_url": self.author_url,
            "text": self.text, "posted_at": self.posted_at, "url": self.url,
            "matched_player": self.matched_player, "player_key": self.player_key,
            "team": self.team, "predicted_status": self.predicted_status,
            "injury": self.injury, "signal": round(self.signal, 3),
            "engagement": self.engagement, "verified": self.verified,
            "verification_detail": self.verification_detail,
            "author_did": self.author_did, "source_kind": self.source_kind,
            "in_game_status": self.in_game_status, "game_event": self.game_event,
        }


# ---------------------------------------------------------------- classifiers --

def classify(text: str) -> Dict[str, Any]:
    """Return (status, injury, signal) evidence found in free text. Never invents."""

    if not text:
        return {"status": "UNKNOWN", "injury": "", "signal": 0.0, "terms": []}
    status = "UNKNOWN"
    for rx, canon in STATUS_PATTERNS:
        if rx.search(text):
            status = canon
            break
    m = INJURY_WORD_RE.search(text)
    injury = m.group(1).lower() if m else ""
    low = text.lower()
    terms = [t for t in INJURY_TERMS if t in low]
    signal = min(1.0, 0.15 * len(terms) + (0.4 if status != "UNKNOWN" else 0.0) + (0.3 if injury else 0.0))
    return {"status": status, "injury": injury, "signal": signal, "terms": terms}


def is_injury_post(text: str, threshold: float = 0.3) -> bool:
    return classify(text)["signal"] >= threshold


# ------------------------------------------------------------------ bluesky --

BSKY_SEARCH = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
BSKY_AUTHOR_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
BSKY_PROFILE = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile"


def bsky_post_url(uri: str, handle: str) -> str:
    """at://did:plc:abc/app.bsky.feed.post/3kxyz -> https://bsky.app/profile/h/post/3kxyz"""
    m = re.search(r"app\.bsky\.feed\.post/([^/]+)$", uri or "")
    if not m or not handle:
        return ""
    return f"https://bsky.app/profile/{handle}/post/{m.group(1)}"


def _bsky_author_verification(author: Dict[str, Any]) -> tuple:
    """(verified, detail, did) straight from the platform's own response.

    Bluesky embeds ``verification.verifiedStatus`` on the author view. Verified
    live 2026-09-18 for rapsheet.bsky.social ("valid", issuer Bluesky). An account
    with no badge returns "" here -- it is never upgraded by a name match.
    """

    ver = (author or {}).get("verification") or {}
    status = ver.get("verifiedStatus") or ""
    issuers = [
        v.get("issuerDisplayName") or v.get("issuer") or ""
        for v in (ver.get("verifications") or [])
        if v.get("isValid")
    ]
    detail = ""
    if status:
        detail = f"Bluesky verification status={status}"
        if issuers:
            detail += f" (issuer: {', '.join(sorted(set(i for i in issuers if i)))})"
    return status == "valid", detail, (author or {}).get("did") or ""


def fetch_bluesky(query: str, *, limit: int = 100, sort: str = "latest") -> Dict[str, Any]:
    """Search Bluesky without authentication (public AppView endpoint).

    HONEST STATUS (2026-09-18): this endpoint returns HTTP 403 to this project
    from both a CI runner and an independent client IP. It is kept because the
    probe result is published in health.json and because the endpoint may open up
    again; the collector does NOT depend on it (see fetch_bluesky_author).
    """

    from urllib.parse import urlencode

    url = f"{BSKY_SEARCH}?{urlencode({'q': query, 'limit': min(limit, 100), 'sort': sort})}"
    payload = fetch_json(url, source="bluesky")
    posts: List[SocialPost] = []
    for item in payload.get("posts") or []:
        author = item.get("author") or {}
        record = item.get("record") or {}
        handle = author.get("handle") or ""
        uri = item.get("uri") or ""
        text = record.get("text") or ""
        cls = classify(text)
        verified, detail, did = _bsky_author_verification(author)
        posts.append(
            SocialPost(
                platform="bluesky",
                post_id=uri,
                author=handle,
                author_name=author.get("displayName") or handle,
                author_url=f"https://bsky.app/profile/{handle}" if handle else "",
                text=text,
                posted_at=record.get("createdAt") or item.get("indexedAt") or "",
                url=bsky_post_url(uri, handle),
                predicted_status=cls["status"],
                injury=cls["injury"],
                signal=cls["signal"],
                engagement=int(item.get("likeCount") or 0) + int(item.get("repostCount") or 0),
                verified=verified, verification_detail=detail, author_did=did,
                source_kind="verified-insider" if verified else "social",
                raw={"likeCount": item.get("likeCount"), "repostCount": item.get("repostCount"),
                     "replyCount": item.get("replyCount"), "did": author.get("did")},
            )
        )
    return {"platform": "bluesky", "query": query, "url": url, "posts": posts,
            "cursor": payload.get("cursor"), "hits_total": payload.get("hitsTotal")}


def fetch_bluesky_author(
    handle: str, *, limit: int = 50, insider: bool = False
) -> Dict[str, Any]:
    """Latest posts from one account -- the working keyless Bluesky read path.

    Verified 2026-09-18: HTTP 200 with no token, and the response states the
    author's own verification status. `insider=True` marks the account as one the
    project deliberately follows (seed/verification directory) rather than one
    found by search; the per-post `verified` flag still comes from the API, so a
    followed account that loses its badge is reported as unverified.
    """

    from urllib.parse import urlencode

    url = f"{BSKY_AUTHOR_FEED}?{urlencode({'actor': handle, 'limit': min(limit, 100)})}"
    payload = fetch_json(url, source="bluesky")
    posts: List[SocialPost] = []
    for item in payload.get("feed") or []:
        post = item.get("post") or {}
        author = post.get("author") or {}
        record = post.get("record") or {}
        h = author.get("handle") or handle
        uri = post.get("uri") or ""
        text = record.get("text") or ""
        cls = classify(text)
        verified, detail, did = _bsky_author_verification(author)
        posts.append(
            SocialPost(
                platform="bluesky", post_id=uri, author=h,
                author_name=author.get("displayName") or h,
                author_url=f"https://bsky.app/profile/{h}", text=text,
                posted_at=record.get("createdAt") or post.get("indexedAt") or "",
                url=bsky_post_url(uri, h),
                predicted_status=cls["status"], injury=cls["injury"], signal=cls["signal"],
                engagement=int(post.get("likeCount") or 0),
                verified=verified, verification_detail=detail, author_did=did,
                source_kind=("verified-insider" if verified and insider
                             else "insider-candidate" if insider else "social"),
                raw={"watched_handle": handle, "did": author.get("did")},
            )
        )
    return {"platform": "bluesky", "query": f"author:{handle}", "url": url, "posts": posts}


# ----------------------------------------------------------------- mastodon --

MASTODON_DEFAULT_INSTANCE = "https://mastodon.social"


def fetch_mastodon_tag(tag: str, *, instance: str = MASTODON_DEFAULT_INSTANCE,
                       limit: int = 40) -> Dict[str, Any]:
    url = f"{instance.rstrip('/')}/api/v1/timelines/tag/{tag}?limit={min(limit, 40)}"
    payload = fetch_json(url, source="mastodon")
    posts: List[SocialPost] = []
    for item in payload if isinstance(payload, list) else []:
        acct = (item.get("account") or {}).get("acct") or ""
        html_text = item.get("content") or ""
        text = re.sub(r"<[^>]+>", "", html_text)
        cls = classify(text)
        posts.append(
            SocialPost(
                platform="mastodon", post_id=item.get("id") or "", author=acct,
                author_name=(item.get("account") or {}).get("display_name") or acct,
                author_url=item.get("url") or "", text=text,
                posted_at=item.get("created_at") or "", url=item.get("url") or "",
                predicted_status=cls["status"], injury=cls["injury"], signal=cls["signal"],
                engagement=int(item.get("favourites_count") or 0) + int(item.get("reblogs_count") or 0),
                raw={"instance": instance},
            )
        )
    return {"platform": "mastodon", "query": f"#{tag}", "url": url, "posts": posts}


# --------------------------------------------------------------- google news --

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

#: Query budget. Google News RSS is keyless and cheap, but each query is a real
#: request, so the collector spends them where an injury is most likely to be
#: reported *right now*:
#:   1. the league-wide injury query (always),
#:   2. every club currently in a live/finished game window ("hot"),
#:   3. the players the pipeline flagged as hot. While a game is in progress
#:      that is EVERY player on the live teams (the pipeline decides; see
#:      _hot_players) — a headline about an in-game injury often names only the
#:      player and never the club, so club-level queries alone cannot find it.
#:      Outside a game window the pipeline passes a small fallback budget.
#:      GOOGLE_NEWS_PLAYER_QUERY_CAP is only a hard safety bound on request
#:      volume (several simultaneous games, unexpectedly large rosters).
#:   4. a rotating slice of the other 28 clubs so full coverage is still reached
#:      several times an hour without 32 simultaneous queries.
GOOGLE_NEWS_PLAYER_QUERY_CAP = 300
GOOGLE_NEWS_ROTATION_SIZE = 8
GOOGLE_NEWS_ROTATION_SECONDS = 600


def google_news_injury_query(subject: str, *, window: str = "1d") -> str:
    """Build a quoted, recency-windowed injury query.

    `when:1d` is Google News' own recency operator; a headline older than a day
    is not an in-game update. Verified live 2026-09-18 that this operator is
    accepted by the RSS endpoint (see tests/fixtures for the captured response
    shape).
    """

    q = f'"{subject}" injury'
    return f"{q} when:{window}" if window else q


def fetch_google_news(query: str, *, limit: int = 40,
                      source_kind: str = "news") -> Dict[str, Any]:
    from urllib.parse import urlencode

    url = f"{GOOGLE_NEWS_RSS}?{urlencode({'q': query, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})}"
    xml_text = fetch_text(url, source="google-news")
    posts: List[SocialPost] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {"platform": "google-news", "query": query, "url": url, "posts": [],
                "error": f"RSS parse error: {exc}"}
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source = item.find("source")
        outlet = (source.text or "").strip() if source is not None and source.text else ""
        cls = classify(title)
        posts.append(
            SocialPost(
                platform="google-news", post_id=link, author=outlet, author_name=outlet,
                author_url=(source.attrib.get("url") if source is not None else "") or "",
                text=title, posted_at=pub, url=link,
                predicted_status=cls["status"], injury=cls["injury"], signal=cls["signal"],
                source_kind=source_kind,
                raw={"outlet": outlet, "query": query},
            )
        )
        if len(posts) >= limit:
            break
    return {"platform": "google-news", "query": query, "url": url, "posts": posts}


# -------------------------------------------------------------------- reddit --

REDDIT_SUBS = ["NFL_Discussion", "fantasyfootball", "NFLNoobs"]


def fetch_reddit(subreddit: str, *, limit: int = 50) -> Dict[str, Any]:
    """Public JSON listing. Availability is contested -- caller must probe first."""

    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={min(limit, 100)}&raw_json=1"
    payload = fetch_json(url, source="reddit")
    posts: List[SocialPost] = []
    children = ((payload.get("data") or {}).get("children")) or []
    for child in children:
        d = child.get("data") or {}
        text = f"{d.get('title') or ''} {d.get('selftext') or ''}".strip()
        cls = classify(text)
        posts.append(
            SocialPost(
                platform="reddit", post_id=d.get("id") or "", author=d.get("author") or "",
                author_name=d.get("author") or "",
                author_url=f"https://www.reddit.com/user/{d.get('author')}" if d.get("author") else "",
                text=text[:1000],
                posted_at=_epoch_to_iso(d.get("created_utc")),
                url=f"https://www.reddit.com{d.get('permalink')}" if d.get("permalink") else "",
                predicted_status=cls["status"], injury=cls["injury"], signal=cls["signal"],
                engagement=int(d.get("score") or 0),
                raw={"subreddit": subreddit, "num_comments": d.get("num_comments")},
            )
        )
    return {"platform": "reddit", "query": f"r/{subreddit}", "url": url, "posts": posts}


def _epoch_to_iso(value: Any) -> str:
    import time

    if not value:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
    except (TypeError, ValueError):
        return ""


# ------------------------------------------------------------------ X links --

def x_search_url(player_name: str, team_code: str = "") -> str:
    """Deep link for MANUAL verification on X. Requires no API key.

    We cannot ingest X for free (see module docstring), so instead of silently
    omitting it we hand the reviewer a one-click search that lands on the live
    conversation about that player.
    """

    from urllib.parse import quote

    q = f'"{player_name}" injury'
    if team_code:
        q += f" OR #{team_code}"
    return f"https://x.com/search?q={quote(q)}&src=typed_query&f=live"


def x_author_search_url(handle: str) -> str:
    return f"https://x.com/search?q={handle}%20injury&src=typed_query&f=live"


# ------------------------------------------------------------- orchestration --

#: Each probe must hit the endpoint the adapter actually calls. Probing
#: app.bsky.actor.getProfile returned HTTP 200 on 2026-09-10 while
#: app.bsky.feed.searchPosts returned HTTP 403 from the same host, so a
#: getProfile probe would have reported Bluesky as usable when search was not.
#:
#: 2026-09-18 addendum: the same mistake must not be repeated in reverse.
#: searchPosts (403) and getAuthorFeed (200) are different endpoints on the same
#: host with opposite availability, so they get separate probes and separate
#: flags: missing search costs nothing (discovery falls back to Google News), a
#: missing author feed means the verified-insider path is down and is reported as
#: a medium-severity irregularity.
PLATFORM_PROBES = {
    "bluesky": (
        BSKY_SEARCH + "?q=nfl%20injury&limit=1&sort=latest", "bluesky"),
    "bluesky-author-feed": (
        BSKY_AUTHOR_FEED + "?actor=rapsheet.bsky.social&limit=1", "bluesky"),
    "mastodon": (f"{MASTODON_DEFAULT_INSTANCE}/api/v1/instance", "mastodon"),
    "google-news": (f"{GOOGLE_NEWS_RSS}?q=nfl&hl=en-US&gl=US&ceid=US:en", "google-news"),
    "reddit": ("https://www.reddit.com/r/NFL_Discussion/about.json?raw_json=1", "reddit"),
}

#: Platforms whose failure is expected/documented rather than a surprise outage,
#: with the severity the flag should carry.
PLATFORM_FLAG_SEVERITY = {
    "bluesky": "low",          # documented 403; discovery falls back to Google News
    "bluesky-author-feed": "medium",   # this one is load-bearing
    "mastodon": "medium",
    "google-news": "medium",
    "reddit": "low",           # contested availability since 2026 (see module doc)
}

PLATFORM_NOTES = {
    "bluesky": (
        "app.bsky.feed.searchPosts is HTTP 403 for this project (re-measured "
        "2026-09-18 from the CI runner and from an independent client IP). "
        "Unauthenticated Bluesky search is therefore unavailable; discovery comes "
        "from Google News and from the verified handles this project follows "
        "directly (app.bsky.feed.getAuthorFeed, which does work)."
    ),
    "bluesky-author-feed": (
        "app.bsky.feed.getAuthorFeed is the keyless endpoint that carries a "
        "verified insider's own posts AND the platform's verification state for "
        "the author. When this is unreachable the pipeline cannot see verified "
        "insider posts at all, so it is treated as a real outage (medium)."
    ),
    "reddit": (
        "Reddit's public JSON endpoints have contested availability in 2026 (some "
        "reports say ~60 req/min unauthenticated, others say broadly blocked with "
        "403 since May 2026), so an unreachable probe here is expected and is not "
        "treated as a pipeline failure."
    ),
}


def probe_platforms() -> Dict[str, Any]:
    """Reachability check for every social platform. Never raises."""

    results: Dict[str, Any] = {}
    for name, (url, source) in PLATFORM_PROBES.items():
        entry = probe(url, source=source)
        results[name] = {
            "url": url,
            "reachable": bool(entry.get("reachable")),
            "status": entry.get("status"),
            "latency_ms": entry.get("latency_ms"),
            "error": entry.get("error", ""),
        }
    return results


def _unreachable_flag(platform: str, info: Dict[str, Any]) -> Irregularity:
    return Irregularity(
        code=f"SOCIAL_UNREACHABLE_{platform.upper().replace('-', '_')}",
        severity=PLATFORM_FLAG_SEVERITY.get(platform, "medium"),
        title=f"{platform} was not reachable on this run",
        detail=(
            f"Probe of {info['url']} returned status={info['status']} "
            f"({info['error'] or 'no response'}). The adapter was SKIPPED rather "
            "than retried, so this run has no posts from that platform. "
            + PLATFORM_NOTES.get(platform, "Treated as a source outage to review, "
                                           "not a code failure.")
        ),
        evidence=[{"label": f"{platform} probe URL", "url": info["url"]}],
    )


def rotation_slice(items: List[str], *, now: float, size: int,
                   period_seconds: int) -> List[str]:
    """Deterministic rotating slice, so 32 clubs are covered without 32 queries."""

    if not items:
        return []
    if len(items) <= size:
        return list(items)
    idx = int(now // period_seconds) % max(1, -(-len(items) // size))
    start = idx * size
    return items[start:start + size]


def collect_social(
    *,
    player_names: Iterable[str] = (),
    watched_handles: Iterable[str] = (),
    candidate_handles: Iterable[str] = (),
    hot_teams: Iterable[str] = (),
    hot_players: Iterable[str] = (),
    team_names: Optional[Dict[str, str]] = None,
    enabled: Optional[Dict[str, bool]] = None,
    max_posts: int = 1500,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Run every enabled social adapter, skipping platforms whose probe failed.

    `watched_handles` are handles the project deliberately follows (national
    insiders from the verification directory plus any account the scorecard has
    promoted to "established"). They are read through getAuthorFeed, which is the
    only keyless Bluesky path that works, and each post carries the platform's own
    verification verdict for the author.
    """

    import time as _time

    enabled = enabled or {}
    now = now if now is not None else _time.time()
    probes = probe_platforms()
    posts: List[SocialPost] = []
    irregularities: List[Irregularity] = []
    fetches: List[Dict[str, Any]] = []
    fetched_at = utc_now_iso()

    watched = [h for h in dict.fromkeys(watched_handles) if h]
    candidates = [h for h in dict.fromkeys(candidate_handles) if h and h not in watched]
    hot_teams = [t for t in dict.fromkeys(hot_teams) if t]

    def record(label: str, res: Dict[str, Any], kind: str) -> None:
        fetched = res.get("posts") or []
        for p in fetched:
            p.source_kind = p.source_kind if p.source_kind != "unknown" else kind
        posts.extend(fetched)
        fetches.append({"label": label, "query": res.get("query", ""),
                        "url": res.get("url", ""), "posts": len(fetched),
                        "error": res.get("error", "")})

    # ---- Bluesky: verified insiders first (this is the fast path) ----------
    feed_info = probes.get("bluesky-author-feed", {})
    if enabled.get("bluesky-author-feed", True) is not False:
        if not feed_info.get("reachable"):
            irregularities.append(_unreachable_flag("bluesky-author-feed", feed_info))
        else:
            for handle in watched:
                try:
                    record(f"author:{handle}", fetch_bluesky_author(handle, limit=30,
                                                                    insider=True),
                           "verified-insider")
                except FetchError as exc:
                    irregularities.append(
                        Irregularity(
                            code="SOCIAL_FETCH_ERROR_BLUESKY_AUTHOR",
                            severity="medium",
                            title=f"Bluesky author feed failed for {handle}",
                            detail=f"{exc.reason} (status={exc.status}) while fetching "
                                   f"{exc.url}. Other watched handles were still tried.",
                            evidence=[{"label": "Failing URL", "url": exc.url}],
                        )
                    )
            for handle in candidates:
                try:
                    record(f"author:{handle}", fetch_bluesky_author(handle, limit=20,
                                                                    insider=True),
                           "insider-candidate")
                except FetchError:
                    pass  # candidates are best-effort; a failure changes nothing

    # ---- Bluesky search (documented 403; kept only if it starts working) ---
    search_info = probes.get("bluesky", {})
    if enabled.get("bluesky", True) is not False:
        if not search_info.get("reachable"):
            irregularities.append(_unreachable_flag("bluesky", search_info))
        else:
            names = list(player_names)[:25]
            for name in names:
                try:
                    record(f"search:{name} injury", fetch_bluesky(f"{name} injury", limit=20),
                           "social")
                except FetchError:
                    break

    # ---- Mastodon ----------------------------------------------------------
    mastodon_info = probes.get("mastodon", {})
    if enabled.get("mastodon", True) is not False:
        if not mastodon_info.get("reachable"):
            irregularities.append(_unreachable_flag("mastodon", mastodon_info))
        else:
            try:
                record("#nfl", fetch_mastodon_tag("nfl", limit=40), "social")
            except FetchError as exc:
                irregularities.append(_fetch_error_flag("mastodon", exc))

    # ---- Google News: league + hot clubs + hot players + rotation ----------
    gn_info = probes.get("google-news", {})
    if enabled.get("google-news", True) is not False:
        if not gn_info.get("reachable"):
            irregularities.append(_unreachable_flag("google-news", gn_info))
        else:
            queries: List[tuple] = [("NFL injury report", "league")]
            for code in hot_teams:
                name = (team_names or {}).get(code, code)
                queries.append((google_news_injury_query(name), f"hot-team:{code}"))
            for name in list(dict.fromkeys(hot_players))[:GOOGLE_NEWS_PLAYER_QUERY_CAP]:
                queries.append((google_news_injury_query(name), "hot-player"))
            rotate = rotation_slice(sorted(team_names or {}), now=now,
                                    size=GOOGLE_NEWS_ROTATION_SIZE,
                                    period_seconds=GOOGLE_NEWS_ROTATION_SECONDS)
            for code in rotate:
                if code in hot_teams:
                    continue
                name = (team_names or {}).get(code, code)
                queries.append((google_news_injury_query(name), f"rotation:{code}"))
            for query, label in queries:
                try:
                    record(f"news:{label}", fetch_google_news(query, limit=25,
                                                              source_kind="news"), "news")
                except FetchError as exc:
                    irregularities.append(_fetch_error_flag("google-news", exc))
                    break

    # ---- Reddit (contested availability; probed every run) -----------------
    reddit_info = probes.get("reddit", {})
    if enabled.get("reddit", True) is not False:
        if not reddit_info.get("reachable"):
            irregularities.append(_unreachable_flag("reddit", reddit_info))
        else:
            for sub in REDDIT_SUBS:
                try:
                    record(f"r/{sub}", fetch_reddit(sub, limit=25), "social")
                except FetchError as exc:
                    irregularities.append(_fetch_error_flag("reddit", exc))
                    break

    # Keep posts that are injury-relevant EITHER on the roster axis (signal >= .3)
    # OR on the in-game axis. The second half is what was missing on 2026-09-18:
    # "Keon Coleman injury update: Bills WR hurt vs. Lions" scores 0.15 on the
    # roster classifier (no status word, no body part) and was dropped, even
    # though it states an in-game injury.
    injury_posts = [
        p for p in posts
        if p.signal >= 0.3 or p.in_game_status != "NONE"
    ]
    # Sort by PARSED instant, not by the raw string. Google News returns RFC-822
    # pubDates ("Sun, 20 Sep 2026 ...") while Bluesky/ESPN return ISO
    # ("2026-09-21T...Z"); as strings every weekday name sorts after "2", so a
    # string sort put ALL of Google News ahead of every insider post and ESPN
    # article — and the max_posts cap below then dropped the newest, most
    # valuable items (verified 2026-09-21: social.json was capped at exactly
    # 400 rows). Undated items sort last: they cannot be placed in time.
    def _sort_instant(p: "SocialPost") -> float:
        if not p.posted_at:
            return float("-inf")
        t = _parse_instant(p.posted_at)
        return t if t is not None else float("-inf")

    injury_posts.sort(key=_sort_instant, reverse=True)

    # De-duplicate by URL: the same headline can arrive from several queries.
    seen: set = set()
    deduped: List[SocialPost] = []
    for p in injury_posts:
        key = (p.platform, p.post_id or p.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)

    return {
        "fetched_at": fetched_at,
        "platforms_probed": probes,
        "watched_handles": watched,
        "candidate_handles": candidates,
        "hot_teams": hot_teams,
        "fetches": fetches,
        "posts_raw": len(posts),
        "posts_injury_relevant": len(deduped),
        "posts": [p.to_dict() for p in deduped[:max_posts]],
        "irregularities": irregularities,
    }


def _fetch_error_flag(platform: str, exc: FetchError) -> Irregularity:
    return Irregularity(
        code=f"SOCIAL_FETCH_ERROR_{platform.upper().replace('-', '_')}",
        severity="medium",
        title=f"{platform} adapter failed after a successful probe",
        detail=f"{exc.reason} (status={exc.status}) while fetching {exc.url}.",
        evidence=[{"label": "Failing URL", "url": exc.url}],
    )
