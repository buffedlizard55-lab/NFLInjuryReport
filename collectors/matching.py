"""Roster/name matching used to tie free-text social posts to a specific player.

There is no published crosswalk between nfl.com slugs, ESPN athlete IDs and
RotoWire IDs, so identity is a normalised name plus club code. This module keeps
that logic in one place and is deliberately conservative: it returns no match
rather than a low-confidence guess, because a wrong player match would poison the
reporter scorecard.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import slugify

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


class PlayerIndex:
    """Lookup of known players, keyed by club + normalised name.

    Keying on club+name matters: two different NFL players share a name (there is
    more than one Chris Jones in the league), and collapsing them into one record
    would let a free-text post about one silently attach to the other. Records
    are therefore `"<TEAM>:<name-slug>"`, and a match is only returned when it is
    unambiguous or when a team hint narrows it to exactly one player.
    """

    def __init__(self) -> None:
        self.players: Dict[str, Dict[str, Any]] = {}
        self._by_last: Dict[str, List[str]] = {}
        self._by_initial_last: Dict[str, List[str]] = {}

    # -- build --------------------------------------------------------------
    def add(self, name: str, team: str, position: str = "", url: str = "",
            source: str = "", ids: Optional[Dict[str, str]] = None) -> None:
        if not name:
            return
        name_key = slugify(name)
        if not name_key:
            return
        key = f"{team or ''}:{name_key}"
        rec = self.players.get(key)
        if rec is None:
            rec = {
                "key": name_key, "name": name, "team": team,
                "positions": [], "urls": {}, "ids": {}, "sources": [],
            }
            self.players[key] = rec
            self._index(key, name)
        if len(name) > len(rec["name"]):
            rec["name"] = name
        if position and position not in rec["positions"]:
            rec["positions"].append(position)
        if source and url:
            rec["urls"][source] = url
        if source and source not in rec["sources"]:
            rec["sources"].append(source)
        for idk, idv in (ids or {}).items():
            if idv:
                rec["ids"][idk] = idv

    def add_many(self, injuries: Iterable[Any]) -> None:
        for rec in injuries:
            self.add(
                rec.player, rec.team, rec.position, rec.url, rec.source,
                getattr(rec, "source_ids", None),
            )

    def _index(self, key: str, name: str) -> None:
        parts = _name_parts(name)
        if not parts:
            return
        last = parts[-1]
        self._by_last.setdefault(last, [])
        if key not in self._by_last[last]:
            self._by_last[last].append(key)
        if len(parts) >= 2:
            ik = f"{parts[0][0]}-{last}"
            self._by_initial_last.setdefault(ik, [])
            if key not in self._by_initial_last[ik]:
                self._by_initial_last[ik].append(key)

    # -- query --------------------------------------------------------------
    def _narrow(self, keys: List[str], team_hint: str) -> Optional[Dict[str, Any]]:
        if team_hint:
            keys = [k for k in keys if k.startswith(f"{team_hint}:")] or keys
        if len(keys) != 1:
            return None  # ambiguous -> no match, never a guess
        return self.players[keys[0]]

    def lookup(self, name: str, team: str = "") -> Optional[Dict[str, Any]]:
        return self.players.get(f"{team}:{slugify(name)}")

    def candidates_for(self, name: str) -> List[Dict[str, Any]]:
        slug = slugify(name)
        return [r for k, r in self.players.items() if k.endswith(":" + slug)]

    def find_in_text(self, text: str, *, team_hint: str = "") -> Optional[Dict[str, Any]]:
        """Best player match inside free text, or None when ambiguous/absent.

        Preference order: full name, then initial+surname, then unique surname.
        A surname shared by more than one known player is NOT resolved without a
        team hint that narrows it to exactly one.
        """

        if not text:
            return None
        low_nopunct = re.sub(r"[^a-z0-9\s]", " ", text.lower())
        haystack = slugify(low_nopunct)

        # 1. full name
        full_hits = [
            k for k, rec in self.players.items()
            if rec["key"] and len(rec["key"]) >= 6 and rec["key"] in haystack
        ]
        if full_hits:
            hit = self._narrow(full_hits, team_hint)
            if hit:
                return hit

        words = [w for w in low_nopunct.split() if w not in _SUFFIXES]

        # 2. initial + surname  ("J. Love", "J Love")
        for i, w in enumerate(words[:-1]):
            if len(w) == 1 or (len(w) == 2 and w.endswith(".")):
                keys = self._by_initial_last.get(f"{w[0]}-{words[i + 1]}", [])
                if keys:
                    hit = self._narrow(keys, team_hint)
                    if hit:
                        return hit

        # 3. unique surname
        for w in words:
            if len(w) < 4:
                continue
            keys = self._by_last.get(w, [])
            if keys:
                hit = self._narrow(keys, team_hint)
                if hit:
                    return hit
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"count": len(self.players), "players": self.players}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlayerIndex":
        idx = cls()
        for key, rec in (data or {}).get("players", {}).items():
            idx.players[key] = {
                "key": rec.get("key", key.split(":", 1)[-1]),
                "name": rec.get("name", ""),
                "team": rec.get("team", key.split(":", 1)[0]),
                "positions": list(rec.get("positions", [])),
                "urls": dict(rec.get("urls", {})),
                "ids": dict(rec.get("ids", {})),
                "sources": list(rec.get("sources", [])),
            }
            idx._index(key, rec.get("name", ""))
        return idx


def _name_parts(name: str) -> List[str]:
    text = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower())
    parts = [p for p in text.split() if p and p not in _SUFFIXES]
    return parts


def match_players(text: str, index: PlayerIndex, *, team_hint: str = "") -> List[Dict[str, Any]]:
    """Best-match player for free text (see PlayerIndex.find_in_text)."""

    rec = index.find_in_text(text, team_hint=team_hint)
    return [rec] if rec else []


def team_codes_in_text(text: str, known: Iterable[str]) -> List[str]:
    """Club codes/nicknames present in free text (e.g. 'Seahawks ruled him out')."""

    from .nfl_com import NFL_TEAMS

    low = (text or "").lower()
    found: List[str] = []
    for code, name in NFL_TEAMS.items():
        if code not in known:
            continue
        nick = name.rsplit(" ", 1)[-1].lower()
        city = name.rsplit(" ", 1)[0].lower()
        if re.search(rf"\b{re.escape(nick)}\b", low) or re.search(rf"\b{re.escape(city)}\b", low):
            if code not in found:
                found.append(code)
    return found
