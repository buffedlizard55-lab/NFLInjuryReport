# NFL Injury Report

A live injury-alert system for all 32 NFL teams, built **only** from free, public,
verifiable sources, with a reporter-accuracy scorecard and a chat-style live feed.

Every designation shown on the site is the **official nfl.com** value whenever
nfl.com publishes one, and every row carries a link back to its source so a human
can check it in one click.

**Live site:** GitHub Pages — enabled by `.github/workflows/pages.yml`.
URL after deploy: `https://buffedlizard55-lab.github.io/NFLInjuryReport/`

---

## 1. Source ledger — verified line by line

Each row below was **actually probed on 2026-09-10** during this build. The
"Result" column is the observed outcome, not an expectation. Re-run any of them
with `python3 -m collectors.pipeline verify`, which republishes this table as
machine-generated JSON (`data/latest/health.json`) on every CI run.

| # | Source | URL | Result observed 2026-09-10 | Used? |
|---|--------|-----|---------------------------|-------|
| 1 | **NFL official injury report** | https://www.nfl.com/injuries/ | **HTTP 200.** Title: *"Official Latest NFL Injury Report for Players - Week 1 of the 2026 Season \| NFL.com"*. Per-game tables: Player \| Position \| Injuries \| Practice Status \| Game Status. Season selector back to 1965. | ✅ **Primary ground truth** |
| 2 | NFL Injury Report Policy | https://operations.nfl.com/gameday/injury-report/ | Reachable; defines the designation vocabulary and filing windows. | ✅ Reference |
| 3 | **ESPN injuries JSON** | https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries | **HTTP 200**, keyless. `timestamp: "2026-09-10T22:03:05Z"`, `season: {year:2026, type:2, name:"Regular Season"}`. Per injury: `longComment`, `shortComment`, `status`, `date` (minute precision), `athlete`, `position.abbreviation`, `team.abbreviation`, `headshot`. | ✅ **Primary feed** (timestamps + attribution) |
| 4 | ESPN teams JSON | https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams | **HTTP 200**, keyless. All 32 clubs with ESPN ids and 2026 season metadata. | ✅ Club mapping |
| 5 | ESPN **per-team** injuries | https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/22/injuries | ⚠️ **HTTP 200 but body is `{}`** — returns no data. | ❌ **Do not use** |
| 6 | NFL's own JSON API | https://api.nfl.com/experience/v1/gamecenter/injuries?week=1&season=2026 | ⚠️ **HTTP 401 Unauthorized** (Varnish error 54113). Requires an OAuth token. | ❌ Not free |
| 7 | **RotoWire lineups** (the reverse-engineering target) | https://www.rotowire.com/football/lineups.php | **HTTP 200**, free. Projected starters, per-player status letters (`Q`, `D`), an `Inactives` section, team logos at `assets.rotowire.com/images/teamlogo/football/{CODE}.svg`, player anchors at `/football/player/{slug}-{id}` whose `title` attribute is the full name. | ⚠️ Optional (`--with-rotowire`) |
| 8 | RotoWire injury report | https://www.rotowire.com/football/injury-report.php | ⚠️ **Paywalled.** The `Est. Return` column renders as *"Subscribers Only"* under *"Unlock the Full Injury Report Today / Subscribe Now"*. Player/Team/Pos/Injury/Status are free. | ❌ Not a primary source |
| 9 | **Bluesky** (AT Protocol) | https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts | Public AppView endpoint; per the official reference, `app.bsky.*` public endpoints *"don't require authentication"* against `https://public.api.bsky.app` — [docs.bsky.app](https://docs.bsky.app/docs/category/http-reference). | ✅ **Primary social feed** |
| 10 | Mastodon | `https://{instance}/api/v1/timelines/tag/{tag}` | Keyless by protocol design; reachability is instance-specific, so it is probed each run. | ✅ Probed |
| 11 | Google News RSS | https://news.google.com/rss/search | Keyless RSS. | ✅ Probed |
| 12 | Reddit `.json` | `https://www.reddit.com/r/{sub}/new.json` | ⚠️ **Sources conflict.** One 2026 write-up says it still works unauthenticated at ~60 req/min; two others say it was broadly blocked (403) from ~May 30 2026. | ⚠️ Probed first, skipped if the probe fails |
| 13 | **X / Twitter** | https://developer.x.com | ❌ **No free read path.** X discontinued its free tier for new developers and moved to pay-per-use (~$0.005 per post read); Basic/Pro are closed to new signups. [sorsa](https://api.sorsa.io/blog/is-twitter-api-free), [socialcrawl](https://www.socialcrawl.dev/blog/x-twitter-api-2026) | ❌ **Link-out only** |
| 14 | Instagram / Facebook | — | No keyless public read API exists. | ❌ Excluded on purpose |

**Why two primary sources rather than one.** nfl.com is authoritative but publishes
only at the report windows and carries neither per-update timestamps nor reporter
attribution. ESPN's payload carries a per-injury `date` down to the minute *and*
names the beat writer inside `shortComment` (observed verbatim: *"…Dani Sureck of
the Cardinals' official site reports."*). nfl.com therefore decides the
designation; ESPN supplies the clock and the attribution.

---

## 2. Irregularities flagged for review

These were found during verification. The pipeline re-derives all of them
automatically on every run and publishes them to the **Flags** tab — nothing here
is a hand-maintained list.

| Code | Severity | Finding |
|------|----------|---------|
| `nfl_api_requires_auth` | info | The NFL's own `api.nfl.com` injury endpoint returns **401**. There is no free official *API*; the free official source is the HTML report. |
| `espn_per_team_empty` | medium | ESPN's per-team injuries endpoint returns `{}` while the league-wide one returns everything. Only the league-wide endpoint is called. |
| `rotowire_paywall` | medium | RotoWire's `Est. Return` is subscriber-only, so RotoWire cannot be a primary injury source. |
| `reddit_availability_conflict` | medium | Published sources disagree about whether Reddit's `.json` is still open in 2026. Resolved at runtime by probing rather than by assumption. |
| `x_no_free_tier` | high | No free X read API exists. Any "live Twitter feed" built on the official API would require payment; scraping X would breach its ToS. |
| `PLAYER_TEAM_CONFLICT` | high | Auto-raised when two sources attach the same player to **different clubs**. The row is flagged, never reassigned by guesswork. |
| `STATUS_CONFLICT` | medium | Auto-raised when nfl.com and ESPN give the same player different designations. The official value is published; the ESPN value is retained on the record. |
| `ESPN_TEAM_UNRESOLVED` | medium | Auto-raised when a team block cannot be mapped to one of the 32 club codes. The row is **dropped**, not guessed. |
| `RW_TEAM_AMBIGUOUS` | low | RotoWire lineup rows sit in two-team blocks with no per-row club marker, so `team` is left empty and both candidates are stored. |
| `TEAMS_WITHOUT_ROWS` | low | Clubs with no entries in the current report window. Usually normal; surfaced so an unexpected gap is visible. |
| `OFFICIAL_SOURCE_MISSING` | critical | Raised when nfl.com could not be fetched — the snapshot is then marked UNVERIFIED rather than presented as current. |
| `CLAIMS_CONTRADICTED` | low | Reporter claims the official report contradicts. Retained as the evidence that lowers a score. |

**Historical-backfill answer (asked directly in the brief):** a *historical*
reporter scorecard is **not** achievable from free sources. The official side can
be backfilled — nfl.com's season selector reaches back to 1965 — but there is no
keyless archive of historical X/Bluesky/Reddit posts with trustworthy timestamps,
and X has had no free read tier since February 2026. The scorecard is therefore
**forward-collected**, starting the moment the collector is enabled. The code
states this in `collectors/scoring.py` and the site states it on the Scorecard tab.

---

## 3. Architecture

```
        ┌────────────────────── GitHub Actions (every 10 min) ─────────────────────┐
        │                                                                          │
        │  nfl.com/injuries  ─┐                                                    │
        │  ESPN injuries JSON ─┼─► reconcile ─► report.json / alerts.json          │
        │  RotoWire (optional)─┘        │                                          │
        │                               ├─► flags.json      (irregularities)       │
        │  Bluesky / Mastodon ─┐        ├─► scorecard.json  (reporter accuracy)    │
        │  Google News        ─┼─► claims + scoring                                │
        │  Reddit (probed)    ─┘        └─► health.json     (source probe ledger)  │
        │                                            │                             │
        │                                  git commit data/                        │
        └────────────────────────────────────────────┼─────────────────────────────┘
                                                     ▼
                          GitHub Pages  ◄── docs/ + data/  (static, no backend)
                                                     │
                          browser also tries ESPN + Bluesky directly (CORS permitting)
```

* **No backend, no secrets.** Every source is keyless. The "server" is a scheduled
  workflow that commits JSON.
* **Standard library only.** No `pip install`, so no supply-chain step in CI.
* **Nothing is guessed.** Unmappable teams are dropped, ambiguous player matches
  return no match, and every disagreement becomes a flag with evidence links.

### Precedence

| Rank | Source | Wins |
|------|--------|------|
| 1 | nfl.com | The published designation. Only the league's Game Status Report is binding. |
| 2 | ESPN | Timestamps, injury detail, reporter attribution. |
| 3 | RotoWire | Lineup/inactive corroboration only. |

---

## 4. Reporter scoring

A **claim** is `(platform, author, player, club, predicted status, timestamp, url)`.
Claims come from two places, both automatic:

1. The beat writer ESPN names on each update (first-party attribution).
2. Any social author who posts an injury-relevant statement matching a known player.

**Ground truth** is the official nfl.com designation for the same club + player.

Availability classes:

| Class | Statuses |
|-------|----------|
| `UNAVAILABLE` | OUT, IR, PUP, NFI, SUSPENDED |
| `LIMITED` | QUESTIONABLE, DOUBTFUL |
| `AVAILABLE` | ACTIVE |

| Resolution | Rule |
|------------|------|
| `CORRECT` | claim class == official class |
| `WRONG` | claim class != official class |
| `UNVERIFIABLE` | no official record exists for that player |
| `PENDING` | claim is younger than 6 h |

A reporter who says *"questionable"* about a player later **ruled out** scores
`WRONG`: the player did not play, so the availability call was wrong. This is
deliberately strict — it is what makes the number mean something.

Guardrails against flattering small samples:

* No score is published before **5 resolved claims**.
* The headline figure is the **Wilson 95% lower bound**, not the raw ratio, so 2/2
  does not read as a certain 100%.
* `score = 100 × accuracy × volume_confidence + lead_time_bonus × volume_confidence`,
  with the lead-time bonus saturating at a 4-hour lead.
* Below **60% accuracy** over ≥10 resolved claims the reporter is auto-`flagged`.
* At ≥80% over ≥5 the reporter is promoted to `established`; Bluesky handles that
  reach `established` are automatically added to the watch list, so the feed
  tightens on proven sources without any manual curation.

---

## 5. Latency — what to actually expect

Stated plainly, because the brief asks for low latency:

* **GitHub Actions cron floors at 5 minutes** and is best-effort, so the committed
  snapshot is up to ~10 minutes old (the schedule used here).
* The page additionally attempts a **direct browser fetch** of ESPN's injuries JSON
  and Bluesky search on load and every 30 s. When those succeed the feed is
  effectively live and the header dot turns green with a `LIVE` tag on each item.
* Those direct calls depend on the upstream **CORS** policy, which this project
  cannot promise. On failure the committed snapshot is used and the UI says so.
  It never claims to be live when it is not.

For true sub-second push you would need a persistent process (a Jetstream
WebSocket consumer for Bluesky, plus a paid X feed). That is outside a static
Pages site and is documented as the upgrade path rather than faked.

---

## 6. Running it

```bash
python3 -m unittest discover -s tests -t .   # 101 tests
python3 -m collectors.pipeline verify        # probe every source -> health.json
python3 -m collectors.pipeline collect       # fetch, reconcile, score, publish
python3 -m collectors.pipeline collect --with-rotowire
python3 -m collectors.pipeline bootstrap     # labelled sample data for UI preview
python3 -m collectors.pipeline status
```

Preview the site locally:

```bash
python3 -m http.server 8000     # then open http://localhost:8000/docs/
```

---

## 7. Repository layout

```
collectors/
  http.py        stdlib fetch + per-source probe accounting
  models.py      canonical model, status vocabulary, normalisation
  nfl_com.py     official nfl.com report parser (class-name agnostic)
  espn.py        ESPN injuries JSON parser + attribution extractor
  rotowire.py    reverse-engineered lineups parser (optional)
  matching.py    club+name player index (ambiguous matches return nothing)
  reporters.py   reporter registry, Wilson scoring, auto-tiering
  reconcile.py   precedence merge, alerts, irregularity detection
  scoring.py     claim building and resolution against the official report
  social.py      Bluesky / Mastodon / Google News / Reddit adapters + X links
  pipeline.py    CLI: collect | verify | bootstrap | status
docs/            GitHub Pages site (plain HTML/CSS/JS, no build step)
data/latest/     committed snapshot the site reads
data/state/      accumulated roster + reporter registry (survives across runs)
data/archive/    per-run history, pruned after 14 days by CI
tests/           101 tests, fixtures reproduce the shapes captured live
```

---

## 8. Verification log

What was actually executed while building this, and what came back:

| Check | Command / action | Result |
|-------|------------------|--------|
| Unit + integration tests | `python3 -m unittest discover -s tests -t .` | **Ran 101 tests — OK** |
| Official report parser | `tests/test_nfl_com.py` against a fixture reproducing the live table structure | 9 rows across 4 clubs, season 2026 week 1, correct club attribution, correct designations |
| ESPN parser | `tests/test_espn.py` against a fixture using the verbatim values from the live payload | `Jeremiyah Love / ARI / QUESTIONABLE / ankle`, attribution `Dani Sureck` → outlet `Cardinals' official site`, timestamp normalised to `2026-09-10T20:48:00Z`; unmappable club dropped and flagged |
| End-to-end pipeline | `tests/test_pipeline.py` (network stubbed to fixtures) | Writes all 7 JSON files; official designation beats ESPN; second run diffs and emits a `cleared` alert; single-source outage exits 0 and is recorded; total outage exits 2 |
| Bootstrap | `python3 -m collectors.pipeline bootstrap` | 10 players, 5 flags, 2 reporters observed with scores withheld (2 resolved < 5 minimum) |
| Site serving | `python3 -m http.server 8000` + `curl` | `/docs/` 200 (8.3 kB), `app.js` 200, `app.css` 200, `data/latest/*.json` 200 |

**Not verified in this sandbox:** the collectors have not been run against the live
internet from this environment (sandbox egress is restricted to GitHub). The
scheduled workflow runs them on a GitHub runner with full network access and
commits the real output; check the **Sources** tab for the live probe ledger.

---

## 9. Legal / terms notes

* Designations are quoted from nfl.com and attributed to their sources; nothing is
  republished as this project's own reporting.
* RotoWire scraping may be restricted by their Terms of Use, which is why that
  source is **off by default** and behind `--with-rotowire`.
* X is not scraped. Instagram and Facebook are not scraped. Both are excluded
  rather than worked around.
* Not affiliated with the NFL, ESPN or RotoWire.
