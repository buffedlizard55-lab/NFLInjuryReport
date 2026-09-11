"""Verified reporter / source directory builder.

This module produces ``data/latest/directory.json`` -- the data behind the
``directory.html`` subpage, which answers the brief's request for "a list of
verified, official, and others for sports writers, sports reporters ... anyone
who reports live from the game itself and can verify the injury status of a
player".

NO-HALLUCINATION RULES (enforced in code, not just promised)
------------------------------------------------------------
1. OFFICIAL TIER (league + 32 clubs). Club website and every social handle are
   parsed out of the club's *own* nfl.com team page
   (https://www.nfl.com/teams/<slug>/) -- the league-published social directory.
   Nothing about a club is typed into this module except the nfl.com URL slug.
   Each row records the HTTP status and fetch timestamp of the page it was
   parsed from. Rows that could not be fetched are labelled "probe pending";
   handles are never guessed to fill a gap.

2. NATIONAL INSIDERS. Names live in ``collectors/directory_seed.json`` (a small
   explicit artifact a reviewer can diff). Each build verifies the social
   accounts rather than trusting the seed:
     * Bluesky is queried through the keyless public AppView
       (app.bsky.feed.searchActors / app.bsky.actor.getProfile), which returns
       the platform's own ``verification`` and moderation ``labels``. A profile
       is "verified" only on an exact name match AND
       verification.verifiedStatus == "valid". An exact name match WITHOUT a
       checkmark is a "candidate" for manual review. A lookalike carrying an
       "impersonation"/"parody" label is recorded as a non-official warning,
       never as the reporter. (Observed live 2026-09-10: the top Bluesky hits
       for Adam Schefter are mirrors and one carries Bluesky's own
       "impersonation" label -- so Schefter is correctly shown as having no
       genuine Bluesky account while Ian Rapoport's rapsheet.bsky.social is a
       Bluesky-verified exact match.)
     * X has no free read/verification API in 2026
       (publish.twitter.com/oembed returned HTTP 403 on 2026-09-10). The oEmbed
       endpoint and the syndication widget are probed on every CI build and,
       when they ever work, an exact ``author_name`` match upgrades the row.
       Until then X rows are explicitly one-click MANUAL-review links, never
       presented as machine-verified.

3. BEAT WRITERS. No social handles are asserted at all. Rows are aggregated
   from writers actually named in the live ESPN injury feed (the
   ``espn-attribution`` claims in data/latest/claims.json): name, outlet as
   ESPN printed it, club coverage, update count, and ESPN player-page evidence
   links. Manual-review X/Google-News *search* deep links are generated; a
   profile URL is never invented.

Usage:
    python3 -m collectors.directory build [--offline] [--force-refresh-teams]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .http import FetchError, fetch_json, fetch_text, get_probe_log, utc_now_iso
from .models import Irregularity
from .nfl_com import NFL_TEAMS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
LATEST_DIR = os.path.join(DATA_DIR, "latest")
STATE_DIR = os.path.join(DATA_DIR, "state")
SEED_PATH = os.path.join(REPO_ROOT, "collectors", "directory_seed.json")

OFFICIAL_INJURY_URL = "https://www.nfl.com/injuries/"
NFL_TEAM_PAGE = "https://www.nfl.com/teams/{slug}/"
NFL_MEDIA_URL = "https://media.nfl.com/"  # NFL Communications (league PR portal)
NFL_OPERATIONS_URL = "https://operations.nfl.com/gameday/injury-report/"

BSKY_PUBLIC = "https://public.api.bsky.app/xrpc"
# NOTE: the AppView renamed this from app.bsky.feed.searchActors; the old path
# returned {"error":"MethodNotImplemented"} when re-checked on 2026-09-10.
BSKY_SEARCH_ACTORS = BSKY_PUBLIC + "/app.bsky.actor.searchActors"
BSKY_GET_PROFILE = BSKY_PUBLIC + "/app.bsky.actor.getProfile"
X_OEMBED = "https://publish.twitter.com/oembed"
X_SYNDICATION = "https://cdn.syndication.twimg.com/widgets/syndication/api/widget"

#: Re-probe club pages at most this often; cached results are committed state.
TEAM_CACHE_TTL_SECONDS = 7 * 24 * 3600
#: Re-run social verification at most this often (X is unreachable free anyway,
#: but there is no point hammering the public AppView every 10 minutes).
SOCIAL_CACHE_TTL_SECONDS = 24 * 3600

# nfl.com URL slug for every club. The slug is only used to fetch the official
# team page; a wrong slug fails the fetch and is flagged rather than guessed.
TEAM_SLUGS: Dict[str, str] = {
    "ARI": "arizona-cardinals", "ATL": "atlanta-falcons", "BAL": "baltimore-ravens",
    "BUF": "buffalo-bills", "CAR": "carolina-panthers", "CHI": "chicago-bears",
    "CIN": "cincinnati-bengals", "CLE": "cleveland-browns", "DAL": "dallas-cowboys",
    "DEN": "denver-broncos", "DET": "detroit-lions", "GB": "green-bay-packers",
    "HOU": "houston-texans", "IND": "indianapolis-colts", "JAX": "jacksonville-jaguars",
    "KC": "kansas-city-chiefs", "LAC": "los-angeles-chargers",
    "LAR": "los-angeles-rams", "LV": "las-vegas-raiders", "MIA": "miami-dolphins",
    "MIN": "minnesota-vikings", "NE": "new-england-patriots",
    "NO": "new-orleans-saints", "NYG": "new-york-giants", "NYJ": "new-york-jets",
    "PHI": "philadelphia-eagles", "PIT": "pittsburgh-steelers",
    "SEA": "seattle-seahawks", "SF": "san-francisco-49ers",
    "TB": "tampa-bay-buccaneers", "TEN": "tennessee-titans",
    "WAS": "washington-commanders",
}

#: Handle/name fragments that mark a Bluesky actor as a non-original account.
NON_OFFICIAL_HANDLE_RE = re.compile(
    r"(mirror|repost|bot|fans?|fanpage|officialsite|daily|newsnow|wire|updates|"
    r"network|nation|country|brasil|\bbr\b|army|world|hub|feed|press|insider(?:s)?\d)",
    re.I,
)
IMPERSONATION_LABELS = {"impersonation", "parody", "compromised"}

#: Role/outlet signals expected in an NFL reporter's own bio. An UNVERIFIED
#: exact-name match whose bio contains none of these is treated as a same-name
#: collision (a different, real person), not a review candidate. Only ever
#: downgrades, never upgrades -- verified profiles need no keyword match.
ROLE_SIGNAL_RE = re.compile(
    r"(nfl|football|espn|nfl network|nfl network|reporter|journalist|insider|"
    r"sport(s)? (writer|reporter)|editor|analyst|correspondent|host|fox sports|"
    r"nbc|cbs|the ringer|the athletic|profootballtalk|football talk|media|"
    r"network|podcast|writer)",
    re.I,
)

#: Smells in the *text* of an account (display name + bio), as opposed to the
#: handle. Tight word list so a genuine reporter bio ("NFL Insider") never trips
#: it, but "Mirror of ...", "PARODY PAGE", "UNOFFICIAL BOT", "account can be
#: claimed by ..." do.
NON_OFFICIAL_TEXT_RE = re.compile(
    r"(mirror|parod\w*|\bbots?\b|unoff+i+cial|not\s+affiliated|unaffiliated|"
    r"fan\s+account|can be claimed|claim(?:ed)? by|repost(?:ing|er)?|nitter|satire)",
    re.I,
)


# ----------------------------------------------------------- small helpers --

def _now() -> str:
    return utc_now_iso()


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def _age_seconds(ts: str) -> Optional[float]:
    if not ts:
        return None
    try:
        return max(0.0, time.time() - time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def _name_key(name: str) -> str:
    """'A.J. Brown' / 'A. J. Brown' / 'AJ Brown' -> 'aj brown'."""

    s = (name or "").lower()
    s = re.sub(r"[.\-_'’]", "", s)          # initials: A.J. -> AJ
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _levenshtein(a: str, b: str) -> int:
    """Standard edit distance (short names only; kept dependency-free)."""

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def x_profile_url(handle: str) -> str:
    return f"https://x.com/{handle.lstrip('@')}" if handle else ""


def x_search_url(name: str) -> str:
    q = f'"{name}" (injury OR injured OR ruled out OR practice)'
    return "https://x.com/search?" + urlencode({"q": q, "f": "live"})


def google_news_url(name: str) -> str:
    q = f'"{name}" NFL injury'
    return ("https://news.google.com/rss/search?" +
            urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}))


def bsky_profile_url(handle: str) -> str:
    return f"https://bsky.app/profile/{handle}" if handle else ""


# ============================================================ official tier ==

class _TeamPageParser(HTMLParser):
    """Pull every outbound anchor off an nfl.com team page.

    Class- and layout-agnostic: we only need href + visible text, and the
    official social rail renders as links such as
    ``<a href="https://twitter.com/AZCardinals">@AZCardinals</a>`` (verified in
    the fetched pages 2026-09-10).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: List[Tuple[str, str]] = []
        self._href = ""
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        self._href = href
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            text = " ".join(" ".join(self._chunks).split())
            self.anchors.append((self._href, text))
            self._href, self._chunks = "", []


_SOCIAL_HOSTS = {
    "x": ("twitter.com", "x.com"),
    "facebook": ("facebook.com", "www.facebook.com", "m.facebook.com"),
    "instagram": ("instagram.com", "www.instagram.com"),
    "snapchat": ("snapchat.com", "www.snapchat.com"),
    "tiktok": ("tiktok.com", "www.tiktok.com"),
    "youtube": ("youtube.com", "www.youtube.com", "m.youtube.com"),
}
_BLOCKED_HOST_SUFFIXES = (
    "nfl.com", "nflshop.com", "nfl.net", "nflonlocation.com", "ticketmaster.com",
    "vividseats.com", "dazn.com", "nflauction.com", "nflsubscriptions.com",
)


def _host_matches(url: str, hosts: Tuple[str, ...]) -> bool:
    m = re.match(r"https?://([^/]+)/?", url or "")
    if not m:
        return False
    host = m.group(1).lower().lstrip("www.")
    return any(host == h.lower().lstrip("www.") or host.endswith("." + h.lower().lstrip("www."))
               for h in hosts)


def parse_team_directory_html(html: str) -> Dict[str, Any]:
    """Extract the official website + social handles from a team page."""

    parser = _TeamPageParser()
    parser.feed(html or "")
    socials: Dict[str, Dict[str, str]] = {}
    official_site = ""
    for href, text in parser.anchors:
        href = href.strip()
        if not href.startswith("http"):
            continue
        is_social = any(_host_matches(href, h) for h in _SOCIAL_HOSTS.values())
        is_blocked = any(suf in href for suf in _BLOCKED_HOST_SUFFIXES)
        if text.lower().strip() == "official website" and not is_social and not is_blocked:
            if not official_site:
                official_site = href
            continue
        for network, hosts in _SOCIAL_HOSTS.items():
            if _host_matches(href, hosts):
                path = re.sub(r"^https?://[^/]+/", "", href).strip("/")
                path = re.sub(r"^add/", "", path)  # snapchat.com/add/<name>
                handle = path.split("?")[0].split("/")[0] or text.lstrip("@")
                if network not in socials and handle:
                    socials[network] = {"handle": "@" + handle.lstrip("@"),
                                        "url": href, "as_published": text}
                break
    if not official_site:
        # Fallback: first non-social, non-nfl external anchor.
        for href, _text in parser.anchors:
            if (href.startswith("http")
                    and not any(_host_matches(href, h) for h in _SOCIAL_HOSTS.values())
                    and not any(h in href for h in _BLOCKED_HOST_SUFFIXES)):
                official_site = href
                break
    return {"official_site": official_site, "socials": socials,
            "n_anchors": len(parser.anchors)}


def fetch_team_directory(code: str) -> Dict[str, Any]:
    slug = TEAM_SLUGS[code]
    url = NFL_TEAM_PAGE.format(slug=slug)
    t0 = time.time()
    try:
        html = fetch_text(url, source="nfl.com")
    except FetchError as exc:
        return {"code": code, "name": NFL_TEAMS[code], "slug": slug, "nfl_team_url": url,
                "official_site": "", "socials": {},
                "probe": {"status": exc.status, "latency_ms": int((time.time() - t0) * 1000),
                          "error": exc.reason, "fetched_at": _now()}}
    parsed = parse_team_directory_html(html)
    return {
        "code": code, "name": NFL_TEAMS[code], "slug": slug, "nfl_team_url": url,
        "official_site": parsed["official_site"],
        "socials": parsed["socials"],
        "probe": {"status": 200, "latency_ms": int((time.time() - t0) * 1000),
                  "error": "", "fetched_at": _now(), "n_anchors": parsed["n_anchors"]},
    }


def build_league_rows() -> List[Dict[str, Any]]:
    return [
        {"name": "NFL — Official Injury Report", "role": "League Game Status Reports (binding)",
         "url": OFFICIAL_INJURY_URL, "evidence_url": OFFICIAL_INJURY_URL,
         "kind": "official-report"},
        {"name": "NFL Communications", "role": "League PR / media portal (media.nfl.com)",
         "url": NFL_MEDIA_URL, "evidence_url": NFL_MEDIA_URL, "kind": "league-office"},
        {"name": "NFL Football Operations — Injury Report policy",
         "role": "Personnel (Injury) Report Policy (page returned 404 to this project's "
                 "fetcher on 2026-09-10: probe-only, not cited as evidence elsewhere)",
         "url": NFL_OPERATIONS_URL, "evidence_url": NFL_OPERATIONS_URL,
         "kind": "league-office", "probe_status_hint": "returned-404-on-2026-09-10"},
    ]


def build_team_rows(*, offline: bool, force_refresh: bool,
                    cache: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Irregularity]]:
    irregularities: List[Irregularity] = []
    teams_state = cache.setdefault("teams", {})
    for code in NFL_TEAMS:
        prior = teams_state.get(code) or {}
        age = _age_seconds(prior.get("probe", {}).get("fetched_at", ""))
        fresh = age is not None and age < TEAM_CACHE_TTL_SECONDS and prior.get("socials")
        if offline or (fresh and not force_refresh):
            continue
        live = fetch_team_directory(code)
        if live["probe"].get("status") != 200 and prior.get("socials"):
            # Transient fetch failure: keep the last good parsed row so one
            # network blip cannot blank a club directory. The TTL is not
            # refreshed, so the next run retries.
            row = prior
            irregularities.append(Irregularity(
                code="DIRECTORY_TEAM_PAGE_FAILED", severity="low",
                title=f"nfl.com team page for {code} unreachable; kept last parsed row",
                detail=f"{live['nfl_team_url']} -> {live['probe'].get('error')}. The cached "
                       f"row parsed {sorted(prior.get('socials', {}))} at "
                       f"{prior.get('probe', {}).get('fetched_at')} is retained; no guessing.",
                evidence=[{"label": "nfl.com team page", "url": live["nfl_team_url"]}]))
        else:
            row = live
            teams_state[code] = row
        if row.get("probe", {}).get("status") != 200:
            irregularities.append(Irregularity(
                code="DIRECTORY_TEAM_PAGE_FAILED", severity="medium",
                title=f"nfl.com team page for {code} could not be fetched",
                detail=f"{row['nfl_team_url']} -> {row['probe'].get('error')}. The blank "
                       "probe-pending row is kept; no handles are guessed.",
                evidence=[{"label": "nfl.com team page", "url": row["nfl_team_url"]}]))
        elif live["probe"].get("status") == 200 and (
                not row["socials"].get("x") or not row["official_site"]):
            irregularities.append(Irregularity(
                code="DIRECTORY_TEAM_INCOMPLETE", severity="low",
                title=f"{code} nfl.com page yielded no X link or official site",
                detail=f"Parsed {row['socials'] and list(row['socials'])} from "
                       f"{row['nfl_team_url']}; markup may have changed.",
                evidence=[{"label": "nfl.com team page", "url": row["nfl_team_url"]}]))

    rows: List[Dict[str, Any]] = []
    for code in NFL_TEAMS:
        row = teams_state.get(code)
        if not row:
            rows.append({"code": code, "name": NFL_TEAMS[code],
                         "slug": TEAM_SLUGS[code],
                         "nfl_team_url": NFL_TEAM_PAGE.format(slug=TEAM_SLUGS[code]),
                         "official_site": "", "socials": {},
                         "probe": {"status": None, "error": "not probed yet (offline build)",
                                   "fetched_at": ""}})
        else:
            rows.append(row)
    return rows, irregularities


# ============================================================ social verify ==

def _bsky_verification(actor: Dict[str, Any]) -> Dict[str, Any]:
    ver = actor.get("verification") or {}
    verifications = ver.get("verifications") or []
    issuers = [
        {"issuer": v.get("issuerDisplayName", ""), "handle": v.get("issuerHandle", ""),
         "valid": bool(v.get("isValid")), "created_at": v.get("createdAt", "")}
        for v in verifications
    ]
    return {"verified_status": ver.get("verifiedStatus", ""),
            "trusted_status": ver.get("trustedVerifierStatus", ""), "issuers": issuers}


def _actor_labels(actor: Dict[str, Any]) -> List[str]:
    out = []
    for lab in actor.get("labels") or []:
        val = (lab.get("val") or "").lower()
        if val:
            out.append(val)
    return out


def classify_bsky_actor(actor: Dict[str, Any], wanted_name: str) -> Tuple[str, str]:
    """Return (status, reason). status in verified|candidate|non-official|weak."""

    display = actor.get("displayName") or ""
    handle = actor.get("handle") or ""
    bio = actor.get("description") or ""
    wanted = _name_key(wanted_name)
    display_n = _name_key(display)
    name_matches = bool(wanted) and (display_n == wanted or wanted in display_n)
    labels = _actor_labels(actor)
    bad_label = next((l for l in labels if l in IMPERSONATION_LABELS), "")
    if bad_label:
        return "non-official", f"carries Bluesky moderation label '{bad_label}'"
    bot_label = "bot" if "bot" in labels else ""
    handle_smell = NON_OFFICIAL_HANDLE_RE.search(handle.split(".")[0] or handle)
    text_smell = NON_OFFICIAL_TEXT_RE.search(f"{display} {bio}")
    if name_matches and not bot_label and not handle_smell and not text_smell:
        ver = _bsky_verification(actor)
        if ver["verified_status"] == "valid":
            issuer = ver["issuers"][0]["issuer"] if ver["issuers"] else "a verifier"
            return "verified", f"exact name match; Bluesky verification valid (issuer: {issuer})"
        if not ROLE_SIGNAL_RE.search(bio or ""):
            snippet = re.sub(r"\s+", " ", (bio or "")).strip()[:80]
            return "non-official", ("same-name account with no reporter/outlet signal in bio "
                                    f"(name collision; bio: {snippet or '(empty)'})")
        return "candidate", "exact name match but no Bluesky verification badge; manual review"
    if name_matches and (bot_label or handle_smell or text_smell):
        why = (bot_label or
               (f"bio/name says {text_smell.group(0)!r}" if text_smell else "") or
               f"handle '{handle}' looks like a non-original account")
        return "non-official", why
    if display_n and wanted and (wanted.split()[-1] in display_n or display_n in wanted):
        return "weak", f"name only partially matches ('{display}')"
    return "weak", f"display name '{display}' does not match"


def verify_bluesky(name: str, expected_handle: str = "") -> Dict[str, Any]:
    """Resolve a person's Bluesky account via the keyless public AppView."""

    rejected: List[Dict[str, str]] = []
    try:
        if expected_handle:
            url = f"{BSKY_GET_PROFILE}?{urlencode({'actor': expected_handle})}"
            payload = fetch_json(url, source="bluesky")
            actors = [payload] if isinstance(payload, dict) and payload.get("handle") else []
            query_url = url
        else:
            query_url = f"{BSKY_SEARCH_ACTORS}?{urlencode({'q': name, 'limit': 15})}"
            payload = fetch_json(query_url, source="bluesky")
            actors = payload.get("actors") if isinstance(payload, dict) else []
    except FetchError as exc:
        return {"platform": "bluesky", "status": "probe-error", "handle": "",
                "url": bsky_profile_url(expected_handle), "detail": exc.reason,
                "http_status": exc.status, "rejected": rejected}

    best: Optional[Dict[str, Any]] = None
    best_status = ""
    for actor in actors or []:
        status, reason = classify_bsky_actor(actor, name)
        if status in ("weak",):
            continue
        rejected_entry = {"handle": actor.get("handle", ""),
                          "display_name": actor.get("displayName", ""), "status": status,
                          "reason": reason}
        if status in ("verified", "candidate") and best is None:
            best = actor
            best_status = status
            # Keep the first strong match as the candidate; others are rejected
            # so a reviewer can see why.
            continue
        rejected.append(rejected_entry)

    if best is None:
        # Surface the closest impersonation-labeled lookalike explicitly.
        labeled = [r for r in rejected if "impersonation" in r["reason"] or "parody" in r["reason"]]
        return {"platform": "bluesky", "status": "not-found", "handle": "", "url": "",
                "detail": ("no genuine account: every exact-name result was a mirror, bot, "
                           "self-declared parody, or a different person with the same name"
                           + ("; one name-matching lookalike also carries Bluesky's own "
                              "'impersonation' moderation label" if labeled else "")),
                "rejected": rejected[:8]}
    ver = _bsky_verification(best)
    status, reason = classify_bsky_actor(best, name)
    return {"platform": "bluesky", "status": status,
            "handle": best.get("handle", ""), "did": best.get("did", ""),
            "display_name": best.get("displayName", ""),
            "url": bsky_profile_url(best.get("handle", "")),
            "detail": reason, "verification": ver,
            "labels": _actor_labels(best),
            "bio_snippet": re.sub(r"\s+", " ", best.get("description", "") or "")[:240],
            "rejected": rejected[:8], "queried_url": query_url}


def verify_x(handle: str, expected_name: str = "") -> Dict[str, Any]:
    """Probe keyless X endpoints. Never raises; honest about what does not work.

    A profile is only marked verified when oEmbed returns an ``author_name``
    that matches the expected reporter name. A non-empty but mismatching name
    degrades to manual review rather than asserting the wrong account.
    """

    if not handle:
        return {"platform": "x", "status": "none", "handle": "", "url": "",
                "detail": "no handle listed", "http_status": None}
    profile = x_profile_url(handle)
    attempts: List[Dict[str, Any]] = []

    # 1) publish.twitter.com oEmbed: on a working day it returns JSON whose
    #    author_name must match. Observed HTTP 403 from some networks on
    #    2026-09-10, but HTTP 200 from a GitHub Actions runner the same day.
    url = f"{X_OEMBED}?{urlencode({'url': profile})}"
    try:
        payload = fetch_json(url, source="x", retries=0)
        author = (payload or {}).get("author_name", "")
        attempts.append({"endpoint": "oembed", "ok": True, "author_name": author})
        if author and expected_name and _name_key(author) != _name_key(expected_name):
            return {"platform": "x", "status": "manual", "handle": handle, "url": profile,
                    "detail": f"oEmbed returned author_name='{author}', which does not match "
                              f"'{expected_name}'; click to review — do not treat as them.",
                    "http_status": 200, "attempts": attempts}
        status = "verified" if author else "manual"
        return {"platform": "x", "status": status, "handle": handle, "url": profile,
                "detail": (f"oEmbed author_name='{author}'" +
                           (f" matches '{expected_name}'" if author else "; empty author_name")),
                "http_status": 200, "attempts": attempts}
    except FetchError as exc:
        attempts.append({"endpoint": "oembed", "ok": False, "status": exc.status,
                         "error": exc.reason})

    # 2) Syndication widget: returns HTML on success; a screen-name inside it is
    # weak confirmation.
    url2 = f"{X_SYNDICATION}?{urlencode({'url': profile})}"
    try:
        body = fetch_text(url2, source="x", retries=0)
        attempts.append({"endpoint": "syndication", "ok": True, "bytes": len(body)})
        found = re.search(r'"screen_name"\s*:\s*"([^"]+)"', body or "")
        if found and found.group(1).lower() == handle.lower().lstrip("@"):
            return {"platform": "x", "status": "verified", "handle": handle, "url": profile,
                    "detail": "syndication widget returned matching screen_name",
                    "http_status": 200, "attempts": attempts}
        return {"platform": "x", "status": "manual", "handle": handle, "url": profile,
                "detail": "syndication responded but no matching screen_name; click to verify",
                "http_status": 200, "attempts": attempts}
    except FetchError as exc:
        attempts.append({"endpoint": "syndication", "ok": False, "status": exc.status,
                         "error": exc.reason})

    return {"platform": "x", "status": "manual", "handle": handle, "url": profile,
            "detail": "X offers no free verification endpoint (oEmbed/syndication both failed "
                      "this run); profile link is for one-click manual verification",
            "http_status": None, "attempts": attempts}


# ============================================================== beat writers ==

def beat_from_claims(claims_payload: Dict[str, Any],
                     exclude_keys: Optional[set] = None) -> List[Dict[str, Any]]:
    """Aggregate the beat writers ESPN itself names on injury updates.

    Returns only people with >= 2 attributed updates. National seed names are
    excluded (they appear on the National tab). No social handles are attached;
    links are profile-agnostic searches so a reviewer can verify by hand.
    """

    exclude_keys = exclude_keys or set()
    buckets: Dict[str, Dict[str, Any]] = {}
    for claim in claims_payload.get("claims", []) or []:
        if claim.get("platform") != "espn-attribution":
            continue
        name = (claim.get("author") or "").strip()
        if not name:
            continue
        key = _name_key(name)
        if key in exclude_keys:
            continue
        b = buckets.setdefault(key, {"name": name, "names": {}, "outlets": {},
                                     "teams": {}, "updates": 0, "latest_at": "",
                                     "evidence": []})
        b["names"][name] = b["names"].get(name, 0) + 1
        outlet = (claim.get("tier") or "").strip()
        if outlet:
            b["outlets"][outlet] = b["outlets"].get(outlet, 0) + 1
        team = claim.get("team") or ""
        if team:
            b["teams"][team] = b["teams"].get(team, 0) + 1
        b["updates"] += 1
        ts = claim.get("posted_at") or ""
        if ts > b["latest_at"]:
            b["latest_at"] = ts
        url = claim.get("url") or ""
        if url and url not in b["evidence"] and len(b["evidence"]) < 4:
            b["evidence"].append(url)

    rows = []
    for key, b in buckets.items():
        if b["updates"] < 2:
            continue
        canonical_name = max(b["names"].items(), key=lambda kv: kv[1])[0]
        outlet = max(b["outlets"].items(), key=lambda kv: kv[1])[0] if b["outlets"] else ""
        teams = sorted(b["teams"], key=lambda t: -b["teams"][t])
        rows.append({
            "key": key, "name": canonical_name, "outlet": outlet, "teams": teams,
            "updates": b["updates"], "latest_at": b["latest_at"], "evidence": b["evidence"],
            "x_search_url": x_search_url(canonical_name),
            "google_news_url": google_news_url(canonical_name),
            "platforms": {"x": {"status": "search-only",
                                "url": x_search_url(canonical_name),
                                "detail": "No handle asserted: this searches X by the exact "
                                          "name ESPN printed; confirm the profile yourself."},
                          "bluesky": {"status": "not-resolved", "url": "",
                                      "detail": "Auto-resolution runs only for national seed "
                                                "accounts to keep verification rate-limited."}},
        })
    rows.sort(key=lambda r: (-len(r["teams"]), -r["updates"], r["name"]))
    return rows


def annotate_duplicate_variants(beat: List[Dict[str, Any]]) -> List[Irregularity]:
    """Flag near-identical names (e.g. an ESPN attribution typo) for review."""

    flags: List[Irregularity] = []
    for row in beat:
        if len(row["teams"]) > 1:
            flags.append(Irregularity(
                code="DIRECTORY_CROSS_TEAM_BEAT", severity="low",
                title=f"Beat writer {row['name']} is attributed across {len(row['teams'])} clubs "
                      f"({', '.join(row['teams'])})",
                detail=f"ESPN printed {row['name']} ({row['outlet'] or 'outlet as printed'}) as "
                       "the attribution on updates for multiple clubs. Local beat writers "
                       "normally cover one club, so this is usually a shared-wire/aggregator "
                       "attribution; it is kept as observed and flagged for manual review rather "
                       "than reassigned by guesswork.",
                evidence=[{"label": f"ESPN attribution #{i + 1}", "url": u}
                          for i, u in enumerate(row["evidence"][:3])]))
    for i, a in enumerate(beat):
        for b in beat[i + 1:]:
            ka, kb = a["key"].replace(" ", ""), b["key"].replace(" ", "")
            if ka == kb:
                continue
            if abs(len(ka) - len(kb)) <= 2 and _levenshtein(ka, kb) <= 2:
                flags.append(Irregularity(
                    code="DIRECTORY_NAME_VARIANT", severity="low",
                    title=f"Possible duplicate reporter names: {a['name']!r} vs {b['name']!r}",
                    detail="ESPN's attribution text printed two near-identical names. They are "
                           "kept as separate rows (not merged by guesswork); review whether "
                           "they are one person.",
                    evidence=[{"label": a["name"], "url": a["evidence"][0] if a["evidence"] else ""},
                              {"label": b["name"], "url": b["evidence"][0] if b["evidence"] else ""}]))
    return flags


# ============================================================== national tier ==

def load_seed() -> List[Dict[str, Any]]:
    payload = _read_json(SEED_PATH, {})
    return payload.get("national", []) if isinstance(payload, dict) else []


def observed_from_claims(claims_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    obs: Dict[str, Dict[str, Any]] = {}
    for claim in claims_payload.get("claims", []) or []:
        if claim.get("platform") != "espn-attribution":
            continue
        key = _name_key(claim.get("author", ""))
        if not key:
            continue
        row = obs.setdefault(key, {"updates": 0, "teams": {}, "evidence": [], "latest_at": ""})
        row["updates"] += 1
        if claim.get("team"):
            row["teams"][claim["team"]] = row["teams"].get(claim["team"], 0) + 1
        if claim.get("url") and claim["url"] not in row["evidence"] and len(row["evidence"]) < 4:
            row["evidence"].append(claim["url"])
        if (claim.get("posted_at") or "") > row["latest_at"]:
            row["latest_at"] = claim["posted_at"] or ""
    return obs


def build_national_rows(*, offline: bool, verification_cache: Dict[str, Any],
                        observed: Dict[str, Dict[str, Any]],
                        ) -> Tuple[List[Dict[str, Any]], List[Irregularity]]:
    irregularities: List[Irregularity] = []
    cache = verification_cache.setdefault("national", {})
    rows: List[Dict[str, Any]] = []
    for seed in load_seed():
        name = seed["name"]
        key = _name_key(name)
        entry: Dict[str, Any] = {
            "name": name, "role": seed.get("role", ""),
            "outlet_evidence": seed.get("outlet_evidence", ""),
            "notes": seed.get("notes", ""),
            "x": {"platform": "x", "status": "manual", "handle": seed.get("x_handle", ""),
                  "url": x_profile_url(seed.get("x_handle", "")),
                  "detail": "Manual-review link: X has no free verification API."},
            "bluesky": {"platform": "bluesky", "status": "not-probed", "handle": "",
                        "url": "", "detail": "", "rejected": []},
        }
        obs = observed.get(key)
        entry["observed_in_feed"] = bool(obs)
        if obs:
            entry["feed_updates"] = obs["updates"]
            entry["feed_teams"] = sorted(obs["teams"], key=lambda t: -obs["teams"][t])
            entry["evidence"] = obs["evidence"]
            entry["latest_feed_at"] = obs["latest_at"]
        else:
            entry["feed_updates"] = 0
            entry["feed_teams"] = []
            entry["evidence"] = []

        cached = cache.get(key) or {}
        age = _age_seconds(cached.get("verified_at", ""))
        fresh = age is not None and age < SOCIAL_CACHE_TTL_SECONDS
        # A previous transient failure must not be cached for the TTL: retry on
        # the very next build rather than showing "probe error" for 24 hours.
        prior_status = cached.get("bluesky", {}).get("status", "")
        if prior_status == "probe-error":
            fresh = False
        if not offline and (not fresh or "bluesky" not in cached):
            try:
                bsky = verify_bluesky(name, seed.get("bsky_expected_handle", ""))
            except Exception as exc:  # noqa: BLE001 - never let one profile break the build
                bsky = {"platform": "bluesky", "status": "probe-error", "handle": "",
                        "url": bsky_profile_url(seed.get("bsky_expected_handle", "")),
                        "detail": f"{type(exc).__name__}: {exc}", "rejected": []}
            xrow = verify_x(seed.get("x_handle", ""), name)
            prior_good = cached.get("bluesky", {}).get("status") in (
                "verified", "candidate", "not-found")
            if bsky.get("status") == "probe-error" and prior_good:
                # Transient AppView failure with a usable prior result: keep
                # it and leave verified_at untouched so the next run retries.
                pass
            else:
                cached = {"verified_at": _now(), "bluesky": bsky, "x": xrow}
                cache[key] = cached
            # Polite pacing between public-AppView / X requests: the public
            # endpoints are shared infrastructure and a rapid-fire burst from
            # one datacenter IP invites throttling.
            time.sleep(0.8)
        # Re-read cached AFTER probing, so first-time results reach the entry
        # (previously this read the pre-probe (empty) dict and new results were
        # written to state but never rendered).
        cached = cache.get(key) or cached
        if "bluesky" in cached:
            entry["bluesky"] = cached["bluesky"]
        if "x" in cached:
            entry["x"] = cached["x"]

        for rej in entry["bluesky"].get("rejected", []) or []:
            if rej.get("status") == "non-official" and (
                    "impersonation" in rej.get("reason", "") or "parody" in rej.get("reason", "")):
                irregularities.append(Irregularity(
                    code="DIRECTORY_LOOKALIKE_ACCOUNT", severity="low",
                    title=f"Bluesky lookalike for {name}: {rej['handle']}",
                    detail=f"The account is marked {rej['reason']}; it is listed nowhere as an "
                           "official source and is shown only as a fraud warning.",
                    evidence=[{"label": rej["handle"],
                               "url": bsky_profile_url(rej["handle"])}]))
        rows.append(entry)
    rows.sort(key=lambda r: (not r["observed_in_feed"], -(r["feed_updates"] or 0), r["name"]))
    return rows, irregularities


# =================================================================== build ==

def build(*, offline: bool = False, force_refresh_teams: bool = False,
          claims_path: Optional[str] = None,
          claims_payload: Optional[Dict[str, Any]] = None,
          latest_dir: Optional[str] = None,
          state_dir: Optional[str] = None) -> Dict[str, Any]:
    irregularities: List[Irregularity] = []
    out_latest = latest_dir or LATEST_DIR
    out_state = state_dir or STATE_DIR

    team_cache = _read_json(os.path.join(out_state, "team_directory.json"),
                            {"fetched_at": "", "teams": {}})
    verification_cache = _read_json(os.path.join(out_state, "directory_verification.json"),
                                    {"verified_at": "", "national": {}})

    league = build_league_rows()
    teams, team_flags = build_team_rows(
        offline=offline, force_refresh=force_refresh_teams, cache=team_cache)
    irregularities.extend(team_flags)

    if claims_payload is None:
        claims_path = claims_path or os.path.join(out_latest, "claims.json")
        claims_payload = _read_json(claims_path, {}) or {}
    observed = observed_from_claims(claims_payload)
    national, national_flags = build_national_rows(
        offline=offline, verification_cache=verification_cache, observed=observed)
    irregularities.extend(national_flags)

    seed_keys = {_name_key(r["name"]) for r in load_seed()}
    beat = beat_from_claims(claims_payload, exclude_keys=seed_keys)
    irregularities.extend(annotate_duplicate_variants(beat))

    # Caches are state even on offline builds (they hold previously-live data).
    # Offline builds must not move the fetch timestamps forward, or the TTL
    # freshness check would skip a real re-probe the data is due for.
    stamp = _now() if not offline else ""
    _write_json(os.path.join(out_state, "team_directory.json"),
                {"fetched_at": team_cache.get("fetched_at", "") or stamp,
                 "provenance": team_cache.get("provenance", ""),
                 "teams": team_cache.get("teams", {})})
    _write_json(os.path.join(out_state, "directory_verification.json"),
                {"fetched_at": verification_cache.get("fetched_at", "") or stamp,
                 "provenance": verification_cache.get("provenance", ""),
                 "national": verification_cache.get("national", {})})

    n_verified = sum(1 for r in national if r["bluesky"]["status"] == "verified")
    n_candidate = sum(1 for r in national if r["bluesky"]["status"] in ("candidate",))
    payload = {
        "generated_at": _now(),
        "mode": "offline" if offline else "live",
        "provenance": {
            "teams": team_cache.get("provenance", ""),
            "social": verification_cache.get("provenance", ""),
        },
        "methodology": {
            "official": "Club websites and social handles are parsed from each club's own "
                        "nfl.com team page (the league-published social directory) and carry "
                        "the fetch status/timestamp as evidence.",
            "national_bluesky": "Keyless public AppView: 'verified' requires an exact display "
                                "name match AND verification.verifiedStatus=='valid'; exact "
                                "matches without a checkmark are manual-review 'candidates'; "
                                "accounts with impersonation/parody labels or mirror/bot handles "
                                "are recorded as non-official warnings.",
            "national_x": "X has no free verification API in 2026 (oEmbed returned HTTP 403 on "
                          "2026-09-10). X entries are one-click manual-verification links until "
                          "an automated probe succeeds; they are never claimed machine-verified.",
            "beat": "Names/outlets/clubs come verbatim from beat-writer attributions inside the "
                    "live ESPN injury feed. No social handles are asserted for beat writers; "
                    "only exact-name search deep links are provided for manual review.",
            "evidence": "Every row links to the page that produced it. The raw probe log is "
                        "in data/latest/meta.json and the probe ledger on the Sources tab.",
        },
        "counts": {"teams": len(teams), "national": len(national), "beat": len(beat),
                   "bluesky_verified": n_verified, "bluesky_candidates": n_candidate,
                   "irregularities": 0},
        "league": league,
        "teams": teams,
        "national": national,
        "beat": beat,
        "irregularities": [i.to_dict() for i in irregularities],
        "probes": get_probe_log(),
    }
    payload["counts"]["irregularities"] = len(payload["irregularities"])
    _write_json(os.path.join(out_latest, "directory.json"), payload)
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="collectors.directory", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", help="build data/latest/directory.json")
    p_build.add_argument("--offline", action="store_true",
                         help="use cached/observed data only, perform no network probes")
    p_build.add_argument("--force-refresh-teams", action="store_true",
                         help="re-fetch every nfl.com team page ignoring the 7-day cache")
    args = parser.parse_args(argv)
    if args.cmd == "build":
        payload = build(offline=args.offline,
                        force_refresh_teams=args.force_refresh_teams)
        c = payload["counts"]
        print(f"directory: {c['teams']} clubs, {c['national']} national "
              f"({c['bluesky_verified']} Bluesky-verified, {c['bluesky_candidates']} candidates), "
              f"{c['beat']} beat writers, {c['irregularities']} flags "
              f"[mode={payload['mode']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
