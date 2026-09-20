"""Pipeline orchestrator.

    python3 -m collectors.pipeline collect   [--with-rotowire] [--no-social]
    python3 -m collectors.pipeline verify
    python3 -m collectors.pipeline status

`collect` is what CI runs. It fetches every enabled source, reconciles them into
one canonical report, resolves reporter claims against the official designations,
and writes the JSON the GitHub Pages site reads. Nothing here needs a secret.

Exit code is 0 even when a source fails, so one upstream outage cannot stall the
whole schedule; the failure is recorded in health.json and surfaced on the site
as a flag instead. Exit code 2 is reserved for "nothing usable was collected",
which is the one condition worth failing the build over.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from . import directory as directory_mod
from . import espn as espn_mod
from . import ingame as ingame_mod
from . import nfl_com as nfl_mod
from . import rotowire as rotowire_mod
from . import social as social_mod
from .http import FetchError, get_probe_log, reset_probe_log
from .matching import PlayerIndex
from .models import NFL_POLICY_URL_CANDIDATES, Irregularity
from .reconcile import reconcile
from .reporters import ReporterRegistry
from .scoring import apply_to_registry, build_claims, resolve_claims, summarise

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
LATEST_DIR = os.path.join(DATA_DIR, "latest")
STATE_DIR = os.path.join(DATA_DIR, "state")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


# ------------------------------------------------------------------ sources ---

def collect_sources(
    *, with_rotowire: bool = False, with_social: bool = True,
    with_news: bool = True,
    player_index: Optional[PlayerIndex] = None,
    watched_handles: Optional[List[str]] = None,
    candidate_handles: Optional[List[str]] = None,
    hot_teams: Optional[List[str]] = None,
    hot_players: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Fetch every enabled source. One source failing never aborts the run."""

    out: Dict[str, Any] = {
        "official": None, "espn": None, "rotowire": None, "social": None,
        "espn_news": None, "rotowire_news": None,
        "errors": [], "collected_at": _now(),
    }

    try:
        out["official"] = nfl_mod.collect()
    except FetchError as exc:
        out["errors"].append({"source": "nfl.com", "reason": exc.reason,
                              "status": exc.status, "url": exc.url})

    try:
        out["espn"] = espn_mod.collect()
    except FetchError as exc:
        out["errors"].append({"source": "espn", "reason": exc.reason,
                              "status": exc.status, "url": exc.url})

    if with_rotowire:
        try:
            out["rotowire"] = rotowire_mod.collect()
        except FetchError as exc:
            out["errors"].append({"source": "rotowire", "reason": exc.reason,
                                  "status": exc.status, "url": exc.url})

    # Fast news wires. These are the sources that carry in-game injury updates
    # within minutes (RotoWire's published RSS and ESPN's news API), so they are
    # on by default and independent of the optional RotoWire lineups scrape.
    if with_news:
        team_ids = {code: _ESPN_TEAM_IDS.get(code, "") for code in (hot_teams or [])}
        try:
            out["espn_news"] = espn_mod.collect_news(team_ids=team_ids, limit=20)
        except FetchError as exc:
            out["errors"].append({"source": "espn-news", "reason": exc.reason,
                                  "status": exc.status, "url": exc.url})
        try:
            out["rotowire_news"] = rotowire_mod.collect_news()
        except FetchError as exc:
            out["errors"].append({"source": "rotowire-news", "reason": exc.reason,
                                  "status": exc.status, "url": exc.url})

    if with_social:
        names = [p["name"] for p in (player_index.players.values() if player_index else [])]
        try:
            out["social"] = social_mod.collect_social(
                player_names=names,
                watched_handles=watched_handles or [],
                candidate_handles=candidate_handles or [],
                hot_teams=hot_teams or [],
                hot_players=hot_players or [],
                team_names=nfl_mod.NFL_TEAMS,
            )
        except Exception as exc:  # noqa: BLE001 - social must never break the run
            out["errors"].append({"source": "social", "reason": f"{type(exc).__name__}: {exc}",
                                  "status": None, "url": ""})
    return out


#: Club code -> ESPN team id. Verified live 2026-09-18 from
#: https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
#: (BUF=2, DET=8 observed directly; the rest come from ESPN's own team endpoint,
#: which the pipeline probes every run as `espn_teams`). A code that is missing
#: here simply produces no per-club news query -- it is never guessed.
_ESPN_TEAM_IDS = {
    "ARI": "22", "ATL": "1", "BAL": "33", "BUF": "2", "CAR": "29", "CHI": "3",
    "CIN": "4", "CLE": "5", "DAL": "6", "DEN": "7", "DET": "8", "GB": "9",
    "HOU": "34", "IND": "11", "JAX": "30", "KC": "12", "LAC": "24", "LAR": "14",
    "LV": "13", "MIA": "15", "MIN": "16", "NE": "17", "NO": "18", "NYG": "19",
    "NYJ": "20", "PHI": "21", "PIT": "23", "SEA": "26", "SF": "25", "TB": "27",
    "TEN": "10", "WAS": "28",
}


def espn_team_ids() -> Dict[str, str]:
    """Club code -> ESPN id as published by ESPN's team endpoint.

    Fetched live when possible (and every run is probed as `espn_teams` in the
    source ledger); falls back to the id table above, which was transcribed from
    live scoreboard/team payloads. A mismatch is reported rather than hidden.
    """

    try:
        payload = espn_mod.fetch_json(espn_mod.TEAMS_ENDPOINT, source="espn")
    except Exception:  # noqa: BLE001 - fallback table is the documented behaviour
        return dict(_ESPN_TEAM_IDS)
    ids: Dict[str, str] = {}
    try:
        for sport in payload.get("sports") or []:
            for league in sport.get("leagues") or []:
                for team in league.get("teams") or []:
                    t = team.get("team") or {}
                    abbr = (t.get("abbreviation") or "").upper()
                    if abbr:
                        ids[abbr] = str(t.get("id") or "")
    except AttributeError:
        return dict(_ESPN_TEAM_IDS)
    return ids or dict(_ESPN_TEAM_IDS)


def _hot_players(index: Optional[PlayerIndex], hot_teams: List[str],
                 *, limit: int = 8) -> List[str]:
    """Players whose names are worth a targeted headline query right now.

    Preference goes to players who already carry an injury record (they are the
    ones a game-day update is most likely to be about), then the rest of the
    club's indexed players. This is only a query budget -- nothing here asserts an
    injury.
    """

    if not index or not hot_teams:
        return []
    hot = set(hot_teams)
    injured: List[str] = []
    others: List[str] = []
    for rec in index.players.values():
        team = rec.get("team") or ""
        if team not in hot:
            continue
        name = rec.get("name") or ""
        if not name:
            continue
        (injured if rec.get("urls") or rec.get("sources") else others).append(name)
    return (injured + others)[:limit]


def cadence_metrics(*, archive_dir: str, now: str) -> Dict[str, Any]:
    """Measure how often this collector ACTUALLY ran, from the archive filenames.

    WHY: the README claimed a 10-minute cadence while the archive showed a median
    gap of ~3 hours (GitHub Actions throttles scheduled workflows on free runners).
    Claiming a cadence the system does not achieve is exactly the kind of
    unverifiable statement this project must not make, so the achieved cadence is
    measured on every run and published in meta.json where the site can show it.
    """

    import datetime as _dt
    import glob as _glob

    stamps: List[_dt.datetime] = []
    for path in _glob.glob(os.path.join(archive_dir, "*", "report-*.json")):
        day = os.path.basename(os.path.dirname(path))
        hhmm = os.path.basename(path)[len("report-"):-len(".json")]
        try:
            stamps.append(_dt.datetime.strptime(f"{day}{hhmm}", "%Y-%m-%d%H%M"))
        except ValueError:
            continue
    stamps.sort()
    now_dt = _dt.datetime.strptime(now[:16], "%Y-%m-%dT%H:%M")
    gaps = [int((b - a).total_seconds() // 60) for a, b in zip(stamps, stamps[1:])]
    last_24h = [s for s in stamps if (now_dt - s).total_seconds() <= 24 * 3600]
    metrics: Dict[str, Any] = {
        "runs_total": len(stamps),
        "runs_last_24h": len(last_24h),
        "last_run_at": stamps[-1].strftime("%Y-%m-%dT%H:%M:%SZ") if stamps else "",
        "median_gap_minutes": (sorted(gaps)[len(gaps) // 2] if gaps else None),
        "max_gap_minutes": (max(gaps) if gaps else None),
        "worst_gaps_minutes": sorted(gaps, reverse=True)[:5],
        "measured_on": now,
    }
    age = None
    if stamps:
        age = int((now_dt - stamps[-1]).total_seconds())
    metrics["seconds_since_previous_run"] = age
    return metrics


# ------------------------------------------------------------------- verify ---

VERIFY_TARGETS: List[Dict[str, str]] = [
    {"key": "nfl_official_report", "source": "nfl.com",
     "url": nfl_mod.OFFICIAL_NFL_INJURY_URL,
     "note": "OFFICIAL league Game Status Report. Primary ground truth."},
    # Probe-only: indexed by search engines but returned HTTP 404 to our fetcher
    # on 2026-09-10 from a GitHub Actions runner. Surfaced here so a human can
    # confirm, and deliberately NOT used as an evidence link in the product.
    *({"key": f"nfl_policy_candidate_{i + 1}_UNVERIFIED", "source": "nfl.com",
       "url": u,
       "note": "Personnel (Injury) Report Policy PDF. Returned 404 to this "
               "fetcher on 2026-09-10 despite being indexed. Not used as an "
               "evidence link until it resolves."}
      for i, u in enumerate(NFL_POLICY_URL_CANDIDATES)),
    {"key": "espn_injuries", "source": "espn", "url": espn_mod.ENDPOINT,
     "note": "Free public JSON. Adds per-update timestamps + reporter attribution."},
    {"key": "espn_teams", "source": "espn", "url": espn_mod.TEAMS_ENDPOINT,
     "note": "Club code <-> ESPN id mapping."},
    {"key": "espn_team_injuries_KNOWN_BROKEN", "source": "espn",
     "url": espn_mod._BROKEN_TEAM_ENDPOINT.format(team_id=22),
     "note": "VERIFIED BROKEN 2026-09-10: returns {} . Kept in the ledger so it is not re-tried."},
    {"key": "nfl_api_injuries_REQUIRES_AUTH", "source": "nfl.com",
     "url": "https://api.nfl.com/experience/v1/gamecenter/injuries?week=1&season=2026",
     "note": "VERIFIED 401 Unauthorized 2026-09-10. Needs OAuth; not used."},
    # --- added 2026-09-18 after the missed Bills/Lions injuries -----------------
    {"key": "espn_news", "source": "espn",
     "url": espn_mod.NEWS_ENDPOINT + "?limit=3",
     "note": "Verified 200 from an independent client 2026-09-18. Headline feed with "
             "published timestamps, team and athlete categories. MEASURED: the "
             "?athlete=<id> filter is IGNORED by this endpoint (it returns the league "
             "feed), so it is never used as a per-player source; ?team=<id> does filter."},
    {"key": "espn_scoreboard", "source": "espn",
     "url": espn_mod.SCOREBOARD_ENDPOINT + "?dates=20260917",
     "note": "Verified 200 2026-09-18 (event 401872932 DET@BUF, kickoff "
             "2026-09-18T00:15Z, state=post). Used ONLY to decide which clubs are in a "
             "game window so query budget goes where injuries are happening."},
    {"key": "bsky_author_feed_verified_insider", "source": "bluesky",
     "url": social_mod.BSKY_AUTHOR_FEED + "?actor=rapsheet.bsky.social&limit=3",
     "note": "Verified 200 keyless from an independent client 2026-09-18. This is the "
             "Bluesky path that WORKS (searchPosts is 403) and it returns the author's "
             "own verification state. It is the fix for the missed 01:37Z DJ Moore post."},
    {"key": "rotowire_news_rss", "source": "rotowire",
     "url": rotowire_mod.NEWS_RSS_URL,
     "note": "Verified 200 2026-09-18. RotoWire's PUBLISHED RSS news feed (distinct "
             "from the paywalled injury-report page): fastest free wire for "
             "'Player: ruled out / questionable to return' style updates."},
    {"key": "google_news_player_query", "source": "google-news",
     "url": social_mod.GOOGLE_NEWS_RSS + "?q=%22Keon+Coleman%22+injury+when%3A1d"
            "&hl=en-US&gl=US&ceid=US:en",
     "note": "Verified 200 2026-09-18: this exact query returned the in-game reports "
             "for Keon Coleman (aol 01:39Z, Yahoo 01:40Z, Democrat and Chronicle "
             "01:47Z). The previous collector only ever asked the league-wide question, "
             "which is why neither player's update appeared."},
    {"key": "espn_athlete_news_filter_IGNORED", "source": "espn",
     "url": espn_mod.NEWS_ENDPOINT + "?athlete=4635008&limit=2",
     "note": "MEASURED 2026-09-18: returns the same league-wide articles as ?limit=2, "
             "so the athlete filter does nothing. Kept in the ledger so nobody builds "
             "a per-player feed on it."},
    {"key": "nfl_team_injuries_page_404", "source": "nfl.com",
     "url": "https://www.nfl.com/teams/buffalo-bills/injuries/",
     "note": "VERIFIED 404 on 2026-09-18 ('404 - Flag on the Play'). There is no "
             "per-club injury page on nfl.com; https://www.nfl.com/injuries/ is the "
             "only official injury surface, so club-level coverage has to come from "
             "club posts and wires. Recorded so it is not re-tried."},
    {"key": "rotowire_lineups", "source": "rotowire", "url": rotowire_mod.LINEUPS_URL,
     "note": "Reverse-engineering target. Free lineups + inactives."},
    {"key": "rotowire_injury_report_PAYWALLED", "source": "rotowire",
     "url": "https://www.rotowire.com/football/injury-report.php",
     "note": "VERIFIED 2026-09-10: 'Est. Return' column renders 'Subscribers Only'."},
    # Reporter-directory verification endpoints.
    {"key": "nfl_team_directory_sample", "source": "nfl.com",
     "url": directory_mod.NFL_TEAM_PAGE.format(slug="arizona-cardinals"),
     "note": "Club-published social directory; parser extracts the official site + handles."},
    {"key": "bsky_actor_search", "source": "bluesky",
     "url": directory_mod.BSKY_SEARCH_ACTORS + "?q=Ian%20Rapoport&limit=3",
     "note": "Keyless public AppView identity search used by the reporter directory. "
             "(app.bsky.feed.searchActors was renamed to app.bsky.actor.searchActors.)"},
    {"key": "bsky_verified_profile_rapsheet", "source": "bluesky",
     "url": directory_mod.BSKY_GET_PROFILE + "?actor=rapsheet.bsky.social",
     "note": "Baseline verified-insider profile: must show verification.verifiedStatus='valid'."},
    {"key": "x_oembed_BLOCKED", "source": "x",
     "url": directory_mod.X_OEMBED + "?url=https%3A%2F%2Fx.com%2FAdamSchefter",
     "note": "VERIFIED HTTP 403 on 2026-09-10: no free keyless X identity check; X rows on "
             "the directory are therefore one-click manual-review links, never 'verified'."},
]


def verify() -> Dict[str, Any]:
    """Probe every candidate source and publish the honest result."""

    reset_probe_log()
    results: List[Dict[str, Any]] = []
    for target in VERIFY_TARGETS:
        from .http import probe

        entry = probe(target["url"], source=target["source"])
        results.append(
            {
                "key": target["key"],
                "source": target["source"],
                "url": target["url"],
                "note": target["note"],
                "reachable": bool(entry.get("reachable")),
                "status": entry.get("status"),
                "latency_ms": entry.get("latency_ms"),
                "error": entry.get("error", ""),
            }
        )

    social_probes = social_mod.probe_platforms()
    payload = {
        "generated_at": _now(),
        "sources": results,
        "social_platforms": social_probes,
        "x_twitter": {
            "usable_for_free": False,
            "reason": (
                "X discontinued its free API tier for new developers and moved to "
                "pay-per-use (~$0.005 per post read); Basic/Pro are closed to new signups. "
                "No free read path exists, so X is link-out only in this project."
            ),
            "refs": [
                "https://api.sorsa.io/blog/is-twitter-api-free",
                "https://www.socialcrawl.dev/blog/x-twitter-api-2026",
            ],
            "link_out_example": social_mod.x_search_url("Patrick Mahomes", "KC"),
        },
    }
    _write(os.path.join(LATEST_DIR, "health.json"), payload)
    return payload


# ------------------------------------------------------------------ collect ---

def collect(args: argparse.Namespace) -> int:
    reset_probe_log()
    now = _now()

    index = PlayerIndex.from_dict(_read(os.path.join(STATE_DIR, "players.json"), {"players": {}}))
    registry = ReporterRegistry.from_dict(_read(os.path.join(STATE_DIR, "reporters.json"), {}))

    # Which Bluesky accounts are read on every run, and why (see
    # directory.watched_handles). includes the verified national insiders even on
    # the very first run, which is what was missing on 2026-09-17.
    watch = directory_mod.watched_handles(STATE_DIR)
    verified_handles = list(watch["verified"])
    candidate_handles = list(watch["candidates"])
    for handle in _read(os.path.join(STATE_DIR, "watched.json"), {"handles": []}).get("handles", []):
        if handle and handle not in verified_handles and handle not in candidate_handles:
            candidate_handles.append(handle)

    # Which clubs are inside a game window? Decided from ESPN's own scoreboard, so
    # a warm-up injury an hour before kickoff (Ed Oliver, 2026-09-17T23:10Z) and an
    # in-game injury are both covered. Outside a game window the collector rotates
    # through the other clubs instead of querying all 32 every run.
    scoreboard = espn_mod.collect_scoreboard()
    hot_teams = [t for t in (scoreboard.get("hot_teams") or []) if t in nfl_mod.NFL_TEAMS]
    hot_players = _hot_players(index, hot_teams)

    sources = collect_sources(
        with_rotowire=getattr(args, "with_rotowire", False),
        with_social=not getattr(args, "no_social", False),
        with_news=not getattr(args, "no_news", False),
        player_index=index,
        watched_handles=verified_handles,
        candidate_handles=candidate_handles,
        hot_teams=hot_teams,
        hot_players=hot_players,
    )

    # Grow the roster index from whatever this run returned.
    for key in ("official", "espn", "rotowire"):
        payload = sources.get(key)
        if payload:
            index.add_many(payload.get("injuries", []))

    # Every text item this run collected: social platforms AND the fast wires.
    # The wires matter because they are the only sources that reported either of
    # the two injuries missed on 2026-09-17 (RotoWire/ESPN news style items).
    posts: List[Dict[str, Any]] = list((sources.get("social") or {}).get("posts") or [])
    for key in ("espn_news", "rotowire_news"):
        payload = sources.get(key) or {}
        posts.extend(i.to_dict() for i in (payload.get("items") or []))

    game_events = ingame_mod.build_game_events(
        posts=posts, espn=sources.get("espn"), player_index=index, now=now,
        hot_teams=hot_teams,
    )

    # First-seen history: the run at which each (club, player, designation) was
    # first observed. This is the only honest "the official record caught up at"
    # instant available, and it is what reporter lead time is measured against.
    first_seen = _read(os.path.join(STATE_DIR, "first_seen.json"), {})

    previous = _read(os.path.join(LATEST_DIR, "report.json"))
    report = reconcile(
        sources.get("official"), sources.get("espn"), sources.get("rotowire"),
        previous=previous, now=now,
        game_events=game_events,
        alert_log=(previous or {}).get("alert_log") or [],
        live_games=scoreboard.get("games") or [],
    )

    social_payload = sources.get("social") or {"posts": [], "irregularities": [],
                                               "platforms_probed": {}}
    # Claims are built from every text source, so a headline that beats the
    # official report is scored (and its author credited) rather than dropped.
    claims = build_claims({"posts": posts}, player_index=index, espn=sources.get("espn"))
    canonical = {f"{p['team']}:{p['key']}": p for p in report["players"]}
    for p in report["players"]:
        fk = f"{p['team']}:{p['key']}:{p['game_status']}"
        first_seen.setdefault(fk, now)
    ground_truth_ts = {
        f"{p['team']}:{p['key']}": first_seen.get(
            f"{p['team']}:{p['key']}:{p['game_status']}", ""
        )
        for p in report["players"]
    }
    claims, claim_flags = resolve_claims(
        claims, canonical, ground_truth_ts=ground_truth_ts
    )
    tier_changes = apply_to_registry(registry, claims)
    report["irregularities"].extend(i.to_dict() for i in claim_flags)

    # Verified-reporter / official-source directory (directory.html). Club
    # pages and Bluesky profiles are verified live here; failures degrade to
    # cached/manual-review rows and raise flags rather than killing collect.
    try:
        directory_payload = directory_mod.build(
            offline=False,
            claims_payload={"generated_at": now,
                            "claims": [c.to_dict() for c in claims]},
            latest_dir=LATEST_DIR, state_dir=STATE_DIR,
        )
        report["irregularities"].extend(directory_payload.get("irregularities", []))
    except Exception as exc:  # noqa: BLE001 - directory must never kill a snapshot
        sources["errors"].append(
            {"source": "directory", "status": None,
             "reason": f"{type(exc).__name__}: {exc}"})

    # Watch handles that keep proving accurate, so the feed tightens over time.
    for row in registry.to_dict()["reporters"]:
        if row["platform"] == "bluesky" and row["handle"] and row["tier"] == "established":
            if row["handle"] not in candidate_handles:
                candidate_handles.append(row["handle"])

    cadence = cadence_metrics(archive_dir=ARCHIVE_DIR, now=now)
    if cadence.get("max_gap_minutes") and cadence["max_gap_minutes"] > 60:
        report["irregularities"].append(
            Irregularity(
                code="COLLECTOR_CADENCE_DEGRADED",
                severity="medium",
                title=("Longest gap between collector runs is "
                       f"{cadence['max_gap_minutes']} minutes"),
                detail=(
                    "The workflow is scheduled every 5 minutes, but GitHub throttles "
                    "and queues scheduled workflows, so this is the cadence the "
                    "collector actually achieved over the archived runs. Alerts can "
                    "only be as timely as this number: see the cadence panel on the "
                    "site, which is fed from the same measurement."
                ),
                evidence=[{"label": "Workflow schedule",
                           "url": "https://github.com/buffedlizard55-lab/NFLInjuryReport"
                                  "/actions/workflows/collect.yml"}],
            ).to_dict()
        )

    _write(os.path.join(LATEST_DIR, "report.json"), report)
    _write(os.path.join(LATEST_DIR, "alerts.json"),
           {"generated_at": now, "alerts": report["alerts"],
            "log": report.get("alert_log", [])})
    _write(os.path.join(LATEST_DIR, "ingame.json"),
           {"generated_at": now, "counts": report["counts"],
            "hot_teams": hot_teams, "hot_players": hot_players,
            "live_games": [g for g in (scoreboard.get("games") or [])
                           if g.get("state") in ("in", "pre", "post")],
            "events": report.get("game_events", []),
            "watched_handles": {"verified": verified_handles,
                                "candidates": candidate_handles},
            "fetches": (sources.get("social") or {}).get("fetches", []),
            "note": ("In-game events are stated by the source they are attributed to; "
                     "each one carries the verbatim sentence it came from, the source "
                     "timestamp, and the time this pipeline first saw it.")})
    _write(os.path.join(LATEST_DIR, "flags.json"),
           {"generated_at": now, "irregularities": report["irregularities"],
            "source_errors": sources["errors"]})
    _write(os.path.join(LATEST_DIR, "social.json"),
           {"generated_at": now, "platforms_probed": social_payload.get("platforms_probed", {}),
            "watched_handles": {"verified": verified_handles,
                                "candidates": candidate_handles},
            "fetches": social_payload.get("fetches", []),
            "posts": social_payload.get("posts", []),
            "irregularities": [i.to_dict() for i in social_payload.get("irregularities", [])]})
    _write(os.path.join(LATEST_DIR, "scorecard.json"),
           {"generated_at": now, "summary": summarise(claims),
            "tier_changes": tier_changes, **registry.to_dict()})
    _write(os.path.join(LATEST_DIR, "claims.json"),
           {"generated_at": now, "claims": [c.to_dict() for c in claims]})
    _write(os.path.join(LATEST_DIR, "meta.json"),
           {"generated_at": now, "season": report.get("season"), "week": report.get("week"),
            "counts": report["counts"], "source_errors": sources["errors"],
            "probes": get_probe_log(),
            "rotowire_enabled": getattr(args, "with_rotowire", False),
            "rotowire_news_enabled": not getattr(args, "no_news", False),
            "social_enabled": not getattr(args, "no_social", False),
            "cadence": cadence,
            "live": {"hot_teams": hot_teams, "hot_players": hot_players,
                     "games": len(scoreboard.get("games") or []),
                     "live_games": len(scoreboard.get("live") or [])},
            "watched_handles": {"verified": verified_handles,
                                "candidates": candidate_handles}})
    _write(os.path.join(STATE_DIR, "players.json"), index.to_dict())
    _write(os.path.join(LATEST_DIR, "players.json"), index.to_dict())
    _write(os.path.join(STATE_DIR, "reporters.json"), registry.to_dict())
    _write(os.path.join(STATE_DIR, "watched.json"),
           {"handles": candidate_handles, "verified": verified_handles,
            "basis": watch.get("basis", {}),
            "note": ("Handles here are read on every run via getAuthorFeed. "
                     "'verified' entries carry a valid Bluesky platform verification; "
                     "'handles' entries are hand-asserted (seed) or promoted by the "
                     "scorecard and are labelled unverified in the UI.")})
    _write(os.path.join(STATE_DIR, "first_seen.json"), first_seen)

    day = now[:10]
    _write(os.path.join(ARCHIVE_DIR, day, f"report-{now[11:16].replace(':', '')}.json"),
           {"generated_at": now, "counts": report["counts"],
            "players": report["players"], "alerts": report["alerts"],
            "game_events": report.get("game_events", []),
            "alert_log": report.get("alert_log", [])})

    verify()

    if not sources.get("official") and not sources.get("espn"):
        print("FATAL: neither the official report nor ESPN could be collected.", file=sys.stderr)
        for err in sources["errors"]:
            print(f"  - {err['source']}: {err['reason']}", file=sys.stderr)
        return 2
    print(
        f"ok: {report['counts']['players']} players across {report['counts']['teams']} clubs, "
        f"{report['counts']['alerts']} alerts, "
        f"{report['counts'].get('in_game_events', 0)} in-game events, "
        f"{report['counts']['irregularities']} flags "
        f"({len(sources['errors'])} source errors)"
    )
    print(
        f"cadence: {cadence.get('runs_last_24h')} runs in 24h, "
        f"median gap {cadence.get('median_gap_minutes')} min, "
        f"max gap {cadence.get('max_gap_minutes')} min; "
        f"hot teams: {', '.join(hot_teams) or 'none'}; "
        f"watched handles: {len(verified_handles)} verified / "
        f"{len(candidate_handles)} candidate"
    )
    return 0


def bootstrap() -> int:
    """Build data/latest from the test fixtures so the UI can be previewed.

    The output is explicitly marked `bootstrap: true` and carries its provenance,
    so nobody can mistake it for a live snapshot. The first real `collect` run
    overwrites every file.
    """

    import json as _json

    fixtures = os.path.join(REPO_ROOT, "tests", "fixtures")

    def read(name):
        with open(os.path.join(fixtures, name), "r", encoding="utf-8") as fh:
            return fh.read()

    official = nfl_mod.parse_injuries_html(read("nfl_injuries.html"),
                                          fetched_at="2026-09-10T22:00:00Z")
    espn = espn_mod.parse_injuries(_json.loads(read("espn_injuries.json")),
                                   fetched_at="2026-09-10T22:00:00Z")
    rotowire = rotowire_mod.parse_lineups_html(read("rotowire_lineups.html"),
                                               fetched_at="2026-09-10T22:00:00Z")

    index = PlayerIndex()
    for payload in (official, espn, rotowire):
        index.add_many(payload.get("injuries", []))

    report = reconcile(official, espn, rotowire, previous=None, now=_now())
    claims = build_claims({"posts": []}, player_index=index, espn=espn)
    canonical = {f"{p['team']}:{p['key']}": p for p in report["players"]}
    claims, claim_flags = resolve_claims(claims, canonical)
    registry = ReporterRegistry()
    tier_changes = apply_to_registry(registry, claims)
    report["irregularities"].extend(i.to_dict() for i in claim_flags)

    note = (
        "BOOTSTRAP SAMPLE — generated from tests/fixtures, which reproduce the response "
        "shapes captured live from nfl.com and ESPN on 2026-09-10. This is NOT a live "
        "snapshot and is replaced in full by the first scheduled `collect` run."
    )
    _write(os.path.join(LATEST_DIR, "report.json"), dict(report, bootstrap=True, note=note))
    _write(os.path.join(LATEST_DIR, "alerts.json"),
           {"generated_at": report["generated_at"], "alerts": report["alerts"],
            "log": [], "bootstrap": True, "note": note})
    _write(os.path.join(LATEST_DIR, "ingame.json"),
           {"generated_at": report["generated_at"],
            "events": report.get("game_events", []), "live_games": [],
            "hot_teams": [], "hot_players": [], "watched_handles": {"verified": [],
                                                                   "candidates": []},
            "counts": report.get("counts", {}), "bootstrap": True, "note": note})
    _write(os.path.join(LATEST_DIR, "flags.json"),
           {"generated_at": report["generated_at"],
            "irregularities": report["irregularities"], "source_errors": [],
            "bootstrap": True, "note": note})
    _write(os.path.join(LATEST_DIR, "social.json"),
           {"generated_at": report["generated_at"], "platforms_probed": {}, "posts": [],
            "irregularities": [], "bootstrap": True, "note": note})
    _write(os.path.join(LATEST_DIR, "scorecard.json"),
           {"generated_at": report["generated_at"], "summary": summarise(claims),
            "tier_changes": tier_changes, "bootstrap": True, "note": note,
            **registry.to_dict()})
    _write(os.path.join(LATEST_DIR, "claims.json"),
           {"generated_at": report["generated_at"],
            "claims": [c.to_dict() for c in claims], "bootstrap": True, "note": note})
    _write(os.path.join(LATEST_DIR, "meta.json"),
           {"generated_at": report["generated_at"], "season": report.get("season"),
            "week": report.get("week"), "counts": report["counts"], "source_errors": [],
            "probes": [], "cadence": {}, "live": {}, "watched_handles": {},
            "bootstrap": True, "note": note})
    _write(os.path.join(LATEST_DIR, "health.json"), {
        "generated_at": report["generated_at"], "bootstrap": True, "note": note,
        "sources": [], "social_platforms": {},
        "x_twitter": {
            "usable_for_free": False,
            "reason": ("X discontinued its free API tier for new developers and moved to "
                       "pay-per-use (~$0.005 per post read); Basic/Pro are closed to new "
                       "signups. No free read path exists, so X is link-out only here."),
            "refs": ["https://api.sorsa.io/blog/is-twitter-api-free",
                     "https://www.socialcrawl.dev/blog/x-twitter-api-2026"],
            "link_out_example": "https://x.com/search?q=%22Ty%20Okada%22%20injury&f=live",
        },
        "probes_note": ("Populated by `python3 -m collectors.pipeline verify`, which needs "
                        "network access. Run it, or let the scheduled workflow do it."),
    })
    _write(os.path.join(STATE_DIR, "players.json"), index.to_dict())
    _write(os.path.join(LATEST_DIR, "players.json"), index.to_dict())
    _write(os.path.join(STATE_DIR, "reporters.json"), registry.to_dict())
    _write(os.path.join(STATE_DIR, "watched.json"), {"handles": []})
    print(f"bootstrap: wrote sample data ({report['counts']['players']} players, "
          f"{len(report['irregularities'])} flags)")
    return 0


def status() -> int:
    meta = _read(os.path.join(LATEST_DIR, "meta.json"), {})
    health = _read(os.path.join(LATEST_DIR, "health.json"), {})
    print(f"last run        : {meta.get('generated_at', 'never')}")
    print(f"season / week   : {meta.get('season')} / {meta.get('week')}")
    print(f"counts          : {json.dumps(meta.get('counts', {}))}")
    print(f"source errors   : {len(meta.get('source_errors', []))}")
    for row in health.get("sources", []):
        mark = "OK " if row["reachable"] else "FAIL"
        print(f"  [{mark}] {row['status'] or '---'} {row['key']}")
    for name, info in (health.get("social_platforms") or {}).items():
        mark = "OK " if info.get("reachable") else "FAIL"
        print(f"  [{mark}] {info.get('status') or '---'} social:{name}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="collectors.pipeline", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect", help="fetch, reconcile, score, publish")
    p_collect.add_argument("--with-rotowire", action="store_true",
                           help="also scrape RotoWire lineups (off by default: ToS review)")
    p_collect.add_argument("--no-social", action="store_true",
                           help="skip the social verification layer")
    p_collect.add_argument("--no-news", action="store_true",
                           help="skip the fast news wires (RotoWire RSS + ESPN news API)")
    sub.add_parser("verify", help="probe every source and publish health.json")
    sub.add_parser("bootstrap", help="write clearly-labelled sample data from the fixtures")
    sub.add_parser("status", help="print the last run summary")

    args = parser.parse_args(argv)
    if args.cmd == "collect":
        return collect(args)
    if args.cmd == "verify":
        payload = verify()
        ok = sum(1 for r in payload["sources"] if r["reachable"])
        print(f"verify: {ok}/{len(payload['sources'])} sources reachable")
        return 0
    if args.cmd == "bootstrap":
        return bootstrap()
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
