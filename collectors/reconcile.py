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
    OFFICIAL_NFL_POLICY_URL,
    Alert,
    Irregularity,
    PlayerInjury,
    is_escalation,
    slugify,
)
from .nfl_com import NFL_TEAMS

#: How long a snapshot may be older than the previous one before we flag staleness.
STALE_AFTER_HOURS = 30.0


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
            "game_status": self.game_status, "practice_status": self.practice_status,
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
) -> Dict[str, Any]:
    """Build the canonical report, the alert feed and the irregularity list."""

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
    per_source_team: Dict[str, Dict[str, List[str]]] = {}
    for source, payload in (("nfl.com", official), ("espn", espn), ("rotowire", rotowire)):
        if not payload:
            continue
        per_source_status[source] = {}
        per_source_team[source] = {}
        for rec in payload.get("injuries", []):
            if not rec.player_key:
                continue
            per_source_status[source][rec.player_key] = rec.game_status
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
    for pkey in sorted(set(off) & set(esp)):
        a, b = off[pkey], esp[pkey]
        if a in ("UNKNOWN", "") or b in ("UNKNOWN", ""):
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
            )
        )

    alerts.sort(key=lambda a: a.ts or "", reverse=True)

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
                    {"label": "NFL Injury Report Policy", "url": OFFICIAL_NFL_POLICY_URL},
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
            "irregularities": len(irregularities),
        },
        "teams": teams,
        "players": players,
        "alerts": [a.to_dict() for a in alerts],
        "irregularities": [i.to_dict() for i in irregularities],
    }


def _describe_new(cp: CanonicalPlayer) -> str:
    base = f"{cp.name} ({cp.team} {cp.position}) is {cp.game_status}"
    if cp.injury:
        base += f" ({cp.injury})"
    if cp.comment:
        base += f". {cp.comment}"
    return base
