"""Collector for ESPN's public NFL injuries JSON endpoint.

Verified live on 2026-09-10 (HTTP 200):
  https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries
Response shape observed (abridged, field names verbatim):

  {
    "timestamp": "2026-09-10T22:03:05Z",
    "status": "success",
    "season": {"year": 2026, "type": 2, "name": "Regular Season", "displayName": "2026"},
    "injuries": [
      {
        "id": "22",
        "displayName": "Arizona Cardinals",
        "injuries": [
          {
            "id": "636710",
            "longComment": "Love is set to practice ...",
            "shortComment": "Love (ankle) is warming up ahead of Thursday's practice,
                              Dani Sureck of the Cardinals' official site reports.",
            "status": "Questionable",
            "date": "2026-09-10T20:48Z",
            "athlete": {
              "firstName": "Jeremiyah", "lastName": "Love",
              "displayName": "Jeremiyah Love", "shortName": "J. Love",
              "links": [... {"rel": ["playercard","desktop","athlete"],
                             "href": "https://www.espn.com/nfl/player/_/id/4870808/jeremiyah-love"} ...],
              "headshot": {"href": "https://a.espncdn.com/i/headshots/nfl/players/full/4870808.png"},
              "position": {"id": "9", "abbreviation": "RB", ...},
              "team": {"id": "22", "abbreviation": "ARI", "displayName": "Arizona Cardinals", ...}
            }
          }
        ]
      }
    ]
  }

IMPORTANT -- verified NOT to work (2026-09-10):
  https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/22/injuries
  -> returns the empty object {} (HTTP 200). Only the league-wide endpoint above
     returns data, so this collector never calls the per-team variant.

Why ESPN is used at all: nfl.com is authoritative for the *official* designation
but publishes only at the report times and carries no per-update timestamp and no
reporter attribution. ESPN's payload carries a per-injury `date` (down to the
minute) and names the beat writer inside `shortComment`. That combination is what
makes low-latency alerting and reporter scoring possible without any API key.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .http import FetchError, fetch_json, utc_now_iso
from .models import Irregularity, PlayerInjury, norm_status
from .nfl_com import NFL_TEAMS

SOURCE_NAME = "espn"
ENDPOINT = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
TEAMS_ENDPOINT = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
_BROKEN_TEAM_ENDPOINT = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/injuries"
)

_TEAM_BY_NAME = {v: k for k, v in NFL_TEAMS.items()}
_TEAM_BY_CITY = {v.rsplit(" ", 1)[0]: k for k, v in NFL_TEAMS.items()}
_TEAM_BY_NICK = {v.rsplit(" ", 1)[-1]: k for k, v in NFL_TEAMS.items()}

#: "Dani Sureck of the Cardinals' official site reports."  ->  ("Dani Sureck", "Cardinals' official site")
_ATTRIB_OF_RE = re.compile(
    r"(?:according to\s+)?([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3})\s+of\s+"
    r"([^,]{2,60}?)\s+(?:reports?|writes?|tweets?|posts?)",
)
#: Outlets are commonly written with an article ("of the Cardinals' official site");
#: the article is not part of the name.
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
#: "per Adam Schefter" / "via Ian Rapoport"
_ATTRIB_PER_RE = re.compile(
    r"(?:per|via)\s+([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3})",
)
#: "@handle" mentions
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,20})")
#: "Love (ankle) ..." -> "ankle"
_BODY_PART_RE = re.compile(r"^[A-Z][\w.'\-]*(?:\s+[A-Z][\w.'\-]+)?\s*\(([^)]{2,60})\)")

_STOPWORDS = {"the", "a", "an", "his", "her", "their", "team", "source", "sources"}


def extract_attribution(text: str) -> Tuple[str, str, str]:
    """Pull (reporter_name, outlet, handle) out of an ESPN comment string.

    Returns empty strings when no attribution is present -- never guesses.
    """

    if not text:
        return "", "", ""
    m = _ATTRIB_OF_RE.search(text)
    if m:
        name = m.group(1).strip(" ,.")
        outlet = _LEADING_ARTICLE_RE.sub("", m.group(2).strip(" ,.'"))
        if name.lower() not in _STOPWORDS and len(name.split()) <= 4:
            handle = ""
            hm = _HANDLE_RE.search(text)
            if hm:
                handle = hm.group(1)
            return name, outlet, handle
    m = _ATTRIB_PER_RE.search(text)
    if m:
        name = m.group(1).strip(" ,.")
        if name.lower() not in _STOPWORDS:
            return name, "", ""
    return "", "", ""


def extract_body_part(short_comment: str) -> str:
    m = _BODY_PART_RE.match(short_comment or "")
    return m.group(1).strip() if m else ""


def _team_code(team_obj: Optional[Dict[str, Any]], fallback_name: str = "") -> str:
    if team_obj:
        abbr = (team_obj.get("abbreviation") or "").upper()
        if abbr in NFL_TEAMS:
            return abbr
        name = team_obj.get("displayName") or ""
        if name in _TEAM_BY_NAME:
            return _TEAM_BY_NAME[name]
    name = (fallback_name or "").strip()
    if name in _TEAM_BY_NAME:
        return _TEAM_BY_NAME[name]
    if name in NFL_TEAMS:
        return name
    if name in _TEAM_BY_NICK:
        return _TEAM_BY_NICK[name]
    if name in _TEAM_BY_CITY:
        return _TEAM_BY_CITY[name]
    return ""


def _pick_link(links: List[Dict[str, Any]], *rels: str) -> str:
    for link in links or []:
        rel = link.get("rel") or []
        if any(r in rel for r in rels):
            href = link.get("href") or ""
            if href.startswith("http"):
                return href
    return ""


def _normalise_date(value: str) -> str:
    """ESPN emits RFC3339 with minute precision and no seconds: 2026-09-10T20:48Z."""

    if not value:
        return ""
    v = value.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(Z|[+-]\d{2}:?\d{2})?$", v)
    if m:
        return m.group(1) + ":00Z"
    return v


def parse_injuries(payload: Any, *, fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Normalise the league-wide ESPN payload into PlayerInjury records."""

    fetched_at = fetched_at or utc_now_iso()
    injuries: List[PlayerInjury] = []
    irregularities: List[Irregularity] = []

    if not isinstance(payload, dict):
        irregularities.append(
            Irregularity(
                code="ESPN_BAD_SHAPE",
                severity="high",
                title="ESPN injuries payload was not a JSON object",
                detail=f"Expected an object, got {type(payload).__name__}. No records parsed.",
                evidence=[{"label": "ESPN injuries endpoint", "url": ENDPOINT}],
            )
        )
        return _envelope(fetched_at, payload, injuries, irregularities, 0, 0)

    api_status = payload.get("status")
    if api_status and api_status != "success":
        irregularities.append(
            Irregularity(
                code="ESPN_STATUS_NOT_SUCCESS",
                severity="high",
                title="ESPN reported a non-success status",
                detail=f"ESPN returned status={api_status!r} from its injuries endpoint.",
                evidence=[{"label": "ESPN injuries endpoint", "url": ENDPOINT}],
            )
        )

    season = payload.get("season") or {}
    teams_payload = payload.get("injuries") or []
    n_teams = 0

    for team_block in teams_payload:
        if not isinstance(team_block, dict):
            continue
        n_teams += 1
        block_name = team_block.get("displayName") or ""
        rows = team_block.get("injuries") or []
        for item in rows:
            if not isinstance(item, dict):
                continue
            athlete = item.get("athlete") or {}
            team_obj = athlete.get("team") or {}
            code = _team_code(team_obj, block_name)
            if not code:
                irregularities.append(
                    Irregularity(
                        code="ESPN_TEAM_UNRESOLVED",
                        severity="medium",
                        title="ESPN team could not be mapped to an NFL club code",
                        detail=(
                            f"Injury id={item.get('id')} was published under team block "
                            f"{block_name!r} with abbreviation "
                            f"{team_obj.get('abbreviation')!r}, which is not one of the 32 NFL "
                            "club codes. The row was SKIPPED instead of being assigned a guess."
                        ),
                        evidence=[{"label": "ESPN injuries endpoint", "url": ENDPOINT}],
                    )
                )
                continue

            display = athlete.get("displayName") or " ".join(
                p for p in (athlete.get("firstName"), athlete.get("lastName")) if p
            )
            if not display:
                continue

            short = item.get("shortComment") or ""
            long_c = item.get("longComment") or ""
            reporter, outlet, handle = extract_attribution(short or long_c)
            position = (athlete.get("position") or {}).get("abbreviation") or ""
            links = athlete.get("links") or []
            player_url = (
                _pick_link(links, "playercard")
                or _pick_link(links, "overview")
                or (f"https://www.espn.com/nfl/team/_/name/{code.lower()}")
            )

            espn_athlete_id = ""
            m = re.search(r"/id/(\d+)", player_url)
            if m:
                espn_athlete_id = m.group(1)

            injuries.append(
                PlayerInjury(
                    source=SOURCE_NAME,
                    team=code,
                    player=display,
                    position=position,
                    injury=extract_body_part(short) or "",
                    game_status=norm_status(item.get("status")),
                    practice_status="NONE",
                    comment=re.sub(r"\s+", " ", (short or long_c)).strip(),
                    attribution=reporter,
                    observed_at=_normalise_date(item.get("date") or ""),
                    url=player_url,
                    source_ids={
                        "espn_injury_id": str(item.get("id") or ""),
                        "espn_athlete_id": espn_athlete_id,
                        "espn_team_id": str(team_obj.get("id") or team_block.get("id") or ""),
                    },
                    raw={
                        "status_raw": item.get("status"),
                        "outlet": outlet,
                        "handle": handle,
                        "headshot": (athlete.get("headshot") or {}).get("href", ""),
                        "long_comment": re.sub(r"\s+", " ", long_c).strip(),
                    },
                )
            )

    if n_teams == 0:
        irregularities.append(
            Irregularity(
                code="ESPN_EMPTY",
                severity="high",
                title="ESPN injuries payload contained no team blocks",
                detail=(
                    "The endpoint returned HTTP 200 but the 'injuries' array was empty or "
                    "missing. Note that the per-team variant of this endpoint "
                    "(/teams/{id}/injuries) is known to return {} and must not be used."
                ),
                evidence=[
                    {"label": "ESPN injuries endpoint", "url": ENDPOINT},
                    {"label": "Broken per-team variant (do not use)",
                     "url": _BROKEN_TEAM_ENDPOINT.format(team_id=22)},
                ],
            )
        )

    return _envelope(
        fetched_at, payload, injuries, irregularities, n_teams,
        int(season.get("year") or 0) or None,
    )


def _envelope(
    fetched_at: str,
    payload: Any,
    injuries: List[PlayerInjury],
    irregularities: List[Irregularity],
    n_teams: int,
    season: Optional[int] = None,
) -> Dict[str, Any]:
    source_ts = ""
    if isinstance(payload, dict):
        source_ts = _normalise_date(payload.get("timestamp") or "")
        season = season or (int((payload.get("season") or {}).get("year") or 0) or None)
    return {
        "source": SOURCE_NAME,
        "url": ENDPOINT,
        "fetched_at": fetched_at,
        "source_timestamp": source_ts,
        "season": season,
        "teams_found": n_teams,
        "injuries": injuries,
        "irregularities": irregularities,
    }


def collect(*, url: str = ENDPOINT, fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Fetch and parse the league-wide ESPN injuries feed."""

    payload = fetch_json(url, source=SOURCE_NAME, referer="https://www.espn.com/nfl/")
    return parse_injuries(payload, fetched_at=fetched_at)


# -------------------------------------------------------------- scoreboard ---
#: ESPN's keyless scoreboard. Verified live 2026-09-18 (HTTP 200) for
#: ?dates=20260917 -> the DET@BUF game (event 401872932, kickoff 2026-09-18T00:15Z,
#: status.type.state="post", final 41-31). It is used for ONE thing: deciding
#: which clubs are inside a game window right now, so the collector can spend its
#: limited query budget on those clubs instead of spraying 32 club queries.
SCOREBOARD_ENDPOINT = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


def collect_scoreboard(*, dates: Optional[str] = None,
                       fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Games for a date (YYYYMMDD) or the current week. Never raises on bad shape."""

    from .ingame import parse_scoreboard

    url = SCOREBOARD_ENDPOINT + (f"?dates={dates}" if dates else "")
    fetched_at = fetched_at or utc_now_iso()
    try:
        payload = fetch_json(url, source="espn")
    except FetchError as exc:
        return {"source": "espn-scoreboard", "url": url, "fetched_at": fetched_at,
                "games": [], "live": [], "hot_teams": [], "count": 0,
                "error": f"{exc.reason} (status={exc.status})"}
    parsed = parse_scoreboard(payload)
    parsed.update({"source": "espn-scoreboard", "url": url, "fetched_at": fetched_at})
    return parsed


# ----------------------------------------------------------------- rosters ---
#: ESPN's keyless game summary. The scoreboard (verified 2026-09-18) carries no
#: player lists, so "every player in an ongoing game" cannot be known from it
#: alone: the injury index only contains players who have an injury record, and
#: a player with no record at all was invisible to in-game matching (2026-09-21
#: gap: a headline naming such a player could never resolve to a roster entry).
#: The public espn.com game centre reads per-game rosters from this `site`
#: family endpoint (same host/shape family as the verified scoreboard/injuries/
#: news endpoints). It is PROBED in the source ledger on every run
#: (`espn_game_summary`) and parsed defensively: any shape the parser does not
#: recognise is ignored rather than guessed, and a fetch failure degrades to an
#: empty roster plus a flag instead of aborting the run.
SUMMARY_ENDPOINT = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"
)


def parse_roster(payload: Any, *, game: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Extract (team, name, position) rows from a game summary payload.

    Tries the documented `boxscore.teams[].athletes[]` shape first. Team codes
    come from the payload when present; otherwise from the scoreboard game's
    team list in the same order. Any row without a usable name is skipped —
    nothing is invented.
    """

    rows: List[Dict[str, str]] = []
    if not isinstance(payload, dict):
        return rows

    box = payload.get("boxscore")
    if not isinstance(box, dict):
        return rows
    teams_blocks = box.get("teams")
    if not isinstance(teams_blocks, list):
        return rows
    game_teams = [t.get("code") or "" for t in ((game or {}).get("teams") or [])]

    def _position_of(athlete: Dict[str, Any]) -> str:
        pos = athlete.get("position")
        if isinstance(pos, dict):
            return (pos.get("abbreviation") or pos.get("displayName") or "").strip().upper()
        if isinstance(pos, str):
            return pos.strip().upper()
        return ""

    for i, block in enumerate(teams_blocks):
        if not isinstance(block, dict):
            continue
        team_obj = block.get("team") or {}
        code = (team_obj.get("abbreviation") or "").upper()
        if code not in NFL_TEAMS:
            # Fall back to the scoreboard's ordering for this game.
            code = game_teams[i] if i < len(game_teams) else ""
        if code not in NFL_TEAMS:
            continue
        athletes = block.get("athletes")
        if not isinstance(athletes, list):
            continue
        for athlete in athletes:
            if not isinstance(athlete, dict):
                continue
            name = (athlete.get("displayName") or "").strip()
            if not name:
                continue
            rows.append({"team": code, "name": name,
                         "position": _position_of(athlete)})
    return rows


def collect_rosters(games: List[Dict[str, Any]], *,
                    fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Game-day rosters for games that are live or about to start.

    `games` is the scoreboard's game list; only state in ("in", "pre") is
    fetched, because "all players in ongoing games" is what the query budget
    needs (a finished game's rosters add nothing the injury index lacks for
    alerting purposes). Each game is one request; a failure on one game never
    stops the others.
    """

    fetched_at = fetched_at or utc_now_iso()
    rows: List[Dict[str, str]] = []
    calls: List[Dict[str, Any]] = []
    irregularities: List[Irregularity] = []

    for game in games or []:
        state = (game or {}).get("state") or ""
        if state not in ("in", "pre"):
            continue
        event_id = str((game or {}).get("id") or "")
        if not event_id:
            continue
        url = SUMMARY_ENDPOINT.format(event_id=event_id)
        try:
            payload = fetch_json(url, source="espn", referer="https://www.espn.com/nfl/")
        except FetchError as exc:
            irregularities.append(
                Irregularity(
                    code="ESPN_ROSTER_UNREACHABLE", severity="low",
                    title=f"Game-day roster for {game.get('short_name') or event_id} could not be read",
                    detail=(
                        f"{exc.reason} (status={exc.status}) while fetching {url}. The "
                        "game-day roster for this game is missing from the player index, so "
                        "players who carry no injury record cannot be matched to a headline "
                        "for this game on this run. Other games were still tried."
                    ),
                    evidence=[{"label": "Failing URL", "url": url}],
                )
            )
            calls.append({"game": game.get("short_name") or event_id, "url": url,
                          "rows": 0, "error": exc.reason})
            continue
        game_rows = parse_roster(payload, game=game)
        rows.extend(game_rows)
        calls.append({"game": game.get("short_name") or event_id, "url": url,
                      "rows": len(game_rows), "error": ""})
        if not game_rows:
            # The endpoint answered but gave no recognisable athletes: the
            # shape may have changed. Report it so a silent coverage hole is
            # visible in health/flags instead of hiding as "no rosters".
            irregularities.append(
                Irregularity(
                    code="ESPN_ROSTER_EMPTY", severity="low",
                    title=f"Game-day roster for {game.get('short_name') or event_id} parsed zero athletes",
                    detail=(
                        f"ESPN answered from {url} but the payload contained no "
                        "boxscore.teams[].athletes the parser recognises. The roster is "
                        "skipped rather than guessed; if this persists the parser and the "
                        "ledger probe need a look."
                    ),
                    evidence=[{"label": "Summary URL", "url": url}],
                )
            )
    return {"source": "espn-roster", "fetched_at": fetched_at, "rows": rows,
            "calls": calls, "irregularities": irregularities}


# ------------------------------------------------------------------- news -----
#: ESPN's keyless news API. Verified live 2026-09-18 (HTTP 200) with this shape
#: (keys verbatim):
#:   {"header": "NFL News",
#:    "articles": [{"id": 49970421, "headline": "...", "description": "...",
#:                  "published": "2026-09-18T06:35:37Z",
#:                  "byline": "Eric Woodyard",
#:                  "categories": [{"type": "team", "description": "Buffalo Bills",
#:                                  "teamId": 2, "team": {"abbreviation": "BUF"}},
#:                                 {"type": "athlete", "description": "DJ Moore",
#:                                  "athleteId": 3915416}],
#:                  "links": {"web": {"href": "https://www.espn.com/nfl/story/_/id/..."}}}]}
#:
#: IMPORTANT, measured 2026-09-18: the `?athlete=<id>` filter is IGNORED by this
#: endpoint -- `?athlete=4635008&limit=2` returned the same league-wide articles
#: as `?limit=2`, so it must never be presented as a per-player feed. Per-club
#: filtering (`?team=2`) does work and is what the in-game collector uses, and
#: articles that mention a player are still matched to that player locally via
#: the `categories` array.
NEWS_ENDPOINT = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"


def parse_news(payload: Any, *, fetched_at: Optional[str] = None,
               source_kind: str = "news") -> Dict[str, Any]:
    """Normalise ESPN news articles into SocialPost records.

    Articles are news, not claims: they carry a headline and a timestamp, and the
    in-game classifier in collectors/ingame.py decides whether the headline says
    anything about availability. Nothing is inferred from the article body,
    because the feed does not include one.
    """

    from .social import SocialPost, classify

    fetched_at = fetched_at or utc_now_iso()
    items: List[SocialPost] = []
    irregularities: List[Irregularity] = []

    if not isinstance(payload, dict):
        irregularities.append(
            Irregularity(
                code="ESPN_NEWS_BAD_SHAPE", severity="medium",
                title="ESPN news payload was not a JSON object",
                detail=f"Expected an object, got {type(payload).__name__}.",
                evidence=[{"label": "ESPN news endpoint", "url": NEWS_ENDPOINT}],
            )
        )
        return {"source": "espn-news", "url": NEWS_ENDPOINT, "fetched_at": fetched_at,
                "items": items, "irregularities": irregularities}

    for art in payload.get("articles") or []:
        if not isinstance(art, dict):
            continue
        headline = (art.get("headline") or "").strip()
        description = (art.get("description") or "").strip()
        text = f"{headline}. {description}".strip(" .")
        if not text:
            continue
        cats = art.get("categories") or []
        team_codes: List[str] = []
        athletes: List[str] = []
        for cat in cats:
            if not isinstance(cat, dict):
                continue
            ctype = cat.get("type") or ""
            if ctype == "team":
                abbr = ((cat.get("team") or {}).get("abbreviation") or "").upper()
                if abbr in NFL_TEAMS and abbr not in team_codes:
                    team_codes.append(abbr)
            elif ctype == "athlete":
                nm = cat.get("description") or ""
                if nm:
                    athletes.append(nm)
        url = (((art.get("links") or {}).get("web") or {}).get("href")
               or (art.get("links") or {}).get("mobile", {}).get("href")
               or "")
        cls = classify(text)
        items.append(
            SocialPost(
                platform="espn-news",
                post_id=str(art.get("id") or url),
                author=(art.get("byline") or "").strip(),
                author_name=(art.get("byline") or "").strip(),
                author_url="",
                text=text,
                posted_at=(art.get("published") or art.get("lastModified") or "").strip(),
                url=url,
                predicted_status=cls["status"],
                injury=cls["injury"],
                signal=cls["signal"],
                source_kind=source_kind,
                team=(team_codes[0] if len(team_codes) == 1 else ""),
                raw={"teams": team_codes, "athletes": athletes,
                     "type": art.get("type"), "headline": headline},
            )
        )
    return {"source": "espn-news", "url": NEWS_ENDPOINT, "fetched_at": fetched_at,
            "articles": len(payload.get("articles") or []), "items": items,
            "irregularities": irregularities}


def collect_news(*, team_ids: Optional[Dict[str, str]] = None,
                 limit: int = 20, fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Fetch league news, plus per-club news for clubs that are playing now.

    `team_ids` maps club code -> ESPN team id (only the clubs in a live/finished
    game window are passed by the pipeline, to stay within a small request budget).
    """

    items: List[SocialPost] = []
    irregularities: List[Irregularity] = []
    fetched_at = fetched_at or utc_now_iso()
    calls: List[Dict[str, Any]] = []

    try:
        league = parse_news(
            fetch_json(f"{NEWS_ENDPOINT}?limit={max(1, min(limit, 50))}", source="espn"),
            fetched_at=fetched_at)
        items.extend(league["items"])
        irregularities.extend(league["irregularities"])
        calls.append({"query": "league", "url": NEWS_ENDPOINT, "items": len(league["items"])})
    except FetchError as exc:
        irregularities.append(
            Irregularity(
                code="ESPN_NEWS_UNREACHABLE", severity="medium",
                title="ESPN news API could not be read",
                detail=f"{exc.reason} (status={exc.status}). In-game headline coverage is "
                       "reduced to Google News for this run.",
                evidence=[{"label": "ESPN news endpoint", "url": exc.url}],
            )
        )

    for code, team_id in sorted((team_ids or {}).items()):
        if not team_id:
            continue
        url = f"{NEWS_ENDPOINT}?team={team_id}&limit={max(1, min(limit, 50))}"
        try:
            payload = parse_news(fetch_json(url, source="espn"), fetched_at=fetched_at)
            items.extend(payload["items"])
            calls.append({"query": f"team:{code}", "url": url, "items": len(payload["items"])})
        except FetchError as exc:
            irregularities.append(
                Irregularity(
                    code="ESPN_NEWS_TEAM_UNREACHABLE", severity="low",
                    title=f"ESPN news feed for {code} could not be read",
                    detail=f"{exc.reason} (status={exc.status}) while fetching {exc.url}.",
                    evidence=[{"label": "Failing URL", "url": exc.url}],
                )
            )

    return {"source": "espn-news", "url": NEWS_ENDPOINT, "fetched_at": fetched_at,
            "calls": calls, "items": items, "irregularities": irregularities}
