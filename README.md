# NFL Injury Report

A live injury-alert system for all 32 NFL teams, built **only** from free, public,
verifiable sources, with a reporter-accuracy scorecard and a chat-style live feed.

Every designation shown on the site is the **official nfl.com** value whenever
nfl.com publishes one, and every row carries a link back to its source so a human
can check it in one click.

**Live site (GitHub Pages):** `https://buffedlizard55-lab.github.io/NFLInjuryReport/`
Deployed by `.github/workflows/pages.yml`. Data refreshed every 10 minutes by
`.github/workflows/collect.yml`.

---

## 1. Source ledger — every line actually probed

Nothing below is an assumption. Each row was probed from a GitHub Actions runner
on **2026-09-10** and the observed result is recorded. The same table is
regenerated as machine-readable JSON on every run (`data/latest/health.json`) and
rendered on the site's **Sources** tab, so it cannot drift out of date.

| # | Source | Result observed 2026-09-10 | Used? |
|---|--------|---------------------------|-------|
| 1 | **NFL official injury report**<br>https://www.nfl.com/injuries/ | **HTTP 200** in 56 ms, 366 kB. Title *"Official Latest NFL Injury Report for Players - Week 1 of the 2026 Season"*. Per-game tables: Player \| Position \| Injuries \| Practice Status \| Game Status. Season selector back to 1965. | ✅ **Primary ground truth** |
| 2 | **ESPN injuries JSON**<br>https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries | **HTTP 200**, keyless, 8.9 MB. `timestamp: "2026-09-10T22:03:05Z"`, `season: {year:2026, type:2, name:"Regular Season"}`. Per injury: `longComment`, `shortComment`, `status`, `date` (minute precision), `athlete.position.abbreviation`, `athlete.team.abbreviation`, `headshot`. | ✅ **Primary feed** — supplies timestamps + beat-writer attribution |
| 3 | ESPN teams JSON<br>https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams | **HTTP 200** in 64 ms. All 32 clubs with ESPN ids. | ✅ Club mapping |
| 4 | ESPN **per-team** injuries<br>`…/nfl/teams/22/injuries` | ⚠️ **HTTP 200 but body is `{}`** — no data. | ❌ **Do not use** |
| 5 | NFL's own JSON API<br>https://api.nfl.com/experience/v1/gamecenter/injuries | ⚠️ **HTTP 401 Unauthorized** (Varnish 54113). Needs OAuth. | ❌ Not free |
| 6 | **RotoWire lineups** (the reverse-engineering target)<br>https://www.rotowire.com/football/lineups.php | **HTTP 200** in 513 ms, free. Starters, status letters (`Q`/`D`), an `Inactives` section, logos at `assets.rotowire.com/images/teamlogo/football/{CODE}.svg`, players at `/football/player/{slug}-{id}` with the full name in `title`. | ⚠️ Optional (`--with-rotowire`) |
| 7 | RotoWire injury report<br>https://www.rotowire.com/football/injury-report.php | ⚠️ **Paywalled.** `Est. Return` renders *"Subscribers Only"* under *"Unlock the Full Injury Report Today"*. | ❌ Not a primary source |
| 8 | NFL Personnel (Injury) Report Policy PDFs<br>`operations.nfl.com/media/2683/…`, `…/media/2235/…` | ⚠️ **HTTP 404 to our fetcher**, despite being indexed by search engines. | ⚠️ **Probe-only — never cited as an evidence link** |
| 9 | **Bluesky** `app.bsky.feed.searchPosts`<br>https://public.api.bsky.app | ⚠️ **HTTP 403 Forbidden.** Note: `app.bsky.actor.getProfile` on the same host returns **200**, so a profile probe would have reported Bluesky as usable when *search* was not. | ❌ Disabled; probe now targets the endpoint we actually call |
| 10 | **Mastodon**<br>`https://mastodon.social/api/v1/timelines/tag/nfl` | **HTTP 200**, 187 kB, keyless. | ✅ Working social source |
| 11 | **Google News RSS**<br>https://news.google.com/rss/search | **HTTP 200**, 141 kB, keyless. | ✅ Working social source |
| 12 | Reddit `.json`<br>`https://www.reddit.com/r/NFL_Discussion/about.json` | ⚠️ **HTTP 403 Blocked.** This settles the conflict between published sources: one 2026 write-up claims ~60 req/min unauthenticated, two others claim it was blocked from ~May 2026. **The block is real.** | ❌ Probed each run; skipped when blocked |
| 13 | **X / Twitter** | ❌ **No free read path.** X discontinued its free tier for new developers and moved to pay-per-use (~$0.005 per post read); Basic/Pro are closed to new signups. [sorsa](https://api.sorsa.io/blog/is-twitter-api-free) · [socialcrawl](https://www.socialcrawl.dev/blog/x-twitter-api-2026) | ❌ **Link-out only** — never scraped |
| 14 | Instagram / Facebook | No keyless public read API exists. | ❌ Excluded rather than worked around |

**Why two primary sources.** nfl.com is authoritative but publishes only at report
windows and carries neither per-update timestamps nor attribution. ESPN carries a
per-injury `date` to the minute *and* names the beat writer inside `shortComment`
(observed verbatim: *"…Dani Sureck of the Cardinals' official site reports."*).
So nfl.com decides the designation; ESPN supplies the clock and the attribution.

### Independent corroboration of the parsed values

The values this project's parser extracted from nfl.com match a third party's
independent Week 1 2026 table row for row — [sharpfootballanalysis.com](https://www.sharpfootballanalysis.com/betting/nfl-injury-report-this-week-all-32-teams/):
`Ben Brown / C / Knee / DNP / Out`, `TreVeyon Henderson / RB / Ankle / DNP / Out`,
`Ty Okada / S / Hamstring / DNP / Out`, `Nick Emmanwori / S / Ankle / LP / Questionable`,
`Tory Horton / WR / Hamstring / LP / Questionable`, and `Christian Barmore / DT / FP /`
*"No designation"* — which is exactly the blank-Game-Status case described below.

---

## 2. What the first real run produced

From the live collector run at **2026-09-10T22:46:51Z**:

| Metric | Value |
|--------|-------|
| Players in the report | **823** |
| Clubs covered | **32 / 32** |
| Out / IR | 177 |
| Questionable | 30 |
| Doubtful | 1 |
| Social items collected | 41 (27 Google News, 14 Mastodon) |
| Irregularities flagged | 5 |
| Source errors | 0 |

Flags raised: `STATUS_CONFLICT` ×3, `PLAYER_TEAM_CONFLICT` ×1,
`INFERRED_VS_REPORTED` ×1, `CLAIMS_CONTRADICTED` ×1.

**A concrete catch:** `PLAYER_TEAM_CONFLICT` fired because **Byron Young** is listed
under **LAR** by nfl.com and **PHI** by ESPN. The independent table above lists him
at LAR, so the ESPN record is the wrong one. The pipeline flagged the disagreement
and left it for review instead of picking a side.

---

## 3. Three defects the live run exposed (and how they were fixed)

Running against the real internet contradicted three things that had been assumed.
All three are fixed; this section exists so the reasoning is auditable.

**1. A cited URL did not exist.** `operations.nfl.com/gameday/injury-report/`
returned **HTTP 404**. So did both Personnel (Injury) Report Policy PDFs. The
policy URLs are now *probe-only*: reported in the ledger so a human can check
them, and never used as an evidence link in the product. All evidence links now
point at `https://www.nfl.com/injuries/`, which is verified reachable.

**2. The Bluesky probe tested the wrong endpoint.** It called
`app.bsky.actor.getProfile` (HTTP **200**) while the adapter calls
`app.bsky.feed.searchPosts` (HTTP **403**). The probe was therefore reporting
Bluesky as usable when search was blocked. The probe now hits the endpoint that
is actually called, and the site shows Bluesky as failed — which is the truth.

**3. 54 of 58 `STATUS_CONFLICT` flags were false positives.** nfl.com prints a
*blank* Game Status for a player who practised without a designation; we render
that as `ACTIVE`, but it is our inference. ESPN's `status` field is news-derived,
not the filed designation. Comparing an inference against a news value is not a
conflict. Designations now carry provenance (`published` vs `inferred`); only
published-vs-published raises a conflict, and inferred-vs-reported is reported
once in aggregate. **Result: 59 irregularities → 5, and the 3 remaining
`STATUS_CONFLICT`s are genuine.**

The blank-designation inference is itself justified by the policy: a club is told
that a player "not injured but has been rested in practice should not be listed
on the Game Status Report with an injury status designation (Out, Doubtful, or
Questionable)" while still appearing on the Practice Report. Those rows are
labelled `inferred*` in the UI so they are never mistaken for a published
designation.

---

## 4. Irregularity catalogue

All auto-derived on every run and published to the **Flags** tab with evidence
links. Nothing here is hand-maintained.

| Code | Severity | Meaning |
|------|----------|---------|
| `PLAYER_TEAM_CONFLICT` | high | Two sources attach the same player to **different clubs**. Row flagged, never reassigned by guesswork. |
| `STATUS_CONFLICT` | medium | Two **published** designations disagree. The official one is shown; the other is retained. |
| `INFERRED_VS_REPORTED` | low | No official designation, but ESPN has a news status. Semantic difference, reported once in aggregate. |
| `OFFICIAL_SOURCE_MISSING` | critical | nfl.com could not be fetched. Snapshot marked UNVERIFIED rather than presented as current. |
| `ESPN_TEAM_UNRESOLVED` | medium | A team block does not map to one of the 32 club codes. Row **dropped**, not guessed. |
| `NFL_TABLE_NO_TEAM` / `NFL_NO_TABLES` | medium/high | Markup changed, or no report is published for the current window. |
| `RW_TEAM_AMBIGUOUS` | low | RotoWire rows sit in two-team blocks with no per-row club marker. |
| `UNATTRIBUTED_TEAM` | low | A row whose club could not be resolved; kept but unassigned. |
| `TEAMS_WITHOUT_ROWS` | low | Clubs with no entries in the current window. |
| `CLAIMS_CONTRADICTED` | low | Reporter claims the official report contradicts. Retained as scoring evidence. |
| `SOCIAL_UNREACHABLE_*` / `SOCIAL_FETCH_ERROR_*` | medium | A platform failed its probe or its adapter. Skipped, not retried into a block. |
| `STALE_SNAPSHOT` | medium | Gap between published snapshots exceeded 30 h. |

**Historical-backfill answer (asked directly in the brief):** a *historical*
reporter scorecard is **not** achievable from free sources. The official side can
be backfilled — nfl.com's season selector reaches 1965 — but there is no keyless
archive of historical X/Bluesky/Reddit posts with trustworthy timestamps, and X
has had no free read tier since February 2026. The scorecard is therefore
**forward-collected** from the moment the collector is enabled.

---

## 5. Architecture

```
      ┌─────────────────── GitHub Actions (every 10 min) ───────────────────┐
      │  nfl.com/injuries  ─┐                                              │
      │  ESPN injuries JSON ─┼─► reconcile ─► report.json / alerts.json    │
      │  RotoWire (optional)─┘        │                                    │
      │                               ├─► flags.json     (irregularities)  │
      │  Mastodon ─┐                  ├─► scorecard.json (reporter scores) │
      │  Google News ┼─► claims ──────┘                                    │
      │  (Bluesky/Reddit probed, skipped when blocked)                     │
      │                               └─► health.json    (probe ledger)    │
      │                                        │ git commit data/          │
      └────────────────────────────────────────┼───────────────────────────┘
                                               ▼
                 GitHub Pages  ◄── docs/ + data/   (static, no backend)
                                               │
                 browser also tries ESPN + Bluesky directly (CORS permitting)
```

* **No backend, no secrets.** Every source used is keyless. The "server" is a
  scheduled workflow that commits JSON.
* **Standard library only** — no `pip install`, so no supply-chain step in CI.
* **Nothing is guessed.** Unmappable clubs are dropped, ambiguous player matches
  return no match, every disagreement becomes a flag with evidence links.

| Rank | Source | Wins |
|------|--------|------|
| 1 | nfl.com | The published designation. Only the league's Game Status Report is binding. |
| 2 | ESPN | Timestamps, injury detail, reporter attribution. |
| 3 | RotoWire | Lineup/inactive corroboration only. |

---

## 6. Reporter scoring

A **claim** is `(platform, author, player, club, predicted status, timestamp, url)`,
built automatically from two places: the beat writer ESPN names on each update,
and any social author posting an injury statement that matches a known player.

Ground truth is the official nfl.com designation for the same club + player.

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
| `PENDING` | claim younger than 6 h |

A reporter who says *"questionable"* about a player later **ruled out** scores
`WRONG`: the player did not play, so the availability call was wrong.

Guardrails against flattering small samples:

* No score published before **5 resolved claims**.
* Headline figure is the **Wilson 95% lower bound**, not the raw ratio.
* `score = 100 × accuracy × volume_confidence + lead_bonus × volume_confidence`,
  lead bonus saturating at a 4-hour lead.
* Below **60%** over ≥10 resolved claims → auto-`flagged`.
* At ≥80% over ≥5 → `established`; established Bluesky handles are added to the
  watch list automatically, so the feed tightens without manual curation.

---

## 7. Latency — what to actually expect

* **GitHub Actions cron floors at 5 minutes** and is best-effort; the schedule
  used is 10 minutes. The committed snapshot is therefore ~10 minutes old.
* The page also attempts a **direct browser fetch** of ESPN's injuries JSON and
  Bluesky search on load and every 30 s. On success the feed is effectively live
  and the header dot turns green with a `LIVE` tag on each item.
* Those calls depend on upstream **CORS**, which this project cannot promise. On
  failure the committed snapshot is used and the UI says so. It never claims to
  be live when it is not.

True sub-second push needs a persistent process (a Jetstream WebSocket consumer
for Bluesky plus a paid X feed). That is documented as the upgrade path rather
than faked inside a static site.

---

## 8. Running it

```bash
python3 -m unittest discover -s tests -t .   # 103 tests
python3 -m collectors.pipeline verify        # probe every source -> health.json
python3 -m collectors.pipeline collect       # fetch, reconcile, score, publish
python3 -m collectors.pipeline collect --with-rotowire
python3 -m collectors.pipeline bootstrap     # labelled sample data for UI preview
python3 -m collectors.pipeline status
python3 -m http.server 8000                  # preview at /docs/
```

---

## 9. Repository layout

```
collectors/
  http.py        stdlib fetch + per-source probe accounting
  models.py      canonical model, status vocabulary, provenance
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
data/state/      accumulated roster + reporter registry
data/archive/    per-run history, pruned after 14 days by CI
tests/           103 tests; fixtures reproduce shapes captured live
```

---

## 10. Verification log

| Check | Action | Result |
|-------|--------|--------|
| Unit + integration | `python3 -m unittest discover -s tests -t .` | **Ran 103 tests — OK** |
| Live collection | `collect.yml` on a GitHub runner, run 34539075886 | 823 players, 32/32 clubs, 0 source errors, 5 flags |
| Live probe ledger | `pipeline verify` in CI | 8 sources probed; 6 reachable, 2 documented failures (401 NFL API, 404 policy PDFs) |
| Live social probes | `probe_platforms()` in CI | Mastodon 200, Google News 200, **Bluesky 403**, **Reddit 403** |
| Parser correctness | `tests/test_nfl_com.py` | Correct club attribution across 4 tables, correct designations, correct provenance tagging |
| ESPN parser | `tests/test_espn.py` on verbatim live values | `Jeremiyah Love / ARI / QUESTIONABLE / ankle`, attribution `Dani Sureck` → `Cardinals' official site` |
| End-to-end | `tests/test_pipeline.py` (network stubbed) | All 7 JSON files written; official beats ESPN; second run diffs and emits a `cleared` alert; single-source outage exits 0; total outage exits 2 |
| Cross-source catch | Live run | `Byron Young` LAR (nfl.com) vs PHI (ESPN) flagged; independent table confirms LAR |
| Site serving | `python3 -m http.server` + `curl` | `/docs/` 200, `app.js` 200, `app.css` 200, `data/latest/*.json` 200 |

---

## 11. Legal / terms notes

* Designations are quoted from nfl.com and attributed to their sources; nothing is
  republished as this project's own reporting.
* RotoWire scraping may be restricted by their Terms of Use, so that source is
  **off by default** behind `--with-rotowire`.
* X, Instagram and Facebook are **not scraped** — excluded rather than worked around.
* Not affiliated with the NFL, ESPN or RotoWire.
