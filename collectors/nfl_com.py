"""Collector for the OFFICIAL NFL injury report at https://www.nfl.com/injuries/

This is the authoritative source: the page is the league's own publication of the
Game Status Reports that clubs file under the NFL Injury Report Policy
(https://operations.nfl.com/gameday/injury-report/).

Verified live on 2026-09-10 (HTTP 200). Page title observed:
"Official Latest NFL Injury Report for Players - Week 1 of the 2026 Season | NFL.com".
The report renders as per-game tables with the columns
Player | Position | Injuries | Practice Status | Game Status, and exposes a season
selector going back to 1965.

Design notes
------------
* Class-name agnostic: we extract real <table> elements in document order and
  attribute each table to the nearest preceding club marker, so the collector
  survives nfl.com front-end redesigns.
* The NFL's own JSON API (https://api.nfl.com/experience/v1/gamecenter/injuries)
  was probed on 2026-09-10 and returned HTTP 401 Unauthorized (Varnish 54113).
  It needs an OAuth token, so it is NOT used. See README "Source ledger".
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from .http import fetch_text, utc_now_iso
from .models import OFFICIAL_NFL_INJURY_URL, Irregularity, PlayerInjury, norm_practice, norm_status

SOURCE_NAME = "nfl.com"

NFL_TEAMS: Dict[str, str] = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

_TEAM_BY_NAME = {v: k for k, v in NFL_TEAMS.items()}


def _unique_part_index(part_idx: int) -> Dict[str, str]:
    """Index teams by one word of their name, dropping ambiguous entries.

    "New York" and "Los Angeles" each belong to two clubs, so they are excluded:
    a guess there would silently attach injuries to the wrong franchise.
    """
    counts: Dict[str, int] = {}
    for code, name in NFL_TEAMS.items():
        part = name.split(" ")[part_idx].lower()
        counts[part] = counts.get(part, 0) + 1
    out: Dict[str, str] = {}
    for code, name in NFL_TEAMS.items():
        part = name.split(" ")[part_idx]
        if counts[part.lower()] == 1:
            out[part] = code
    return out


#: "Patriots" -> NE (nicknames are unique across the league)
_TEAM_BY_NICK = _unique_part_index(-1)
#: "New England" -> NE, but "New York"/"Los Angeles" are excluded as ambiguous
_TEAM_BY_CITY = _unique_part_index(0)

#: nfl.com has used "LA" for the Rams in club-logo URLs; normalise legacy codes.
CODE_ALIASES = {"LA": "LAR", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "WFT": "WAS"}

_DAY = r"(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)"
_DATE_RE = re.compile(rf"{_DAY},\s+([A-Z]+)\s+(\d{{1,2}})(?:ST|ND|RD|TH)?", re.IGNORECASE)
_SEASON_TITLE_RE = re.compile(r"Week\s+(\d+)\s+of\s+the\s+(\d{4})\s+Season", re.IGNORECASE)
_WEEK_RE = re.compile(r"Week\s+(\d+)", re.IGNORECASE)

_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}

_SKIP_TAGS = {"script", "style", "noscript", "svg"}
#: Sentinel pushed into the context stream at each <table> so that markers can be
#: bucketed to the table they precede without tracking raw byte offsets.
_TABLE_BOUNDARY = "\x00TABLE\x00"


class _Cell:
    __slots__ = ("text_parts", "links")

    def __init__(self) -> None:
        self.text_parts: List[str] = []
        self.links: List[Tuple[str, str]] = []

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts)).strip()


class _ReportParser(HTMLParser):
    """Single pass: collects tables (in order) and the text stream around them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[_Cell]]] = []
        self.context: List[str] = []
        self.title_parts: List[str] = []
        self._skip_depth = 0
        self._table_depth = 0
        self._cur_table: Optional[List[List[_Cell]]] = None
        self._cur_row: Optional[List[_Cell]] = None
        self._cur_cell: Optional[_Cell] = None
        self._cur_href: Optional[str] = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = dict(attrs)
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._cur_table = []
                self.context.append(_TABLE_BOUNDARY)
            return
        if self._table_depth:
            if tag == "tr":
                self._cur_row = []
            elif tag in ("td", "th"):
                self._cur_cell = _Cell()
            elif tag == "a" and self._cur_cell is not None:
                self._cur_href = a.get("href")
            return
        if tag == "img":
            # Club logos encode the club code in the URL, e.g.
            # .../league/api/clubs/logos/SEA -- a reliable team signal.
            code = _club_code_from_url(a.get("src") or a.get("data-src") or "")
            if code:
                self.context.append(code)
            if a.get("alt"):
                self.context.append(a["alt"])

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _SKIP_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._cur_table is not None:
                self.tables.append(self._cur_table)
                self._cur_table = None
            return
        if self._table_depth:
            if tag == "tr" and self._cur_row is not None:
                if self._cur_table is not None and self._cur_row:
                    self._cur_table.append(self._cur_row)
                self._cur_row = None
            elif tag in ("td", "th") and self._cur_cell is not None:
                if self._cur_row is not None:
                    self._cur_row.append(self._cur_cell)
                self._cur_cell = None
            elif tag == "a":
                self._cur_href = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if not data.strip():
            return
        if self._cur_cell is not None:
            self._cur_cell.text_parts.append(data)
            if self._cur_href:
                self._cur_cell.links.append((self._cur_href, data))
        else:
            self.context.append(data)


def _club_code_from_url(url: str) -> str:
    m = re.search(r"/clubs/logos/([A-Za-z]{2,4})", url or "")
    if not m:
        return ""
    code = CODE_ALIASES.get(m.group(1).upper(), m.group(1).upper())
    return code if code in NFL_TEAMS else ""


def _header_map(header: List[_Cell]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for idx, cell in enumerate(header):
        label = re.sub(r"\s+", " ", cell.text).strip().lower()
        if label in ("player", "players", "name"):
            out["player"] = idx
        elif label in ("position", "pos"):
            out["position"] = idx
        elif label in ("injuries", "injury"):
            out["injury"] = idx
        elif "practice" in label:
            out["practice"] = idx
        elif "game" in label or "status" in label:
            out["game"] = idx
    return out


def _is_injury_table(rows: List[List[_Cell]]) -> bool:
    if len(rows) < 2:
        return False
    header = _header_map(rows[0])
    return "player" in header and ("injury" in header or "game" in header)


def _match_team(text: str) -> str:
    """Resolve a context string to a club code, or "" when it is not a club.

    Only EXACT whole-string matches are accepted, in order of specificity:
    club code, full name, nickname, unambiguous city. Substring matching is
    deliberately not used -- it is how a wrong club gets attached to a player.
    """

    stripped = text.strip()
    if not stripped:
        return ""
    if stripped in NFL_TEAMS:
        return stripped
    if stripped in _TEAM_BY_NAME:
        return _TEAM_BY_NAME[stripped]
    if stripped in _TEAM_BY_NICK:
        return _TEAM_BY_NICK[stripped]
    if stripped in _TEAM_BY_CITY:
        return _TEAM_BY_CITY[stripped]
    upper = stripped.upper()
    if upper in NFL_TEAMS:
        return upper
    return ""


def _parse_date(text: str) -> str:
    m = _DATE_RE.search(text)
    if not m:
        return ""
    month = _MONTHS.get(m.group(1).upper())
    if not month:
        return ""
    return f"{month:02d}-{int(m.group(2)):02d}"


def _bucket_markers(context: List[str], n_tables: int) -> List[List[Tuple[str, str]]]:
    """Split context markers into per-table buckets in document order."""

    buckets: List[List[Tuple[str, str]]] = [[] for _ in range(max(n_tables, 1))]
    current = 0
    for chunk in context:
        if chunk == _TABLE_BOUNDARY:
            current = min(current + 1, len(buckets) - 1)
            continue
        text = chunk.strip()
        if not text:
            continue
        team = _match_team(text)
        if team:
            buckets[current].append(("team", team))
            continue
        date = _parse_date(text)
        if date:
            buckets[current].append(("date", date))
    return buckets


def _row_to_injury(
    row: List[_Cell],
    cols: Dict[str, int],
    team: str,
    date_hint: str,
    fetched_at: str,
    season: Optional[int],
    week: Optional[int],
) -> Optional[PlayerInjury]:
    def cell(name: str) -> _Cell:
        idx = cols.get(name)
        if idx is None or idx >= len(row):
            return _Cell()
        return row[idx]

    pcell = cell("player")
    name = pcell.text.strip()
    if not name:
        return None

    url = ""
    for href, _ in pcell.links:
        if href and "/players/" in href:
            url = href if href.startswith("http") else "https://www.nfl.com" + href
            break

    practice = norm_practice(cell("practice").text.strip())
    game_raw = cell("game").text.strip()
    game = norm_status(game_raw)

    # nfl.com leaves Game Status blank for players who practised without a
    # designation. A practice participation with no designation means available.
    if not game_raw and practice in ("FULL", "LIMITED"):
        game = "ACTIVE"

    observed = fetched_at
    if date_hint and season:
        observed = f"{season}-{date_hint}T00:00:00Z"

    m = re.search(r"/players/([^/?#]+)/?", url or "")
    return PlayerInjury(
        source=SOURCE_NAME,
        team=team,
        player=name,
        position=cell("position").text.strip(),
        injury=cell("injury").text.strip(),
        game_status=game,
        practice_status=practice,
        observed_at=observed,
        url=url or OFFICIAL_NFL_INJURY_URL,
        source_ids={"nfl_slug": m.group(1) if m else ""},
        raw={"game_status_raw": game_raw, "report_week": week,
             "report_season": season, "game_date": date_hint},
    )


def parse_injuries_html(html: str, *, fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Parse the official injury report page into PlayerInjury records."""

    fetched_at = fetched_at or utc_now_iso()
    parser = _ReportParser()
    parser.feed(html)
    parser.close()

    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()
    season: Optional[int] = None
    week: Optional[int] = None
    m = _SEASON_TITLE_RE.search(title)
    if m:
        week, season = int(m.group(1)), int(m.group(2))

    buckets = _bucket_markers(parser.context, len(parser.tables))

    injuries: List[PlayerInjury] = []
    irregularities: List[Irregularity] = []
    tables_seen = 0
    teams_seen: List[str] = []

    # Club attribution. nfl.com puts BOTH club logos in the game header, then a
    # per-club heading immediately above each table. So a bucket can contain
    # either one club marker (the per-club heading case) or several (the game
    # header case). Rule, verified against the fixture:
    #   * exactly one distinct club in the bucket  -> that club
    #   * several clubs                            -> positional: the Nth injury
    #     table inside a game belongs to the Nth club of that game
    # Positional index resets on every date marker (a new game).
    game_teams: List[str] = []
    seq_in_game = 0
    cur_team = ""
    cur_date = ""

    for i, rows in enumerate(parser.tables):
        bucket = buckets[i]
        if any(kind == "date" for kind, _ in bucket):
            game_teams = []
            seq_in_game = 0
            cur_date = [v for k, v in bucket if k == "date"][-1]

        teams_here = [v for k, v in bucket if k == "team"]
        for t in teams_here:
            if t not in game_teams:
                game_teams.append(t)
            if t not in teams_seen:
                teams_seen.append(t)

        distinct = list(dict.fromkeys(teams_here))
        if len(distinct) == 1:
            cur_team = distinct[0]
        elif seq_in_game < len(game_teams):
            cur_team = game_teams[seq_in_game]
        elif teams_here:
            cur_team = teams_here[-1]

        if not _is_injury_table(rows):
            continue
        tables_seen += 1
        seq_in_game += 1
        cols = _header_map(rows[0])
        if not cur_team:
            irregularities.append(
                Irregularity(
                    code="NFL_TABLE_NO_TEAM",
                    severity="medium",
                    title="Injury table with no attributable team",
                    detail=(
                        f"Table #{i + 1} on the official report could not be attributed to a "
                        "club from the surrounding markup. Its rows were SKIPPED rather than "
                        "guessed at, so the affected club may be missing from this snapshot."
                    ),
                    evidence=[{"label": "Official NFL injury report",
                               "url": OFFICIAL_NFL_INJURY_URL}],
                )
            )
            continue
        for row in rows[1:]:
            rec = _row_to_injury(row, cols, cur_team, cur_date, fetched_at, season, week)
            if rec is not None:
                injuries.append(rec)

    if tables_seen == 0:
        irregularities.append(
            Irregularity(
                code="NFL_NO_TABLES",
                severity="high",
                title="No injury tables found on the official report page",
                detail=(
                    "The page was fetched successfully but contained no recognisable injury "
                    "tables. This usually means nfl.com changed its markup, or the league has "
                    "not yet published a report for the current week (reports are published "
                    "Wed/Thu/Fri and on game day, per the NFL Injury Report Policy)."
                ),
                evidence=[
                    {"label": "Official NFL injury report", "url": OFFICIAL_NFL_INJURY_URL},
                    {"label": "NFL Injury Report Policy",
                     "url": "https://operations.nfl.com/gameday/injury-report/"},
                ],
            )
        )

    return {
        "source": SOURCE_NAME,
        "url": OFFICIAL_NFL_INJURY_URL,
        "fetched_at": fetched_at,
        "season": season,
        "week": week,
        "title": title,
        "tables_found": len(parser.tables),
        "injury_tables": tables_seen,
        "teams_seen": teams_seen,
        "injuries": injuries,
        "irregularities": irregularities,
    }


def collect(*, url: str = OFFICIAL_NFL_INJURY_URL, fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Fetch and parse the official report. Raises FetchError on network failure."""

    html = fetch_text(url, source=SOURCE_NAME, referer="https://www.nfl.com/")
    return parse_injuries_html(html, fetched_at=fetched_at)
