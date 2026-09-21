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
        # Full-name matching runs once per collected post. Cache the compiled
        # alternation for each club constraint so a large live-game snapshot does
        # not recompile hundreds of regular expressions for every headline.
        self._full_name_cache: Dict[str, Tuple[Any, List[str]]] = {}

    # -- build --------------------------------------------------------------
    def add(self, name: str, team: str, position: str = "", url: str = "",
            source: str = "", ids: Optional[Dict[str, str]] = None,
            seen_at: str = "") -> None:
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
                "last_seen": seen_at,
            }
            self.players[key] = rec
            self._index(key, name)
            self._full_name_cache.clear()
        if seen_at:
            # `last_seen` is the most recent run in which a live source still
            # asserted this (club, player). Pruning (see prune) uses it.
            rec["last_seen"] = seen_at
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

    def add_many(self, injuries: Iterable[Any], *, seen_at: str = "") -> None:
        for rec in injuries:
            self.add(
                rec.player, rec.team, rec.position, rec.url, rec.source,
                getattr(rec, "source_ids", None), seen_at=seen_at,
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

    def _reindex(self) -> None:
        """Rebuild the surname/initial indexes from the surviving records."""

        self._by_last = {}
        self._by_initial_last = {}
        for key, rec in self.players.items():
            self._index(key, rec.get("name") or "")

    # -- pruning --------------------------------------------------------------
    def prune(
        self,
        *,
        now: str,
        official_pairs: Iterable[Tuple[str, str]] = (),
        asserted_pairs: Iterable[Tuple[str, str]] = (),
        stale_days: float = 14.0,
    ) -> List[str]:
        """Drop index records that current sources no longer support.

        The index used to be append-only. That had two observed consequences
        (both verified against archived snapshots on 2026-09-21):

        * A one-off official-report misattribution on 2026-09-16 (archive
          `report-2131.json` lists Aaron Banks and Zach Bako-Bewele under BOTH
          HOU and GB, Kyler Murray under BOTH PHI and MIN, Brock Bowers under
          BOTH CAR and LV; every later snapshot lists them under one club only)
          left permanent duplicate records that made the in-game event builder
          attribute a Green Bay injury headline to a Houston player.
        * A player traded or released keeps his old (club, name) record forever,
          which both mis-attributes events and poisons ambiguity checks.

        Rules, in order (a record is dropped when EITHER fires):

        1. CONTRADICTION — the official report names the player under club T and
           this record attaches him to T' != T, and NO current source (official,
           ESPN, RotoWire, or a game-day roster) still asserts (T', name). A
           genuine cross-source conflict (Justin Jefferson: nfl.com CLE, ESPN
           MIN) survives, because the asserting source keeps its record and the
           conflict is already flagged by reconcile.
        2. STALE — `last_seen` (set by add/add_many on every run in which a live
           source asserts the record) is older than `stale_days`. Records
           predating the last_seen field (no value) are given today's date and
           survive this run; they become prunable only after a full missed
           window.

        `official_pairs` is ((club, player_key), ...) from the CURRENT official
        report; `asserted_pairs` is the union over all current sources. When
        the official report is missing, `official_pairs` is empty and the
        contradiction rule is skipped entirely — there is no authoritative view
        to contradict.

        Returns the dropped "TEAM:name-slug (reason)" rows, for audit.
        """

        import datetime as _dt

        official_by_key: Dict[str, List[str]] = {}
        for team, key in official_pairs:
            if team and key:
                official_by_key.setdefault(key, [])
                if team not in official_by_key[key]:
                    official_by_key[key].append(team)
        asserted = {(t, k) for t, k in asserted_pairs if t and k}

        now_dt = None
        try:
            now_dt = _dt.datetime.strptime(now[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            now_dt = None

        dropped: List[str] = []
        for full_key, rec in list(self.players.items()):
            team = rec.get("team") or ""
            key = rec.get("key") or ""
            reason = ""
            if (official_by_key and key in official_by_key
                    and team not in official_by_key[key]
                    and (team, key) not in asserted):
                reason = "official report lists the player under a different club"
            if not reason and now_dt is not None:
                last = rec.get("last_seen") or ""
                if not last:
                    rec["last_seen"] = now  # backfill; prunable next window
                else:
                    try:
                        last_dt = _dt.datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        last_dt = None
                    if last_dt is not None:
                        age_days = (now_dt - last_dt).total_seconds() / 86400.0
                        if age_days > stale_days:
                            reason = f"not asserted by any source for {age_days:.1f} days"
            if reason:
                del self.players[full_key]
                dropped.append(f"{full_key} ({reason})")
        if dropped:
            self._reindex()
            self._full_name_cache.clear()
        return dropped

    # -- query --------------------------------------------------------------
    def _narrow(self, keys: List[str], team_hint: str) -> Optional[Dict[str, Any]]:
        if team_hint:
            # A club named by the source is a constraint, not a preference. The
            # previous ``or keys`` fallback attached "Patriots ... Boston" to
            # CLE:Denzel Boston simply because Boston was the only matching
            # surname in the index. A mismatch is unresolvable and must stay
            # unmatched rather than becoming a fabricated injury report.
            keys = [k for k in keys if k.startswith(f"{team_hint}:")]
        if len(keys) != 1:
            return None  # ambiguous or contradicted -> no match, never a guess
        return self.players[keys[0]]

    def lookup(self, name: str, team: str = "") -> Optional[Dict[str, Any]]:
        return self.players.get(f"{team}:{slugify(name)}")

    def candidates_for(self, name: str) -> List[Dict[str, Any]]:
        slug = slugify(name)
        return [r for k, r in self.players.items() if k.endswith(":" + slug)]

    def full_name_hits(self, text: str, *, team_hint: str = "") -> List[str]:
        """Every roster record whose FULL name appears in ``text``.

        Unlike ``find_in_text`` (best single match), this returns all of them: a
        headline that names two injured players ("Kelce and Pierce both out")
        is about both, and the in-game builder must be able to emit an event
        for each. Matching uses token boundaries, so a roster name cannot be
        found merely because its key is a substring of an outlet or place name.
        When the source names one club, only that club's records are eligible;
        a club hint is a constraint, never a reason to choose a different club.
        """

        if not text:
            return []
        low_nopunct = re.sub(r"[^a-z0-9\s]", " ", text.lower())
        haystack = slugify(low_nopunct)
        cached = self._full_name_cache.get(team_hint)
        if cached is None:
            candidate_keys = [
                key for key, rec in self.players.items()
                if len(rec.get("key") or "") >= 6
                and (not team_hint or rec.get("team") == team_hint)
            ]
            names = sorted(
                {self.players[key]["key"] for key in candidate_keys},
                key=lambda value: (-len(value), value),
            )
            if names:
                pattern = re.compile(
                    r"(?<![a-z0-9])({})(?![a-z0-9])".format(
                        "|".join(re.escape(n) for n in names)
                    )
                )
            else:
                pattern = re.compile(r"(?!x)x")
            cached = (pattern, candidate_keys)
            self._full_name_cache[team_hint] = cached

        pattern, candidate_keys = cached
        matched_names = {match.group(1) for match in pattern.finditer(haystack)}
        return [key for key in candidate_keys if self.players[key]["key"] in matched_names]

    def find_in_text(self, text: str, *, team_hint: str = "") -> Optional[Dict[str, Any]]:
        """Best player match inside free text, or None when ambiguous/absent.

        Preference order: full name, then initial+surname, then unique surname.
        A surname shared by more than one known player is NOT resolved without a
        team hint that narrows it to exactly one.

        When the text contains at least one KNOWN FULL name, the surname step is
        suppressed: the text is naming someone by full name, so a bare-surname
        hit on a *different* player is a misread, not a match. Verified live
        2026-09-21: "Caleb Williams Leaves Bears-Vikings Game With Injury; Tyson
        Bagent Enters" used to fall through to the surname step and return
        NO:jordyn-tyson because the FIRST name "Tyson" is a known surname — an
        injury event about two Bears/Vikings players was attributed to a Saint.
        """

        if not text:
            return None
        low_nopunct = re.sub(r"[^a-z0-9\s]", " ", text.lower())
        haystack = slugify(low_nopunct)

        # 1. full name. Use token boundaries: a player key must be a complete
        # name in the text, not a substring of an outlet, place, or another word.
        full_hits = self.full_name_hits(text)
        if full_hits:
            hit = self._narrow(full_hits, team_hint)
            if hit:
                return hit
            # The text names known players by full name but the match is
            # ambiguous (several full names, or none narrowed by the hint).
            # Returning here — instead of trying surname matches — is what keeps
            # "Tyson Bagent" from resolving to a player surnamed Tyson.
            return None

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
                "last_seen": rec.get("last_seen", ""),
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
