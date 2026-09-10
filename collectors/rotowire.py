"""Reverse-engineered collector for https://www.rotowire.com/football/lineups.php

Verified live on 2026-09-10 (HTTP 200). Structure observed in the served markup:

  * Game blocks headed by a day/time string, e.g. "WED 8:20 PM ET".
  * Two team logos per game, e.g.
        https://assets.rotowire.com/images/teamlogo/football/NE.svg?v=12   alt="NE"
    followed by "Patriots (0-1)".
  * Per-team projected starters as list items: a position token ("QB", "RB", ...)
    followed by an anchor to /football/player/<slug>-<id> whose `title` attribute
    is the full player name, optionally followed by a one-letter status flag
    (Q = questionable, D = doubtful). Example anchors captured:
        /football/player/drake-maye-17673        title="Drake Maye"
        /football/player/sam-darnold-12490       title="Sam Darnold"  -> flag "D"
        /football/player/aj-brown-13432          title="A.J. Brown"   -> flag "Q"
  * An "Inactives" section per team listing the players ruled out; before the
    league publishes inactives it renders "Not Yet Available".
  * Non-injury context: weather, spread, moneyline and total per game.

FLAGGED FOR REVIEW (see README): RotoWire's *injury report* page
(https://www.rotowire.com/football/injury-report.php) was fetched on 2026-09-10 and
its "Est. Return" column renders as "Subscribers Only" behind an
"Unlock the Full Injury Report Today / Subscribe Now" paywall. So RotoWire is used
here ONLY for lineup/inactive corroboration, never as a primary injury source.
Also note that scraping RotoWire may be restricted by their Terms of Use -- the
pipeline keeps this source behind a flag (--with-rotowire) and off by default.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from .http import fetch_text, utc_now_iso
from .models import Irregularity, PlayerInjury, norm_status
from .nfl_com import NFL_TEAMS

SOURCE_NAME = "rotowire"
LINEUPS_URL = "https://www.rotowire.com/football/lineups.php"

_LOGO_RE = re.compile(r"/teamlogo/football/([A-Za-z]{2,4})\.svg", re.IGNORECASE)
_PLAYER_RE = re.compile(r"/football/player/([a-z0-9\-]+?)-(\d+)/?$", re.IGNORECASE)
_RECORD_RE = re.compile(r"^\s*([A-Za-z .]+?)\s*\((\d+-\d+(?:-\d+)?)\)\s*$")

_STATUS_LETTERS = {"Q": "QUESTIONABLE", "D": "DOUBTFUL", "O": "OUT", "P": "ACTIVE", "S": "SUSPENDED"}
_POSITIONS = {
    "QB", "RB", "WR", "TE", "K", "KR", "PR", "FB", "DEF", "DST", "IDP",
    "OT", "OG", "C", "OL", "DE", "DT", "NT", "DL", "LB", "ILB", "OLB", "MLB",
    "CB", "S", "SS", "FS", "DB", "LS", "P",
}
_TIME_RE = re.compile(
    r"(MON|TUE|WED|THU|FRI|SAT|SUN)\s+(\d{1,2}:\d{2}\s*[AP]M\s*ET)", re.IGNORECASE
)


class _Cell:
    __slots__ = ("text_parts", "links")

    def __init__(self) -> None:
        self.text_parts: List[str] = []
        self.links: List[Tuple[str, str, str]] = []  # href, title, text

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts)).strip()


class _LineupsParser(HTMLParser):
    """Streams the page and emits lineup entries with game/team context."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: List[Dict[str, Any]] = []
        self.games: List[Dict[str, Any]] = []
        self.irregularities: List[Irregularity] = []
        self.title_parts: List[str] = []

        self._skip_depth = 0
        self._in_title = False
        self._in_li = 0
        self._li: Optional[_Cell] = None
        self._pending_pos = ""
        self._cur_href: Optional[str] = None
        self._cur_title: Optional[str] = None

        self._cur_game: Dict[str, Any] = {}
        self._teams: List[str] = []
        self._inactives = False

    # -- context ------------------------------------------------------------
    def _new_game(self, when: str) -> None:
        self._cur_game = {"when": when, "teams": []}
        self._teams = []
        self._inactives = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "img":
            code = _club_from_logo(a.get("src") or a.get("data-src") or "")
            if code:
                if len(self._teams) >= 2:
                    self._new_game("")
                self._teams.append(code)
                self._cur_game["teams"] = list(self._teams)
                if self._cur_game not in self.games:
                    self.games.append(self._cur_game)
            return
        if tag == "li":
            self._in_li += 1
            if self._in_li == 1:
                self._li = _Cell()
            return
        if tag == "a" and self._li is not None:
            self._cur_href = a.get("href")
            self._cur_title = a.get("title")

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("script", "style", "noscript", "svg"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a":
            self._cur_href = None
            self._cur_title = None
            return
        if tag == "li" and self._in_li:
            self._in_li -= 1
            if self._in_li == 0 and self._li is not None:
                self._consume_li(self._li)
                self._li = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        text = data.strip()
        if not text:
            return
        if text.lower().startswith("inactive"):
            self._inactives = True
        if self._li is not None:
            self._li.text_parts.append(data)
            if self._cur_href:
                self._li.links.append((self._cur_href, self._cur_title or "", data))
            return
        m = _TIME_RE.search(text)
        if m:
            self._new_game(f"{m.group(1).upper()} {m.group(2)}")

    # -- entry extraction ---------------------------------------------------
    def _consume_li(self, cell: _Cell) -> None:
        if not self._teams:
            return
        player_link: Optional[Tuple[str, str, str]] = None
        for href, title, _text in cell.links:
            if href and _PLAYER_RE.search(href):
                player_link = (href, title, _text)
                break
        if player_link is None:
            return

        href, title, link_text = player_link
        slug = _PLAYER_RE.search(href)
        rw_id = slug.group(2) if slug else ""
        name = (title or "").strip() or link_text.strip()
        if not name:
            return

        # Position token: the text in the item before the anchor.
        position = ""
        full = cell.text
        tokens = full.split()
        for tok in tokens:
            clean = tok.strip(".,:")
            if clean.upper() in _POSITIONS:
                position = clean.upper()
                break

        # Status flag: a lone letter immediately after the player's link text.
        flag = ""
        tail = full.split(name)[-1].strip() if name in full else ""
        if tail:
            first = tail[0].upper()
            if first in _STATUS_LETTERS and (len(tail) == 1 or not tail[1].isalpha()):
                flag = first

        # Team attribution: RotoWire alternates home/away per block. Without an
        # explicit marker we cannot be certain which side a list item belongs to,
        # so record both candidates and let the reconciler confirm by roster.
        team = self._teams[0] if len(self._teams) == 1 else ""

        if self._inactives:
            status = "OUT"
        elif flag:
            status = _STATUS_LETTERS[flag]
        else:
            status = "ACTIVE"

        self.entries.append(
            {
                "player": name,
                "position": position,
                "team": team,
                "team_candidates": list(self._teams),
                "status": status,
                "flag": flag,
                "section": "inactives" if self._inactives else "lineup",
                "url": href if href.startswith("http") else "https://www.rotowire.com" + href,
                "rotowire_id": rw_id,
                "game": self._cur_game.get("when", ""),
            }
        )


def _club_from_logo(url: str) -> str:
    m = _LOGO_RE.search(url or "")
    if not m:
        return ""
    code = m.group(1).upper()
    return code if code in NFL_TEAMS else ""


def parse_lineups_html(html: str, *, fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Parse RotoWire lineups into PlayerInjury records for non-active players only."""

    fetched_at = fetched_at or utc_now_iso()
    parser = _LineupsParser()
    parser.feed(html)
    parser.close()

    injuries: List[PlayerInjury] = []
    irregularities: List[Irregularity] = list(parser.irregularities)
    ambiguous = 0

    for e in parser.entries:
        if e["status"] == "ACTIVE" and e["section"] == "lineup":
            continue  # only carry players RotoWire flags or lists as inactive
        team = e["team"]
        if not team:
            ambiguous += 1
        injuries.append(
            PlayerInjury(
                source=SOURCE_NAME,
                team=team,
                player=e["player"],
                position=e["position"],
                injury="",
                game_status=norm_status(e["status"]),
                observed_at=fetched_at,
                url=e["url"],
                source_ids={"rotowire_id": e["rotowire_id"]},
                raw={
                    "section": e["section"],
                    "flag": e["flag"],
                    "team_candidates": e["team_candidates"],
                    "game": e["game"],
                },
            )
        )

    if ambiguous:
        irregularities.append(
            Irregularity(
                code="RW_TEAM_AMBIGUOUS",
                severity="low",
                title="RotoWire lineup rows lack an explicit team marker",
                detail=(
                    f"{ambiguous} flagged RotoWire rows sit in a two-team game block with no "
                    "unambiguous club marker in the markup, so `team` was left empty and both "
                    "candidates were stored in raw.team_candidates. The reconciler resolves "
                    "these against the official report rather than guessing."
                ),
                evidence=[{"label": "RotoWire lineups", "url": LINEUPS_URL}],
            )
        )

    if not parser.entries:
        irregularities.append(
            Irregularity(
                code="RW_NO_ENTRIES",
                severity="medium",
                title="No lineup entries parsed from RotoWire",
                detail=(
                    "The page fetched successfully but no /football/player/ anchors were found. "
                    "Either the markup changed or there are no projected lineups published yet."
                ),
                evidence=[{"label": "RotoWire lineups", "url": LINEUPS_URL}],
            )
        )

    return {
        "source": SOURCE_NAME,
        "url": LINEUPS_URL,
        "fetched_at": fetched_at,
        "title": re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip(),
        "games": parser.games,
        "entries_parsed": len(parser.entries),
        "injuries": injuries,
        "irregularities": irregularities,
    }


def collect(*, url: str = LINEUPS_URL, fetched_at: Optional[str] = None) -> Dict[str, Any]:
    html = fetch_text(url, source=SOURCE_NAME, referer="https://www.rotowire.com/football/")
    return parse_lineups_html(html, fetched_at=fetched_at)
