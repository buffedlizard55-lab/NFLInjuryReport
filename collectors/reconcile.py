"""Cross-source reconciliation: official report + corroborating sources -> alerts.

Precedence (highest first)
--------------------------
1. nfl.com  -- the league's own Game Status Report. This is the only source whose
   designation is *officially binding*, so it wins every conflict.
2. espn     -- adds per-update timestamps, injury detail and reporter attribution.
3. rotowire -- lineup/inactive corroboration only (its injury table is paywalled).

Anything the sources disagree about is emitted as an Irregularity with deep links
for manual review rather than being silently resolved. That includes player/club
mismatches, which is how upstream data errors get surfaced instead of hidden.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    OFFICIAL_NFL_INJURY_URL,
    Alert,
    Irregularity,
    PlayerInjury,
    is_escalation,
    short_id,
    slugify,
)
from .nfl_com import NFL_TEAMS

#: How long a snapshot may be older than the previous one before we flag staleness.
STALE_AFTER_HOURS = 30.0

#: In-game events are only turned into alerts when the source reported them within
#: this window. This is what stops the first run after a deploy from dumping a
#: week of history into the alert feed, and it is also why the alert log matters:
#: an alert raised at 01:42Z is still visible at 09:00Z.
IN_GAME_ALERT_WINDOW_HOURS = 12.0

#: The append-only alert log keeps this many hours (and at most ALERT_LOG_MAX rows)
#: so a user who opens the page after the game can still see what happened during it.
ALERT_LOG_RETENTION_HOURS = 72.0
ALERT_LOG_MAX = 400


def _in_game_alert(event: Dict[str, Any], *, now: str) -> Optional[Alert]:
    """Build an alert from one in-game event, or None when it is out of window."""

    reported_at = event.get("reported_at") or ""
    age_h = None
    a, b = _parse_ts(reported_at), _parse_ts(now)
    if a and b:
        age_h = (b - a).total_seconds() / 3600.0
    if age_h is not None and age_h > IN_GAME_ALERT_WINDOW_HOURS:
        return None

    status_phrase = {
        "OUT_FOR_GAME": "out for the rest of the game",
        "RETURN_QUESTIONABLE": "questionable to return",
        "EVALUATED": "being evaluated (locker room / X-rays)",
        "RETURNED": "returned to the game",
        "INJURY_REPORTED": "reported injured",
    }.get(event.get("in_game_status", ""), event.get("in_game_status", ""))

    sources = event.get("sources") or []
    verified = [s for s in sources if s.get("verified")]
    lead = ""
    if verified:
        lead = (f"Verified source: {verified[0].get('author') or verified[0].get('source')} "
                f"({verified[0].get('verification_detail') or 'platform-verified'})"
                f"{', posted ' + verified[0]['posted_at'] if verified[0].get('posted_at') else ''}. ")
    elif sources:
        src = sources[0]
        lead = (f"Source: {src.get('author') or src.get('source')} "
                f"({src.get('platform')})"
                f"{', posted ' + src['posted_at'] if src.get('posted_at') else ''}. ")
    evidence = " ".join(event.get("evidence") or [])[:400]
    detail = (
        f"{event.get('player') or 'Unidentified player'}"
        + (f" ({event.get('team')}" + (f" {event.get('position')}" if event.get('position') else "")
           + ")" if event.get("team") else "")
        + f" — {status_phrase}"
        + (f" ({event.get('injury')})" if event.get("injury") else "")
        + f" (in-game status {event.get('in_game_status')}). "
        + lead
        + (f"“{evidence}”" if evidence else "")
    )
    return Alert(
        ts=reported_at or now,
        kind="in-game",
        severity=event.get("severity") or "medium",
        player=event.get("player") or "",
        player_key=event.get("player_key") or "",
        team=event.get("team") or "",
        position=event.get("position") or "",
        injury=event.get("injury") or "",
        from_status="",
        to_status=event.get("in_game_status") or "",
        detail=detail.strip(),
        sources=[{"source": s.get("source") or s.get("platform") or "",
                  "url": s.get("url") or "",
                  "observed_at": s.get("posted_at") or ""} for s in sources],
        alert_id=f"ingame:{event.get('event_key') or ''}",
        event_key=event.get("event_key") or "",
        first_seen_at=event.get("first_seen_at") or now,
        reported_at=reported_at,
        detection_latency_seconds=event.get("detection_latency_seconds"),
        source_verified=bool(event.get("source_verified")),
        in_game=True,
        evidence=list(event.get("evidence") or []),
    )


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()):
        v += "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _minutes_between(a: str, b: str) -> Optional[int]:
    da, db = _parse_ts(a), _parse_ts(b)
    if not da or not db:
        return None
    return int((db - da).total_seconds() // 60)


class CanonicalPlayer:
    """One player's reconciled injury state for the current snapshot.

    Deliberately a plain object rather than __slots__: `_merge_into` records a
    per-field `_auth_*` authority marker so a lower-authority source can never
    overwrite the official designation, and that needs a real __dict__.
    """

    def __init__(self, key: str, name: str, team: str) -> None:
        self.key = key
        self.name = name
        self.team = team
        self.position = ""
        self.injury = ""
        self.game_status = "UNKNOWN"
        #: "published" | "inferred" | "" — see models.PlayerInjury
        self.designation_source = ""
        self.practice_status = "NONE"
        self.est_return = ""
        self.comment = ""
        self.attribution = ""
        self.outlet = ""
        self.observed_at = ""
        self.sources: List[Dict[str, str]] = []
        self.url = ""
        self.headshot = ""
        self.rotowire_id = ""
        self.espn_athlete_id = ""
        self.nfl_slug = ""
        self.discrepancies: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "team": self.team,
            "position": self.position, "injury": self.injury,
            "game_status": self.game_status,
            "designation_source": self.designation_source,
            "practice_status": self.practice_status,
            "est_return": self.est_return, "comment": self.comment,
            "attribution": self.attribution, "outlet": self.outlet,
            "observed_at": self.observed_at, "sources": self.sources,
            "url": self.url, "headshot": self.headshot,
            "rotowire_id": self.rotowire_id, "espn_athlete_id": self.espn_athlete_id,
            "nfl_slug": self.nfl_slug, "discrepancies": self.discrepancies,
        }


def _merge_into(target: CanonicalPlayer, rec: PlayerInjury, *, authority: int) -> None:
    """Fold a source record into the canonical player. Lower authority wins."""

    target.sources.append({"source": rec.source, "url": rec.url, "observed_at": rec.observed_at})
    if rec.source_ids:
        target.rotowire_id = target.rotowire_id or rec.source_ids.get("rotowire_id", "")
        target.espn_athlete_id = target.espn_athlete_id or rec.source_ids.get("espn_athlete_id", "")
        target.nfl_slug = target.nfl_slug or rec.source_ids.get("nfl_slug", "")
    if rec.raw.get("headshot") and not target.headshot:
        target.headshot = rec.raw["headshot"]
    if rec.raw.get("outlet") and not target.outlet:
        target.outlet = rec.raw["outlet"]

    def prefer(field: str, value: str) -> None:
        current = getattr(target, field)
        if not current or authority < getattr(target, f"_auth_{field}", 99):
            setattr(target, field, value)
            setattr(target, f"_auth_{field}", authority)

    if rec.position:
        prefer("position", rec.position)
    if rec.injury:
        prefer("injury", rec.injury)
    if rec.comment:
        prefer("comment", rec.comment)
    if rec.attribution:
        prefer("attribution", rec.attribution)
    if rec.est_return:
        prefer("est_return", rec.est_return)
    if rec.url and authority <= 1 and not target.url:
        target.url = rec.url

    # Status: official source always wins over ESPN/RotoWire.
    if rec.game_status and rec.game_status != "UNKNOWN":
        if not target.game_status or target.game_status == "UNKNOWN" or authority < getattr(
            target, "_auth_status", 99
        ):
            target.game_status = rec.game_status
            target._auth_status = authority  # type: ignore[attr-defined]
            target.designation_source = rec.designation_source
    if rec.practice_status and rec.practice_status != "NONE":
        if target.practice_status == "NONE" or authority < getattr(target, "_auth_practice", 99):
            target.practice_status = rec.practice_status
            target._auth_practice = authority  # type: ignore[attr-defined]

    ts = _parse_ts(rec.observed_at)
    cur = _parse_ts(target.observed_at)
    if ts and (cur is None or ts > cur):
        target.observed_at = rec.observed_at


AUTHORITY = {"nfl.com": 0, "espn": 1, "rotowire": 2}


def reconcile(
    official: Optional[Dict[str, Any]],
    espn: Optional[Dict[str, Any]],
    rotowire: Optional[Dict[str, Any]] = None,
    *,
    previous: Optional[Dict[str, Any]] = None,
    now: str = "",
    game_events: Optional[List[Dict[str, Any]]] = None,
    alert_log: Optional[List[Dict[str, Any]]] = None,
    live_games: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the canonical report, the alert feed and the irregularity list.

    `game_events` are the in-game events derived by collectors/ingame.py (verified
    insider posts, wires, first-party ESPN comments). They are alerts on their own
    axis: a player who is "out for the rest of the game" is NOT a roster-status
    change, and the roster report may not catch up to it for days -- on 2026-09-17
    that is exactly what happened with DJ Moore (out of the game at ~01:37Z, ESPN
    row still saying "Questionable" the next morning).

    `alert_log` is the previous run's log. Alerts are merged into it by stable
    `alert_id`, so an alert raised mid-game is still visible hours later instead of
    disappearing with the next snapshot.
    """

    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    canonical: Dict[str, CanonicalPlayer] = {}
    irregularities: List[Irregularity] = []

    def bucket(rec: PlayerInjury) -> str:
        return f"{rec.team}:{rec.player_key}"

    # ---- pass 1: fold every source in, highest authority first -------------
    for source, payload in (
        ("nfl.com", official),
        ("espn", espn),
        ("rotowire", rotowire),
    ):
        if not payload:
            continue
        for rec in payload.get("injuries", []):
            if not rec.team:
                # RotoWire rows often have no unambiguous club marker. Try to
                # attach them to an existing canonical player by name.
                attached = False
                for key, cp in canonical.items():
                    if key.endswith(":" + rec.player_key):
                        _merge_into(cp, rec, authority=AUTHORITY[source])
                        attached = True
                        break
                if not attached:
                    irregularities.append(
                        Irregularity(
                            code="UNATTRIBUTED_TEAM",
                            severity="low",
                            title=f"{source} row without a resolvable club",
                            detail=(
                                f"'{rec.player}' was published by {source} without a club code "
                                "the pipeline could resolve, and no matching player exists in "
                                "the official report yet. Row kept but unassigned to a team."
                            ),
                            evidence=[{"label": "Source row", "url": rec.url}],
                        )
                    )
                continue
            key = bucket(rec)
            cp = canonical.get(key)
            if cp is None:
                cp = CanonicalPlayer(rec.player_key, rec.player, rec.team)
                canonical[key] = cp
            _merge_into(cp, rec, authority=AUTHORITY[source])

    # ---- pass 2: cross-source disagreement --------------------------------
    per_source_status: Dict[str, Dict[str, str]] = {}
    per_source_published: Dict[str, Dict[str, bool]] = {}
    per_source_team: Dict[str, Dict[str, List[str]]] = {}
    for source, payload in (("nfl.com", official), ("espn", espn), ("rotowire", rotowire)):
        if not payload:
            continue
        per_source_status[source] = {}
        per_source_published[source] = {}
        per_source_team[source] = {}
        for rec in payload.get("injuries", []):
            if not rec.player_key:
                continue
            per_source_status[source][rec.player_key] = rec.game_status
            per_source_published[source][rec.player_key] = (
                getattr(rec, "designation_source", "") == "published"
            )
            per_source_team[source].setdefault(rec.player_key, [])
            if rec.team and rec.team not in per_source_team[source][rec.player_key]:
                per_source_team[source][rec.player_key].append(rec.team)

    # Player listed under DIFFERENT clubs by different sources -> high severity.
    all_players = set()
    for d in per_source_team.values():
        all_players.update(d.keys())
    for pkey in sorted(all_players):
        teams_by_source = {
            src: d[pkey] for src, d in per_source_team.items() if pkey in d and d[pkey]
        }
        flat = {t for lst in teams_by_source.values() for t in lst}
        if len(flat) > 1:
            name = canonical.get(f"{sorted(flat)[0]}:{pkey}")
            display = name.name if name else pkey
            evidence = []
            for src, teams in teams_by_source.items():
                url = OFFICIAL_NFL_INJURY_URL if src == "nfl.com" else (
                    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
                    if src == "espn" else "https://www.rotowire.com/football/lineups.php"
                )
                evidence.append({"label": f"{src}: {', '.join(teams)}", "url": url})
            irregularities.append(
                Irregularity(
                    code="PLAYER_TEAM_CONFLICT",
                    severity="high",
                    title=f"'{display}' is listed under conflicting clubs",
                    detail=(
                        f"Sources disagree about which club '{display}' belongs to "
                        f"({', '.join(sorted(flat))}). This is usually an upstream data error "
                        "(wrong club attached to a player record) and must be reviewed before "
                        "the row is trusted. No club was assigned by guesswork."
                    ),
                    evidence=evidence,
                )
            )
            for code in flat:
                cp = canonical.get(f"{code}:{pkey}")
                if cp is not None:
                    cp.discrepancies.append("PLAYER_TEAM_CONFLICT")

    # Same player, different designation between official and ESPN.
    off = per_source_status.get("nfl.com", {})
    esp = per_source_status.get("espn", {})
    inferred_vs_reported = 0
    for pkey in sorted(set(off) & set(esp)):
        a, b = off[pkey], esp[pkey]
        if a in ("UNKNOWN", "") or b in ("UNKNOWN", ""):
            continue
        # Only a *published* official designation can genuinely contradict ESPN.
        # nfl.com prints a blank Game Status for a player who practised without a
        # designation; we render that as ACTIVE, but it is our inference, and
        # ESPN's status field is news-derived rather than the filed designation.
        # Comparing the two produced 54 spurious "conflicts" on the 2026-09-10
        # run, so inferred rows are counted separately instead of flagged.
        if not per_source_published.get("nfl.com", {}).get(pkey, False):
            if a != b:
                inferred_vs_reported += 1
            continue
        if a != b:
            cp = None
            for k, v in canonical.items():
                if k.endswith(":" + pkey):
                    cp = v
                    break
            irregularities.append(
                Irregularity(
                    code="STATUS_CONFLICT",
                    severity="medium",
                    title=f"Designation conflict for {cp.name if cp else pkey}",
                    detail=(
                        f"nfl.com (official) says {a}; ESPN says {b}. The OFFICIAL nfl.com "
                        "designation is the one published on this site, because only the "
                        "league's Game Status Report is binding. The ESPN value is retained on "
                        "the record for review."
                    ),
                    evidence=[
                        {"label": f"Official ({a})", "url": (cp.url if cp else OFFICIAL_NFL_INJURY_URL)},
                        {"label": f"ESPN ({b})",
                         "url": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"},
                    ],
                )
            )
            if cp is not None:
                cp.discrepancies.append("STATUS_CONFLICT")

    if inferred_vs_reported:
        irregularities.append(
            Irregularity(
                code="INFERRED_VS_REPORTED",
                severity="low",
                title=(
                    f"{inferred_vs_reported} player(s) have no official designation but do "
                    "have an ESPN status"
                ),
                detail=(
                    "nfl.com printed a blank Game Status for these players (they practised "
                    "without a designation), so this site renders them as ACTIVE and marks the "
                    "designation as inferred. ESPN separately carries a news-derived status. "
                    "This is a semantic difference between the two sources, NOT a data error, "
                    "so it is reported once in aggregate rather than once per player."
                ),
                evidence=[
                    {"label": "Official NFL injury report", "url": OFFICIAL_NFL_INJURY_URL},
                ],
            )
        )

    # ---- pass 3: alerts vs previous snapshot ------------------------------
    alerts: List[Alert] = []
    prev_index: Dict[str, Dict[str, Any]] = {}
    if previous:
        for row in previous.get("players", []):
            prev_index[f"{row.get('team')}:{row.get('key')}"] = row

    for key, cp in sorted(canonical.items()):
        old = prev_index.get(key)
        if old is None:
            if cp.game_status in ("OUT", "IR", "PUP", "NFI", "SUSPENDED", "DOUBTFUL"):
                sev = "critical" if cp.game_status in ("OUT", "IR") else "high"
                alerts.append(
                    Alert(
                        ts=cp.observed_at or now, kind="new-injury", severity=sev,
                        player=cp.name, player_key=cp.key, team=cp.team,
                        position=cp.position, injury=cp.injury,
                        from_status="", to_status=cp.game_status,
                        detail=_describe_new(cp), sources=cp.sources,
                        alert_id=short_id("new-injury", cp.team, cp.key, cp.game_status,
                                          (cp.observed_at or now)[:13]),
                        first_seen_at=now, reported_at=cp.observed_at or "",
                    )
                )
            continue
        old_status = old.get("game_status", "UNKNOWN")
        if old_status == cp.game_status:
            continue
        if cp.game_status == "ACTIVE":
            sev, kind = "high", "cleared"
        elif is_escalation(old_status, cp.game_status):
            sev, kind = ("critical" if cp.game_status in ("OUT", "IR") else "high"), "status-change"
        else:
            sev, kind = "medium", "status-change"
        alerts.append(
            Alert(
                ts=cp.observed_at or now, kind=kind, severity=sev,
                player=cp.name, player_key=cp.key, team=cp.team,
                position=cp.position, injury=cp.injury,
                from_status=old_status, to_status=cp.game_status,
                detail=(
                    f"{cp.name} ({cp.team} {cp.position}) moved {old_status} -> "
                    f"{cp.game_status}"
                    + (f" — {cp.injury}" if cp.injury else "")
                    + (f". {cp.comment}" if cp.comment else "")
                ),
                sources=cp.sources,
                alert_id=short_id(kind, cp.team, cp.key, old_status, cp.game_status,
                                  (cp.observed_at or now)[:13]),
                first_seen_at=now, reported_at=cp.observed_at or "",
            )
        )

    for key, old in prev_index.items():
        if key in canonical:
            continue
        alerts.append(
            Alert(
                ts=now, kind="removed", severity="low",
                player=old.get("name", ""), player_key=old.get("key", ""),
                team=old.get("team", ""), position=old.get("position", ""),
                injury=old.get("injury", ""), from_status=old.get("game_status", ""),
                to_status="REMOVED_FROM_REPORT",
                detail=(
                    f"{old.get('name')} ({old.get('team')}) is no longer on the official "
                    "injury report."
                ),
                sources=[],
                alert_id=short_id("removed", old.get("team", ""), old.get("key", ""),
                                  old.get("game_status", ""), now[:10]),
                first_seen_at=now,
            )
        )

    # ---- pass 3b: in-game events, and the append-only alert log -----------
    in_game_alerts: List[Alert] = []
    for event in game_events or []:
        alert = _in_game_alert(event, now=now)
        if alert is not None:
            in_game_alerts.append(alert)
    alerts.extend(in_game_alerts)

    alerts.sort(key=lambda a: a.ts or "", reverse=True)

    # The log is what makes an alert survivable: `alerts` above is only THIS run's
    # view, and on 2026-09-18 the user opened the page after the game and found
    # nothing, because the DJ Moore alert had already rolled out of the diff.
    merged_log: Dict[str, Dict[str, Any]] = {}
    for row in alert_log or []:
        rid = row.get("alert_id") or short_id("legacy", row.get("ts", ""), row.get("player", ""),
                                              row.get("to_status", ""))
        merged_log[rid] = dict(row, alert_id=rid)
    for alert in alerts:
        row = alert.to_dict()
        rid = row.get("alert_id") or short_id("alert", row.get("ts", ""), row.get("player", ""))
        row["alert_id"] = rid
        existing = merged_log.get(rid)
        if existing is None:
            merged_log[rid] = row
            continue
        # Same event seen again: keep the ORIGINAL detection time (that is the
        # honest latency) but pick up any new evidence/sources.
        merged_sources = list(existing.get("sources") or [])
        for src in row.get("sources") or []:
            if src not in merged_sources:
                merged_sources.append(src)
        existing["sources"] = merged_sources
        if row.get("detection_latency_seconds") is not None:
            existing["detection_latency_seconds"] = row["detection_latency_seconds"]
        for field_name in ("evidence", "detail"):
            if row.get(field_name):
                existing[field_name] = row[field_name]

    cutoff = _parse_ts(now)
    if cutoff is not None:
        from datetime import timedelta

        oldest = cutoff - timedelta(hours=ALERT_LOG_RETENTION_HOURS)
        merged_log = {
            rid: row for rid, row in merged_log.items()
            if (_parse_ts(row.get("ts", "")) or cutoff) >= oldest
        }
    alert_log_out = sorted(merged_log.values(),
                           key=lambda r: (r.get("ts") or "", r.get("alert_id") or ""),
                           reverse=True)[:ALERT_LOG_MAX]

    # ---- pass 4: freshness / coverage flags -------------------------------
    official_ts = (official or {}).get("fetched_at", "")
    if official is None:
        irregularities.append(
            Irregularity(
                code="OFFICIAL_SOURCE_MISSING",
                severity="critical",
                title="Official nfl.com injury report was not collected on this run",
                detail=(
                    "Without the official report there is no authoritative designation, so this "
                    "snapshot must be treated as UNVERIFIED. The site shows the last known good "
                    "snapshot with its timestamp instead of pretending to be current."
                ),
                evidence=[
                    {"label": "Official NFL injury report", "url": OFFICIAL_NFL_INJURY_URL},
                ],
            )
        )
    elif previous and previous.get("generated_at"):
        age_h = (_minutes_between(previous["generated_at"], now) or 0) / 60.0
        if age_h > STALE_AFTER_HOURS:
            irregularities.append(
                Irregularity(
                    code="STALE_SNAPSHOT",
                    severity="medium",
                    title=f"Snapshot is {age_h:.1f} hours newer than the last published one",
                    detail=(
                        "The gap between published snapshots exceeded "
                        f"{STALE_AFTER_HOURS:.0f}h, which suggests the collector did not run or "
                        "was rate-limited. Latency claims on the site should be read against "
                        "this."
                    ),
                    evidence=[{"label": "Official NFL injury report", "url": OFFICIAL_NFL_INJURY_URL}],
                )
            )

    teams_with_data = {cp.team for cp in canonical.values()}
    missing_teams = sorted(set(NFL_TEAMS) - teams_with_data)
    if official is not None and official.get("injuries") and missing_teams:
        irregularities.append(
            Irregularity(
                code="TEAMS_WITHOUT_ROWS",
                severity="low",
                title=f"{len(missing_teams)} club(s) have no rows in the official report",
                detail=(
                    "Clubs with no entries in this snapshot: "
                    + ", ".join(missing_teams)
                    + ". This is normal when a club has filed no injuries for the upcoming "
                    "game(s) in the current report window, but it is surfaced so an unexpected "
                    "gap is visible rather than silent."
                ),
                evidence=[{"label": "Official NFL injury report", "url": OFFICIAL_NFL_INJURY_URL}],
            )
        )

    # ---- assemble ---------------------------------------------------------
    players = sorted(
        (cp.to_dict() for cp in canonical.values()),
        key=lambda p: (p["team"], p["name"]),
    )
    teams = []
    for code in sorted(NFL_TEAMS):
        rows = [p for p in players if p["team"] == code]
        teams.append(
            {
                "code": code,
                "name": NFL_TEAMS[code],
                "count": len(rows),
                "out": sum(1 for p in rows if p["game_status"] in ("OUT", "IR")),
                "questionable": sum(1 for p in rows if p["game_status"] == "QUESTIONABLE"),
                "doubtful": sum(1 for p in rows if p["game_status"] == "DOUBTFUL"),
                "players": rows,
            }
        )

    for payload in (official, espn, rotowire):
        if payload:
            irregularities.extend(payload.get("irregularities", []))

    return {
        "generated_at": now,
        "season": (official or {}).get("season") or (espn or {}).get("season"),
        "week": (official or {}).get("week"),
        "official_fetched_at": official_ts,
        "espn_source_timestamp": (espn or {}).get("source_timestamp", ""),
        "counts": {
            "players": len(players),
            "teams": sum(1 for t in teams if t["count"] > 0),
            "out": sum(1 for p in players if p["game_status"] in ("OUT", "IR")),
            "questionable": sum(1 for p in players if p["game_status"] == "QUESTIONABLE"),
            "doubtful": sum(1 for p in players if p["game_status"] == "DOUBTFUL"),
            "alerts": len(alerts),
            "in_game_events": len(game_events or []),
            "in_game_alerts": len(in_game_alerts),
            "irregularities": len(irregularities),
        },
        "teams": teams,
        "players": players,
        "alerts": [a.to_dict() for a in alerts],
        "alert_log": alert_log_out,
        "game_events": list(game_events or []),
        "live_games": list(live_games or []),
        "irregularities": [i.to_dict() for i in irregularities],
    }


def _describe_new(cp: CanonicalPlayer) -> str:
    base = f"{cp.name} ({cp.team} {cp.position}) is {cp.game_status}"
    if cp.injury:
        base += f" ({cp.injury})"
    if cp.comment:
        base += f". {cp.comment}"
    return base
