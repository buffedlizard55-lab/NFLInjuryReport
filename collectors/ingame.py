"""In-game injury events: the vocabulary that actually takes a player out of a game.

WHY THIS MODULE EXISTS (post-mortem, 2026-09-18)
------------------------------------------------
On Thursday night 2026-09-17 (Bills 41-31 Lions) two injuries were reported by
sources this project already knew about, and the site showed nothing:

  * DJ Moore (BUF WR), shoulder, ~2026-09-18T01:37Z. Ian Rapoport's
    Bluesky-verified account (rapsheet.bsky.social) posted it verbatim:
    "Bills WR DJ Moore, who landed hard on his shoulder, is questionable to
     return with a shoulder injury and a stinger. He was taken into the locker
     room for X-rays."
    ESPN's injuries feed carried the same thing 35 minutes later (date
    2026-09-18T02:12Z) with the comment "Moore (shoulder) has been ruled out for
    the remainder of Thursday night's game against the Lions." -- and the ESPN
    `status` field for that row still said "Questionable".
  * Keon Coleman (BUF WR), hurt and back on the field at ~01:39-01:47Z, per
    Google News headlines ("Keon Coleman injury update: Bills WR hurt vs. Lions",
    aol.com 01:39Z; "...Bills WR returns after getting hurt vs. Lions",
    Democrat and Chronicle 01:47Z).

Both were missed for two separate reasons, and neither of them was a parsing bug:

  1. The collector did not run. Between 01:00Z and 05:51Z no snapshot was
     published at all (see README "measured uptime": the 10-minute cron actually
     ran every ~3 hours on this repo).
  2. There was no notion of an IN-GAME event. A player who is "ruled out for the
     remainder of the game" is not describable by the league's roster-status
     vocabulary that this project models, and "hurt, returned to the game" is not
     a change in that vocabulary at all. The pipeline only knew how to diff
     OUT/DOUBTFUL/QUESTIONABLE/ACTIVE, so a first-quarter injury that the roster
     report has not caught up to produced nothing to alert on.

So this module adds the missing vocabulary. It is deliberately a *separate axis*
from `models.GAME_STATUS`:

  * `game_status` is the roster/designation axis. Only nfl.com may publish it
    (ESPN's value is news-derived and is marked as such). It is never overwritten
    by anything in this file.
  * `in_game_status` is the availability axis DURING a game, and it is derived
    from what a source actually wrote. Every value carries the sentence it came
    from, so it can be checked by hand.

Nothing here infers an injury that was not written down. `classify_game_event`
returns an empty event ("" / "NONE") when the text only describes play.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .nfl_com import NFL_TEAMS as _KNOWN_TEAMS

# --------------------------------------------------------------- vocabulary ---

#: In-game availability. Order matters: the first matching rule wins, so the most
#: severe/unambiguous statements are tested first ("ruled out" beats "questionable").
IN_GAME_STATUSES = [
    "OUT_FOR_GAME",         # will not return (the event the user needed)
    "RETURNED",             # came back onto the field
    "RETURN_QUESTIONABLE",  # questionable/uncertain to return; being evaluated
    "EVALUATED",            # taken to the locker room / tent / x-rays, no verdict
    "INJURY_REPORTED",      # an injury was reported, availability not stated
    "NONE",
]

#: `event` (alert kind) -> is this something that can take a player out of a game?
GAME_EVENT_KINDS = [
    "ruled-out-for-game",
    "returned-to-game",
    "questionable-to-return",
    "injury-being-evaluated",
    "injury-reported",
]

#: Sentence-level patterns. Each entry is (regex, in_game_status, event_kind).
#: Written against text actually observed on 2026-09-17/18 (quoted in the module
#: docstring) plus the standard phrasing used by every club/insider feed.
_EVENT_RULES = [
    # --- out for the rest of the game ------------------------------------
    (re.compile(r"\bruled\s+out\b[^.]{0,60}?\b(?:remainder|rest)\b", re.I),
     "OUT_FOR_GAME", "ruled-out-for-game"),
    (re.compile(r"\bout\s+for\s+the\s+(?:remainder|rest)\s+of\s+the\s+"
                r"(?:game|half|night|contest)\b", re.I),
     "OUT_FOR_GAME", "ruled-out-for-game"),
    (re.compile(r"\bout\s+for\s+the\s+(?:game|night)\b", re.I),
     "OUT_FOR_GAME", "ruled-out-for-game"),
    # Bare "has been ruled out" is an in-game OUT unless the sentence names a
    # future window ("ruled out for Sunday", "for Week 3"), which is the roster
    # axis and is handled by nfl.com/ESPN, not here.
    (re.compile(
        r"\b(?:has|have|had|was|were|is|are|been)?\s*ruled\s+out\b"
        r"(?!\s+(?:for|of)\s+(?:the\s+)?(?:season|week\s*\d+|year|"
        r"sunday|monday|tuesday|wednesday|thursday|friday|saturday|"
        r"rest\s+of\s+the\s+season|remainder\s+of\s+the\s+season)\b)",
        re.I),
     "OUT_FOR_GAME", "ruled-out-for-game"),
    (re.compile(r"\b(?:will|would|is)\s+not\s+return\b|\bwon'?t\s+return\b"
                r"|\bnot\s+expected\s+to\s+return\b|\bno\s+longer\s+in\s+the\s+game\b", re.I),
     "OUT_FOR_GAME", "ruled-out-for-game"),
    (re.compile(r"\bdone\s+for\s+the\s+(?:game|night)\b", re.I),
     "OUT_FOR_GAME", "ruled-out-for-game"),
    # Game-day inactive: this is the league's own mechanism for "cannot play
    # tonight", so it belongs on the in-game axis and nowhere else.
    (re.compile(r"\b(?:listed|declared|ruled|marked)\s+(?:as\s+)?inactive\b"
                r"|\bis\s+inactive\b|\binactive\s+(?:for|on)\s+(?:tonight|today|"
                r"thursday|friday|saturday|sunday|monday)\b|\bwon'?t\s+play\b"
                r"|\bwill\s+not\s+play\b|\bnot\s+playing\s+(?:tonight|today)\b", re.I),
     "OUT_FOR_GAME", "ruled-out-for-game"),
    # --- returned ---------------------------------------------------------
    (re.compile(r"\breturn(?:s|ed)?\s+(?:to\s+the\s+(?:game|field|lineup|huddle)|"
                r"after|from)\b|\bhas\s+returned\b|\bback\s+in\s+the\s+game\b"
                r"|\bre-?entered\s+the\s+game\b|\breturned\s+to\s+action\b", re.I),
     "RETURNED", "returned-to-game"),
    # --- questionable to return ------------------------------------------
    (re.compile(r"\bquestionable\s+to\s+return\b|\buncertain\s+to\s+return\b"
                r"|\bstatus\s+(?:is\s+)?questionable\b[^.]{0,40}\breturn\b", re.I),
     "RETURN_QUESTIONABLE", "questionable-to-return"),
    (re.compile(r"\b(?:being|under)\s+evaluat(?:ed|ion)\b|\bevaluat(?:ed|ing)\s+for\b"
                r"|\b(?:getting|having)\s+(?:an?\s+)?(?:x-?rays?|mri)\b"
                r"|\bx-?rays?\s+(?:were|are|is|on)\b", re.I),
     "EVALUATED", "injury-being-evaluated"),
    (re.compile(r"\bcarted?\s+off\b|\bcarried\s+off\b|\bhelped\s+off\s+the\s+field\b"
                r"|\btaken\s+(?:in)?to\s+the\s+locker\s+room\b"
                r"|\bwent\s+(?:in)?to\s+the\s+locker\s+room\b"
                r"|\b(?:headed|headed\s+back)\s+to\s+the\s+locker\s+room\b", re.I),
     "EVALUATED", "injury-being-evaluated"),
    # --- an injury was reported, availability unstated --------------------
    (re.compile(r"\b(?:suffer(?:ed|s)?|injured|injur(?:y|ies)|hurt|exits?|exited|"
                r"leaves?|left)\b[^.]{0,60}?\b(?:with|on|during|vs\.?|against|"
                r"in)\b", re.I),
     "INJURY_REPORTED", "injury-reported"),
    (re.compile(r"\binjur(?:y|ies)\s+update\b|\bexits?\s+(?:with|after)\b"
                r"|\b(?:out|doubtful|questionable)\s+(?:with|due\s+to)\b", re.I),
     "INJURY_REPORTED", "injury-reported"),
]

#: Body parts. Same list the social classifier used, kept here so that ingame.py
#: has no dependency on social.py (social.py imports THIS module, not the reverse).
INJURY_WORD_RE = re.compile(
    r"\b(ankle|knee|hamstring|quadriceps|quad|groin|shoulder|concussion|back|foot|"
    r"feet|wrist|hand|hip|calf|elbow|neck|achilles|acl|mcl|lcl|pcl|pec|bicep|rib|ribs|"
    r"toe|toes|thumb|finger|illness|abdomen|oblique|core|head|stinger|shoulder\s+injury)\b",
    re.I,
)

#: Emergency / severity markers worth surfacing even without a status word.
_SEVERE_RE = re.compile(
    r"\b(concussion|protocol|cart(?:ed)?|stretcher|ambulance|hospital|fracture|broken|"
    r"torn|tear|surgery|stinger|neck|head)\b", re.I)

#: Practice-report language. An injury mentioned ONLY in a practice report is a
#: roster item: it does not say whether the player can play tonight, and the
#: in-game list exists to answer exactly that. Practice mentions used to reach
#: the in-game feed as "INJURY_REPORTED" for clubs that were not even playing.
_PRACTICE_RE = re.compile(
    r"\b(practice|practiced|practising|practicing|limited\s+participant|"
    r"full\s+participant|did\s+not\s+practice|dnp|estimated|walk-?through|"
    r"rest\s+day|practice\s+report|practice\s+squad|practising|week\s+\d+\s+"
    r"practice)\b", re.I)

#: Words that only ever appear in a post-game stat recap. Used to REJECT a row as
#: an injury signal: on 2026-09-18 ESPN's injuries feed carried
#: "Coleman brought in all six targets for 63 yards in the Bills' 41-31 win over
#: the Lions on Thursday." for Keon Coleman whose real injury was reported by
#: headline sites 3 hours earlier. A stat line is not an injury update.
_PERFORMANCE_RE = re.compile(
    r"\b(brought\s+in|caught|receptions?|targets?|rushed\s+(?:for|\d)|carries|"
    r"completed\s+\d+|threw\s+for|passing\s+yards|rushing\s+yards|receiving\s+yards|"
    r"logged|tallied|recorded|finished\s+with|scored|touchdowns?|sacks?|tackles?|"
    r"in\s+the\s+\S+\s+\d+-\d+\s+(?:win|loss)|extra-?point|field\s+goal)\b", re.I)


def find_injury_word(text: str) -> str:
    m = INJURY_WORD_RE.search(text or "")
    return m.group(1).lower() if m else ""


def is_performance_blurb(text: str) -> bool:
    """True when the text only recaps play (no injury or availability language).

    A blurb is never treated as an injury report, and never clears an injury:
    "played well" is not evidence that a player is healthy.
    """

    if not text:
        return False
    if INJURY_WORD_RE.search(text):
        return False
    for rx, _status, _kind in _EVENT_RULES:
        if rx.search(text):
            return False
    return bool(_PERFORMANCE_RE.search(text))


def classify_game_event(text: str) -> Dict[str, Any]:
    """Derive the in-game availability a piece of text actually states.

    Returns a dict with:
        status    one of IN_GAME_STATUSES ("NONE" when nothing is stated)
        event     one of GAME_EVENT_KINDS ("" when nothing is stated)
        injury    first body part named, else ""
        severe    a severity word was present (concussion/carted/hospital/...)
        sentences the sentences the decision is based on (evidence, verbatim)
        blurb     the text is a stat recap, not an update
        terms     matched vocabulary, for auditing

    Never invents: an empty result means the text said nothing about availability.
    """

    out: Dict[str, Any] = {
        "status": "NONE", "event": "", "injury": find_injury_word(text),
        "severe": bool(_SEVERE_RE.search(text or "")), "sentences": [],
        "blurb": is_performance_blurb(text), "terms": [],
    }
    if not text:
        return out

    # Split on sentence boundaries so evidence is quotable and so a stat line in
    # sentence 2 cannot be attached to an injury in sentence 1.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    for rx, status, event in _EVENT_RULES:
        for sent in sentences:
            if rx.search(sent):
                if status == "INJURY_REPORTED" and _PRACTICE_RE.search(sent):
                    # No availability word matched and the sentence is about
                    # practice, so all this says is "something happened at some
                    # point in the week". Not an in-game event.
                    continue
                out["status"] = status
                out["event"] = event
                out["sentences"] = [sent]
                out["terms"] = [t for t in _matched_terms(sent)]
                return out

    return out


def _matched_terms(sentence: str) -> List[str]:
    return sorted({m.group(0).lower() for m in INJURY_WORD_RE.finditer(sentence)})


def merge_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Collapse several reports about the same player into the most severe one.

    Severity order is IN_GAME_STATUSES order. A later "returned" beats an earlier
    "questionable to return", because that is newer information; anything else
    only escalates.
    """

    best: Optional[Dict[str, Any]] = None
    for ev in events:
        if not ev or ev.get("status", "NONE") == "NONE":
            continue
        if best is None:
            best = ev
            continue
        old_rank = IN_GAME_STATUSES.index(best["status"]) if best["status"] in IN_GAME_STATUSES else 99
        new_rank = IN_GAME_STATUSES.index(ev["status"]) if ev["status"] in IN_GAME_STATUSES else 99
        # Newer information wins; otherwise the more severe state wins.
        old_ts = best.get("posted_at") or ""
        new_ts = ev.get("posted_at") or ""
        if new_ts > old_ts or (new_ts == old_ts and new_rank < old_rank):
            best = ev
    return best or {"status": "NONE", "event": "", "injury": "", "severe": False,
                    "sentences": [], "blurb": False, "terms": []}


def availability_class(status: str) -> str:
    """Map an in-game status onto the scorecard's availability classes."""

    if status == "OUT_FOR_GAME":
        return "UNAVAILABLE"
    if status in ("RETURN_QUESTIONABLE", "EVALUATED"):
        return "LIMITED"
    if status == "RETURNED":
        return "AVAILABLE"
    return "UNKNOWN"


def event_severity(status: str, *, severe: bool = False) -> str:
    """Alert severity for an in-game event. Deliberately blunt: an in-game
    injury that could remove a player is never lower than 'medium'."""

    if status == "OUT_FOR_GAME":
        return "critical"
    if status in ("RETURN_QUESTIONABLE", "EVALUATED"):
        return "high" if severe else "medium"
    if status == "INJURY_REPORTED":
        return "medium"
    return "low"


# ------------------------------------------------------------------- games -----

def parse_scoreboard(payload: Any) -> Dict[str, Any]:
    """Read ESPN's scoreboard payload into the games + clubs currently playing.

    Shape verified live 2026-09-18 against
    https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=20260917
    (verbatim keys used here: events[].id / .date / .name / .shortName /
    .week.number / .competitions[].competitors[].team.abbreviation /
    .competitions[].status.type.state). Unknown keys are ignored rather than
    guessed, and a malformed payload yields an empty result instead of raising.
    """

    games: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return {"games": [], "live": [], "hot_teams": [], "count": 0}

    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        comps = event.get("competitions") or []
        comp = comps[0] if comps and isinstance(comps[0], dict) else {}
        state, detail, period, clock = "", "", None, ""
        status = comp.get("status") or {}
        stype = status.get("type") or {}
        state = stype.get("state") or ""
        detail = status.get("type", {}).get("detail") or status.get("displayClock") or ""
        period = status.get("period")
        clock = status.get("displayClock") or ""
        teams: List[Dict[str, str]] = []
        for side in comp.get("competitors") or []:
            team = (side or {}).get("team") or {}
            abbr = (team.get("abbreviation") or "").upper()
            if abbr:
                teams.append({
                    "code": abbr,
                    "name": team.get("displayName") or team.get("name") or "",
                    "home_away": (side or {}).get("homeAway") or "",
                    "score": str((side or {}).get("score") or ""),
                })
        games.append({
            "id": str(event.get("id") or ""),
            "name": event.get("name") or "",
            "short_name": event.get("shortName") or "",
            "date": event.get("date") or "",
            "week": ((event.get("week") or {}).get("number")),
            "state": state,          # pre | in | post
            "detail": detail,
            "period": period,
            "clock": clock,
            "teams": teams,
        })

    live = [g for g in games if g["state"] == "in"]
    hot: List[str] = []
    for g in games:
        if g["state"] in ("in", "post"):
            for t in g["teams"]:
                if t["code"] not in hot:
                    hot.append(t["code"])
    return {
        "games": games,
        "live": live,
        "hot_teams": hot,
        "count": len(games),
    }


# ------------------------------------------------------- event construction ---

def _epoch(value: str) -> Optional[float]:
    """ISO8601, a bare date, or an RFC-822 stamp -> epoch seconds.

    RFC-822 matters because that is what Google News RSS returns in its pubDate
    (``Fri, 18 Sep 2026 01:39:00 GMT``). Without this branch those items had no
    usable clock, so their age could not be checked and no detection latency
    could be computed for them.
    """

    import re as _re
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    if not value:
        return None
    v = value.strip()
    if _re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        v += "T00:00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _as_dict(post: Any) -> Dict[str, Any]:
    if isinstance(post, dict):
        return post
    to_dict = getattr(post, "to_dict", None)
    return to_dict() if callable(to_dict) else {}


#: A report older than this cannot become a live in-game event. The window is
#: generous (a full day covers an overnight game and the next morning's
#: follow-ups) but it keeps a month-old headline from being presented as
#: tonight's injury -- which is exactly what a Google News fixture did when the
#: query window was widened during development.
MAX_EVENT_AGE_HOURS = 24.0


def build_game_events(
    *,
    posts: Any = (),
    espn: Optional[Dict[str, Any]] = None,
    player_index: Any = None,
    now: str = "",
    hot_teams: Any = (),
) -> List[Dict[str, Any]]:
    """Turn reported text into per-player in-game events, with evidence attached.

    Sources, in the order they usually arrive:
      1. verified insider posts / news wires (fastest -- Rapoport's DJ Moore post
         was 35 minutes ahead of ESPN's injuries feed on 2026-09-17),
      2. ESPN injuries-feed comments ("...has been ruled out for the remainder of
         Thursday night's game..."),
      3. headline feeds (Google News, RotoWire wire, ESPN news).

    A player is only used when the text can be tied to a roster entry (name match,
    or an explicit club in the payload such as ESPN's team block / News article
    `categories`). Nothing is attributed to a player that the text does not name,
    and a stat recap never becomes an event (see is_performance_blurb).
    """

    buckets: Dict[str, List[Dict[str, Any]]] = {}

    def add(team: str, player: str, player_key: str, position: str,
            text: str, *, source: str, platform: str, author: str, url: str,
            posted_at: str, verified: bool, verification_detail: str,
            source_kind: str = "") -> None:
        ev = classify_game_event(text)
        if ev["status"] == "NONE":
            return
        if not (player or player_key):
            # An event must name a player. "A Bills receiver was carted off" is a
            # real report but it is not attributable, so it must not become an
            # alert about a specific person.
            return
        # Time guard: an event is only "in-game" if the source said it recently.
        # Undated text cannot be placed in time, so it never becomes a live
        # event; stale text is rejected outright.
        if now:
            if not posted_at:
                return
            a, b = _epoch(posted_at), _epoch(now)
            if a is not None and b is not None and (b - a) / 3600.0 > MAX_EVENT_AGE_HOURS:
                return
        key = f"{team or '??'}:{player_key or player.lower()}"
        buckets.setdefault(key, []).append({
            "team": team, "player": player, "player_key": player_key or "",
            "position": position,
            "status": ev["status"], "event": ev["event"], "injury": ev["injury"],
            "severe": ev["severe"], "evidence": ev["sentences"],
            "source": source, "platform": platform, "author": author, "url": url,
            "posted_at": posted_at, "verified": bool(verified),
            "verification_detail": verification_detail,
            "source_kind": source_kind,
        })

    # ---- 1/3: text posts and headlines ------------------------------------
    for raw in posts or []:
        post = _as_dict(raw)
        text = post.get("text") or ""
        if not text:
            continue
        # Club hint from the text ("Bills WR DJ Moore...") or from the payload
        # (ESPN news `categories`). Only a SINGLE unambiguous hint is passed,
        # because the matcher refuses to guess between two clubs.
        hint_codes: List[str] = []
        if post.get("team"):
            hint_codes.append(post["team"])
        payload_teams = (post.get("raw") or {}).get("teams") or []
        for code in payload_teams:
            if code and code not in hint_codes:
                hint_codes.append(code)
        if player_index is not None:
            from .matching import team_codes_in_text

            for code in team_codes_in_text(text, _KNOWN_TEAMS):
                if code not in hint_codes:
                    hint_codes.append(code)
        team_hint = hint_codes[0] if len(hint_codes) == 1 else ""
        match = (player_index.find_in_text(text, team_hint=team_hint)
                 if player_index else None)
        team = ""
        player = ""
        player_key = ""
        position = ""
        if match:
            team = match.get("team") or team_hint
            player = match.get("name") or ""
            player_key = match.get("key") or ""
            positions = match.get("positions") or []
            position = positions[0] if positions else ""
        else:
            # No roster match: only proceed when the payload itself names a club
            # (ESPN news `categories`, club feeds) or a headline names the player
            # ("DJ Moore: Ruled out ..."). A post that merely says "a receiver" is
            # not attached to anyone.
            team = team_hint or (post.get("team") or "") or ""
            headline_hint = ""
            m = re.match(r"^\s*([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3})\s*:", text)
            if m:
                headline_hint = m.group(1).strip()
            hint = ((post.get("raw") or {}).get("player_hint") or "").strip() or headline_hint
            if not team and not hint:
                continue
            player = hint or (post.get("matched_player") or "")
        add(team, player, player_key, position, text,
            source=post.get("author") or post.get("platform") or "",
            platform=post.get("platform") or "",
            author=post.get("author_name") or post.get("author") or "",
            url=post.get("url") or "",
            posted_at=post.get("posted_at") or "",
            verified=bool(post.get("verified")),
            verification_detail=post.get("verification_detail") or "",
            source_kind=post.get("source_kind") or "")

    # ---- 2: ESPN injuries-feed comments (first-party timestamps) ----------
    for rec in (espn or {}).get("injuries", []) or []:
        comment = getattr(rec, "comment", "") or ""
        if not comment:
            continue
        add(getattr(rec, "team", "") or "", getattr(rec, "player", "") or "",
            getattr(rec, "player_key", "") or "", getattr(rec, "position", "") or "",
            comment,
            source="espn",
            platform="espn-injuries",
            author=getattr(rec, "attribution", "") or "ESPN",
            url=getattr(rec, "url", "") or "",
            posted_at=getattr(rec, "observed_at", "") or "",
            verified=False,
            verification_detail="ESPN injuries feed (first-party timestamp)")

    # ---- merge per player, keep the newest/severest, then emit ------------
    hot = set(hot_teams or ())
    events: List[Dict[str, Any]] = []
    for key, reports in buckets.items():
        reports.sort(key=lambda r: (r.get("posted_at") or ""), reverse=False)
        merged = merge_events(reports)
        if not merged or merged.get("status") == "NONE":
            continue
        team = merged.get("team") or ""
        # Two hard requirements, both learned from the first live run (2026-09-18):
        #
        #  1. The event must name a player the roster index knows. The headline
        #     fallback ("Vikings Injury Report: ...", "Saints Thursday Injury
        #     Report") produced pseudo-players that were really article titles;
        #     inventing a person out of a headline is precisely the fabrication
        #     this project forbids.
        #  2. The club must be in the game window right now. A practice report
        #     for a club that is not playing is a roster item, not an in-game
        #     event, and the in-game list is only allowed to answer "can this
        #     player play tonight?".
        if not merged.get("player_key"):
            continue
        if not hot or team not in hot:
            continue
        # Collect every source that agrees with the merged status, so the UI can
        # show both the verified insider post and the first-party feed entry.
        sources = []
        for r in reports:
            # Every source that reported on this player's event is kept, including
            # ones that stated an earlier/lesser status: the merge decision must be
            # auditable from the event itself, and the user should be able to read
            # the original post AND the feed entry that followed it.
            if r["url"] and any(s["url"] == r["url"] for s in sources):
                continue
            sources.append({
                "source": r["source"], "platform": r["platform"], "author": r["author"],
                "url": r["url"], "posted_at": r["posted_at"], "verified": r["verified"],
                "verification_detail": r["verification_detail"],
                "stated_status": r["status"], "source_kind": r.get("source_kind") or "",
            })
        sources.sort(key=lambda s: (s.get("posted_at") or ""))
        reported_at = merged.get("posted_at") or ""
        latency = None
        if reported_at and now:
            a, b = _epoch(reported_at), _epoch(now)
            if a is not None and b is not None and b >= a:
                latency = int(b - a)
        events.append({
            "event_key": f"{key}:{merged['status']}",
            "team": team,
            "player": merged.get("player") or "",
            "player_key": merged.get("player_key") or "",
            "position": merged.get("position") or "",
            "in_game_status": merged["status"],
            "event": merged["event"],
            "injury": merged.get("injury") or "",
            "severity": event_severity(merged["status"], severe=bool(merged.get("severe"))),
            "availability_class": availability_class(merged["status"]),
            "evidence": merged.get("evidence") or [],
            "reported_at": reported_at,          # what the source said
            "first_seen_at": now,                # when THIS pipeline saw it
            "detection_latency_seconds": latency,
            "sources": sources,
            "source_verified": any(s.get("verified") for s in sources),
            # True when the club was playing (or had just played) at the time this
            # snapshot was built -- i.e. this is a game-day event, not a weekly
            # practice report.
            "in_game_window": bool(team and team in hot),
        })

    events.sort(key=lambda e: (e.get("reported_at") or "", e.get("event_key") or ""),
                reverse=True)
    return events
