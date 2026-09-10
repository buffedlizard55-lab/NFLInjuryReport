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

from .http import fetch_json, utc_now_iso
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
