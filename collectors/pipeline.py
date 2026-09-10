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

from . import espn as espn_mod
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
    player_index: Optional[PlayerIndex] = None,
    watched_handles: Optional[List[str]] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "official": None, "espn": None, "rotowire": None, "social": None,
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

    if with_social:
        names = [p["name"] for p in (player_index.players.values() if player_index else [])]
        try:
            out["social"] = social_mod.collect_social(
                player_names=names,
                watched_handles=watched_handles or [],
            )
        except Exception as exc:  # noqa: BLE001 - social must never break the run
            out["errors"].append({"source": "social", "reason": f"{type(exc).__name__}: {exc}",
                                  "status": None, "url": ""})
    return out


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
    {"key": "rotowire_lineups", "source": "rotowire", "url": rotowire_mod.LINEUPS_URL,
     "note": "Reverse-engineering target. Free lineups + inactives."},
    {"key": "rotowire_injury_report_PAYWALLED", "source": "rotowire",
     "url": "https://www.rotowire.com/football/injury-report.php",
     "note": "VERIFIED 2026-09-10: 'Est. Return' column renders 'Subscribers Only'."},
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

    watched = _read(os.path.join(STATE_DIR, "watched.json"), {"handles": []})["handles"]
    sources = collect_sources(
        with_rotowire=args.with_rotowire,
        with_social=not args.no_social,
        player_index=index,
        watched_handles=watched,
    )

    # Grow the roster index from whatever this run returned.
    for key in ("official", "espn", "rotowire"):
        payload = sources.get(key)
        if payload:
            index.add_many(payload.get("injuries", []))

    # First-seen history: the run at which each (club, player, designation) was
    # first observed. This is the only honest "the official record caught up at"
    # instant available, and it is what reporter lead time is measured against.
    first_seen = _read(os.path.join(STATE_DIR, "first_seen.json"), {})

    previous = _read(os.path.join(LATEST_DIR, "report.json"))
    report = reconcile(
        sources.get("official"), sources.get("espn"), sources.get("rotowire"),
        previous=previous, now=now,
    )

    social_payload = sources.get("social") or {"posts": [], "irregularities": [],
                                               "platforms_probed": {}}
    claims = build_claims(social_payload, player_index=index, espn=sources.get("espn"))
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

    # Watch handles that keep proving accurate, so the feed tightens over time.
    for row in registry.to_dict()["reporters"]:
        if row["platform"] == "bluesky" and row["handle"] and row["tier"] == "established":
            if row["handle"] not in watched:
                watched.append(row["handle"])

    _write(os.path.join(LATEST_DIR, "report.json"), report)
    _write(os.path.join(LATEST_DIR, "alerts.json"),
           {"generated_at": now, "alerts": report["alerts"]})
    _write(os.path.join(LATEST_DIR, "flags.json"),
           {"generated_at": now, "irregularities": report["irregularities"],
            "source_errors": sources["errors"]})
    _write(os.path.join(LATEST_DIR, "social.json"),
           {"generated_at": now, "platforms_probed": social_payload.get("platforms_probed", {}),
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
            "probes": get_probe_log(), "rotowire_enabled": args.with_rotowire,
            "social_enabled": not args.no_social})
    _write(os.path.join(STATE_DIR, "players.json"), index.to_dict())
    _write(os.path.join(STATE_DIR, "reporters.json"), registry.to_dict())
    _write(os.path.join(STATE_DIR, "watched.json"), {"handles": watched})
    _write(os.path.join(STATE_DIR, "first_seen.json"), first_seen)

    day = now[:10]
    _write(os.path.join(ARCHIVE_DIR, day, f"report-{now[11:16].replace(':', '')}.json"),
           {"generated_at": now, "counts": report["counts"],
            "players": report["players"], "alerts": report["alerts"]})

    verify()

    if not sources.get("official") and not sources.get("espn"):
        print("FATAL: neither the official report nor ESPN could be collected.", file=sys.stderr)
        for err in sources["errors"]:
            print(f"  - {err['source']}: {err['reason']}", file=sys.stderr)
        return 2
    print(
        f"ok: {report['counts']['players']} players across {report['counts']['teams']} clubs, "
        f"{report['counts']['alerts']} alerts, {report['counts']['irregularities']} flags "
        f"({len(sources['errors'])} source errors)"
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
            "bootstrap": True, "note": note})
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
            "probes": [], "bootstrap": True, "note": note})
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
