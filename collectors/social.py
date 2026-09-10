"""Social / secondary verification layer.

Design rule: every adapter is PROBED before it is trusted, and its probe result is
written to data/latest/health.json. Nothing in this file asserts that a platform
is reachable -- the pipeline records what actually happened on each run.

Source ledger (verified 2026-09-10 unless noted)
------------------------------------------------
Bluesky      USABLE, FREE, NO AUTH.
             app.bsky.feed.searchPosts is documented as a public AppView endpoint
             callable against https://public.api.bsky.app without a token.
             Ref: https://docs.bsky.app/docs/category/http-reference
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
    raw: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.raw is None:
            self.raw = {}
        self.predicted_status = norm_status(self.predicted_status)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform, "post_id": self.post_id, "author": self.author,
            "author_name": self.author_name, "author_url": self.author_url,
            "text": self.text, "posted_at": self.posted_at, "url": self.url,
            "matched_player": self.matched_player, "player_key": self.player_key,
            "team": self.team, "predicted_status": self.predicted_status,
            "injury": self.injury, "signal": round(self.signal, 3),
            "engagement": self.engagement,
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


def fetch_bluesky(query: str, *, limit: int = 100, sort: str = "latest") -> Dict[str, Any]:
    """Search Bluesky without authentication (public AppView endpoint)."""

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
                raw={"likeCount": item.get("likeCount"), "repostCount": item.get("repostCount"),
                     "replyCount": item.get("replyCount"), "did": author.get("did")},
            )
        )
    return {"platform": "bluesky", "query": query, "url": url, "posts": posts,
            "cursor": payload.get("cursor"), "hits_total": payload.get("hitsTotal")}


def fetch_bluesky_author(handle: str, *, limit: int = 50) -> Dict[str, Any]:
    """Latest posts from one account -- used to follow a known beat writer."""

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
        posts.append(
            SocialPost(
                platform="bluesky", post_id=uri, author=h,
                author_name=author.get("displayName") or h,
                author_url=f"https://bsky.app/profile/{h}", text=text,
                posted_at=record.get("createdAt") or "", url=bsky_post_url(uri, h),
                predicted_status=cls["status"], injury=cls["injury"], signal=cls["signal"],
                engagement=int(post.get("likeCount") or 0),
                raw={"watched_handle": handle},
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


def fetch_google_news(query: str, *, limit: int = 40) -> Dict[str, Any]:
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
                raw={"outlet": outlet},
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

PLATFORM_PROBES = {
    "bluesky": ("https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=bsky.app", "bluesky"),
    "mastodon": (f"{MASTODON_DEFAULT_INSTANCE}/api/v1/instance", "mastodon"),
    "google-news": (f"{GOOGLE_NEWS_RSS}?q=nfl&hl=en-US&gl=US&ceid=US:en", "google-news"),
    "reddit": ("https://www.reddit.com/r/NFL_Discussion/about.json?raw_json=1", "reddit"),
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


def collect_social(
    *,
    player_names: Iterable[str] = (),
    watched_handles: Iterable[str] = (),
    enabled: Optional[Dict[str, bool]] = None,
    max_posts: int = 400,
) -> Dict[str, Any]:
    """Run every enabled social adapter, skipping platforms whose probe failed."""

    enabled = enabled or {}
    probes = probe_platforms()
    posts: List[SocialPost] = []
    irregularities: List[Irregularity] = []
    fetched_at = utc_now_iso()

    for platform, info in probes.items():
        if enabled.get(platform, True) is False:
            continue
        if not info["reachable"]:
            irregularities.append(
                Irregularity(
                    code=f"SOCIAL_UNREACHABLE_{platform.upper().replace('-', '_')}",
                    severity="medium" if platform != "reddit" else "low",
                    title=f"{platform} was not reachable on this run",
                    detail=(
                        f"Probe of {info['url']} returned status={info['status']} "
                        f"({info['error'] or 'no response'}). The adapter was SKIPPED rather "
                        "than retried, so this run has no posts from that platform. "
                        + (
                            "Reddit's public JSON endpoints have contested availability in "
                            "2026 (some reports say ~60 req/min unauthenticated, others say "
                            "broadly blocked with 403 since May 2026), so an unreachable probe "
                            "here is expected and is not treated as a pipeline failure."
                            if platform == "reddit"
                            else "Treated as a source outage to review, not a code failure."
                        )
                    ),
                    evidence=[{"label": f"{platform} probe URL", "url": info["url"]}],
                )
            )
            continue
        try:
            if platform == "bluesky":
                for handle in watched_handles:
                    res = fetch_bluesky_author(handle, limit=30)
                    posts.extend(res["posts"])
                for name in list(player_names)[:25]:
                    res = fetch_bluesky(f"{name} injury", limit=20)
                    posts.extend(res["posts"])
            elif platform == "mastodon":
                res = fetch_mastodon_tag("nfl", limit=40)
                posts.extend(res["posts"])
            elif platform == "google-news":
                res = fetch_google_news("NFL injury report", limit=40)
                posts.extend(res["posts"])
            elif platform == "reddit":
                for sub in REDDIT_SUBS:
                    res = fetch_reddit(sub, limit=25)
                    posts.extend(res["posts"])
        except FetchError as exc:
            irregularities.append(
                Irregularity(
                    code=f"SOCIAL_FETCH_ERROR_{platform.upper().replace('-', '_')}",
                    severity="medium",
                    title=f"{platform} adapter failed after a successful probe",
                    detail=f"{exc.reason} (status={exc.status}) while fetching {exc.url}.",
                    evidence=[{"label": "Failing URL", "url": exc.url}],
                )
            )

    # Keep only posts that actually look like injury reports.
    injury_posts = [p for p in posts if p.signal >= 0.3]
    injury_posts.sort(key=lambda p: (p.posted_at or ""), reverse=True)

    return {
        "fetched_at": fetched_at,
        "platforms_probed": probes,
        "posts_raw": len(posts),
        "posts_injury_relevant": len(injury_posts),
        "posts": [p.to_dict() for p in injury_posts[:max_posts]],
        "irregularities": irregularities,
    }
