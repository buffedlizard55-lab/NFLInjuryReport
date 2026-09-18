# NFL Injury Report

A live injury-alert system for all 32 NFL teams, built **only** from free, public,
verifiable sources, with a reporter-accuracy scorecard and a chat-style live feed.

Every designation shown on the site is the **official nfl.com** value whenever
nfl.com publishes one, and every row carries a link back to its source so a human
can check it in one click.

**Live site (GitHub Pages):** https://buffedlizard55-lab.github.io/NFLInjuryReport/

Pages is configured as *legacy* → branch `main` → path `/`, so the site is the
repository root (`index.html` + `assets/`) reading `data/latest/*.json`. There is
no build step. Data is refreshed every 10 minutes by
`.github/workflows/collect.yml`, which commits to the branch it runs on.

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

Re-probed on **2026-09-18**, after the missed-alert incident in section 4. These
are the endpoints the fix actually reads; every value below was captured verbatim
into `tests/fixtures/` so the parsers are tested against bytes that really came
back, not against a hand-written idea of the response.

| # | Source | Result observed 2026-09-18 | Used? |
|---|--------|---------------------------|-------|
| 15 | **ESPN scoreboard**<br>`…/nfl/scoreboard?dates=YYYYMMDD` | **HTTP 200**, keyless. `events[].competitions[0].status.type.state` = `pre \| in \| post`; event **401872932** *Detroit Lions at Buffalo Bills*, `date: 2026-09-18T00:15Z`, final BUF 41 – DET 31 (`status.type.state=post`). | ✅ Decides **which clubs are in a game window**, so per-player queries and insider feeds are aimed there |
| 16 | **Bluesky author feed**<br>`app.bsky.feed.getAuthorFeed?actor=…` | **HTTP 200, keyless** — while `app.bsky.feed.searchPosts` answers **403**. Returns post text, `createdAt` to the millisecond, and the platform's own verification verdict (`author.verification.verifiedStatus == "valid"`). | ✅ **The social fast path.** This is the endpoint the collector now reads for every watched insider |
| 17 | **ESPN news API**<br>`…/nfl/news?limit=N` | **HTTP 200**, ISO timestamps, deep links per story, `categories` naming club/athlete. **`?athlete=<id>` is silently ignored** (returns the same league feed), so per-player coverage comes from the wires below rather than from an ESPN filter. | ✅ League/team headlines wire |
| 18 | **RotoWire news RSS**<br>`https://www.rotowire.com/rss/news.php?sport=NFL` | **HTTP 200**, keyless, 5 recent items; per-player lines with 12-hour PDT timestamps (*"…Lions RB Jameson Williams caught 2 of 3 targets for 33 yards Thursday…"*, pubDate `Thu, 17 Sep 2026 9:54:00 PM PDT`). Dates are **not** RFC-822-clean, so the parser handles them with a strict regex first (see the verification log). | ✅ Headline wire (`--no-news` disables it); the paywalled lineups page stays off by default |
| 19 | **Google News per-player query**<br>`news.google.com/rss/search?q="Keon Coleman" injury when:1d` | **HTTP 200**. Returned the two headlines that documented the missed injury: *"Keon Coleman injury update: Bills WR hurt vs. Lions"* (aol.com, `Fri, 18 Sep 2026 01:39:00 GMT`) and *"…returns after getting hurt vs. Lions"* (Democrat and Chronicle, `01:47:00 GMT`). | ✅ Queried for the players in a live game window, so a headline that beats the official report is captured rather than waiting for the roster feed |

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

## 3. Four defects the live run exposed (and how they were fixed)

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

**4. Reporter lead times were nonsense.** Mean **909 minutes**, maximum **19
days**, with 252 of 302 values exactly zero. The cause: nfl.com publishes no
per-row publication time, so those rows are stamped with the *game date at
00:00:00Z*, and lead time was being measured against that synthetic instant. Lead
time is now measured against the first pipeline run that actually observed the
designation (`data/state/first_seen.json`), and is **null** until two runs have
accumulated history for that player and status. It is never inferred from a
source timestamp.

The blank-designation inference is itself justified by the policy: a club is told
that a player "not injured but has been rested in practice should not be listed
on the Game Status Report with an injury status designation (Out, Doubtful, or
Questionable)" while still appearing on the Practice Report. Those rows are
labelled `inferred*` in the UI so they are never mistaken for a published
designation.

---

## 4. Incident — 2026-09-17: two in-game injuries, no alert

A user watched the Bills–Lions Thursday game, saw **Keon Coleman** and **DJ
Moore** get hurt, and received no alert for either. This is what the archived
data says happened, with the source for every line.

| Time (UTC) | What is on the record | Where it came from |
|---|---|---|
| 2026-09-18 00:15 | Kickoff, DET @ BUF (ESPN event 401872932; final BUF 41 – DET 31) | ESPN scoreboard API, captured verbatim |
| 01:37:12 | *"Bills WR DJ Moore, who landed hard on his shoulder, is questionable to return with a shoulder injury and a stinger. He was taken into the locker room for X-rays."* — Ian Rapoport | `getAuthorFeed` for `rapsheet.bsky.social`, captured verbatim |
| 01:39–01:47 | *"Keon Coleman injury update: Bills WR hurt vs. Lions"* (aol.com); *"…returns after getting hurt vs. Lions"* (Democrat and Chronicle) | Google News RSS per-player query, captured verbatim |
| 02:12 | ESPN's injuries feed carries: *"Moore (shoulder) has been ruled out for the remainder of Thursday night's game against the Lions."* Status field: `Questionable` | `data/latest/report.json`, committed 05:51:44Z |
| 02:24:21 | *"Bills Standout WR DJ Moore Departs Game vs. Lions (Shoulder, Stinger), Ruled Out"* — already inside our own snapshot | `data/latest/social.json` (Mastodon) |
| 05:51:44 | The **first snapshot after the game**. Only then did a Moore alert exist, and it read `ACTIVE → QUESTIONABLE` | `data/latest/alerts.json`, `data/archive/2026-09-18/` |

Three independent failures, each measured rather than assumed:

**1. The collector did not run during the game.** The cron said every 10 minutes.
The archive shows **two runs** that night (`report-0100`, `report-0551`) and, over
the eight days to 2026-09-18, a **median gap of 188 minutes** and a **worst gap of
412 minutes** across 57 runs. GitHub throttles and queues scheduled workflows, and
a job whose only output is a data commit sits at the back of the queue. The
injuries were reported at 01:37–01:47; the next run was 05:51.

**2. The only working social path was switched off.** `data/state/watched.json`
contained `{"handles": []}` — the collector followed nobody — and the Bluesky
adapter's only path was `searchPosts`, which answers **403** (re-measured
2026-09-18 from the CI runner *and* from a second client IP). Rapoport's post sat
behind `getAuthorFeed`, which is keyless and returns **200**. The project had
already verified that account's platform badge; it simply never read it.

**3. The product had one axis.** Alerts were produced only by diffing roster
designations. Keon Coleman is `ACTIVE` in every roster feed — he finished with six
catches for 63 yards — so *no possible diff* could fire for him, even though two
headlines described him hurt and returning. "Can this player still play tonight?"
was not a question the system could ask, and "ruled out for the remainder of the
game" was stored as `QUESTIONABLE` because that is what ESPN's status field said.

### What changed

* **`collectors/ingame.py`** (new) — an in-game vocabulary kept separate from the
  roster vocabulary: `OUT_FOR_GAME`, `RETURN_QUESTIONABLE`, `RETURNED`,
  `EVALUATED`, `INJURY_REPORTED`. It classifies the sources' own sentences, keeps
  the verbatim sentence as evidence, and merges reports about the same player so
  a later "returned" beats an earlier "questionable to return".
* **Verified insider feeds are read every run.** `directory.watched_handles()`
  derives the handles from evidence the project already holds — the platform's own
  verification badge for `rapsheet.bsky.social`, plus seed-declared candidates
  that are labelled *unverified* wherever they appear. `data/state/watched.json`
  is rewritten with the result, so the empty list cannot recur silently.
* **Game-window targeting.** The scoreboard decides which clubs are playing (or
  about to play); per-player Google News queries and the wires are aimed at those
  clubs, and a warm-up injury is covered too (Ed Oliver, 23:10:58Z, one hour
  before kickoff).
* **In-game alerts are durable.** They are timestamped at the moment the source
  published and kept in an append-only log (`alerts.json` → `log`) for 72 hours,
  so an alert raised at 01:37 is still on the page the next morning. The site
  renders them on their own axis, above the roster diffs, and raises a desktop
  notification (or banner) when a new one arrives.
* **The schedule is honest.** Four staggered cron entries at the 5-minute floor,
  plus **game-day self-dispatch**: while a game is in progress the run re-arms
  itself via `workflow_dispatch` (depth-capped at 12), and every run measures the
  cadence it actually achieved from the archive and publishes it in
  `meta.json` — with a `COLLECTOR_CADENCE_DEGRADED` flag whenever a gap exceeds
  60 minutes.

### What is still not solved

* X/Twitter remains link-out only; there is no free read path, so "no tweets
  about the injury" cannot become "we read the tweets". Bluesky, Mastodon and the
  news wires are read instead, and every claim says which platform it came from.
* GitHub's scheduler remains best-effort even with chaining; the site shows the
  measured cadence rather than a promise. Sub-minute latency needs a persistent
  process (a Jetstream WebSocket consumer for Bluesky), which this static
  pipeline does not have.
* Roster designations still come from nfl.com/ESPN; in-game events are a
  *reporting* trail, not a league status, and are labelled that way.

---

## 5. Irregularity catalogue

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

## 6. Architecture

```
      ┌─────────────────── GitHub Actions (every 10 min) ───────────────────┐
      │  nfl.com/injuries  ─┐                                              │
      │  ESPN injuries JSON ─┼─► reconcile ─► report.json / alerts.json    │
      │  RotoWire (optional)─┘        │                                    │
      │                               ├─► flags.json     (irregularities)  │
      │  Mastodon ─┐                  ├─► scorecard.json (reporter scores) │
      │  Google News ┼─► claims ──────┤                                    │
      │  (Bluesky/Reddit probed,     ├─► directory.json (verified reporter │
      │   skipped when blocked)      │     & official-source directory)    │
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

## 7. Reporter scoring

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

**Two caveats that change how the numbers read** (both published in
`scorecard.json` and shown on the site):

* `espn-attribution` claims are **not independent predictions**. ESPN names the
  beat writer inside the same update whose status is being checked, so their
  accuracy measures whether the reporter's characterisation matched the filed
  designation — not whether they beat anyone to it. On the first live run
  **301 of 303** claims were of this type. Read the social-platform rows for
  prediction value.
* Lead time is null until history accumulates (see defect 4 above).

The summary is therefore broken down `by_platform` rather than reported as one
blended figure.

Guardrails against flattering small samples:

* No score published before **5 resolved claims**.
* Headline figure is the **Wilson 95% lower bound**, not the raw ratio.
* `score = 100 × accuracy × volume_confidence + lead_bonus × volume_confidence`,
  lead bonus saturating at a 4-hour lead.
* Below **60%** over ≥10 resolved claims → auto-`flagged`.
* At ≥80% over ≥5 → `established`; established Bluesky handles are added to the
  watch list automatically, so the feed tightens without manual curation.

---

## 8. Verified reporter & source directory — `directory.html`

A subpage (linked from the main tab bar) listing **everyone who can verify an
injury live**, in three tiers. Every row carries the link it was derived from
so a human can check it in one click; nothing is a guessed handle.

1. **League & 32 clubs.** The binding nfl.com injury report, NFL Communications
   and, per club, the official website plus X / Facebook / Instagram /
   Snapchat. These are **parsed out of each club's own nfl.com team page** —
   the league-published social directory — by a layout-agnostic anchor parser
   (`collectors/directory.py`). Parsed rows are cached
   (`data/state/team_directory.json`) and re-fetched live every 7 days
   (`--force-refresh-teams` for immediately). A page that fails fetching is
   shown as “probe pending” with the error; the slot is never filled by a
   guess. (Washington's page listed no Snapchat, so none is shown.) The
   initial cache was populated from nfl.com team pages fetched and reviewed
   2026-09-10; its provenance note is inside the state file and rendered on
   the page's “How this is verified” tab.
2. **National insiders.** A deliberately small, explicit, diff-able seed
   (`collectors/directory_seed.json`, 14 names). Social accounts are
   **re-verified on every build**, not trusted from the seed:
   * Bluesky is queried through the keyless public AppView
     (`app.bsky.actor.getProfile` / `app.bsky.actor.searchActors`; the older
     `feed.searchActors` path was renamed and now returns
     `MethodNotImplemented`). A green **verified** badge requires an exact
     display-name match **and** `verification.verifiedStatus == "valid"`. An
     exact name match without the platform checkmark is a yellow
     **review** candidate — never called verified. Accounts with Bluesky's
     own `impersonation`/`parody` moderation labels, `.mirrors.bot`/
     `.bluesky.bot` handles, or self-described mirror/parody/bot bios are
     rejected automatically and listed on the **Fraud warnings** tab
     (observed live 2026-09-10: every exact-name Bluesky result for Adam
     Schefter is a mirror, a parody, or carries Bluesky's `impersonation`
     label; Ian Rapoport's `rapsheet.bsky.social` is genuinely
     Bluesky-verified). Cached 24h in `data/state/directory_verification.json`.
   * **X cannot be verified for free in 2026.** `publish.twitter.com/oembed`
     returned HTTP 403 and the syndication widget an empty body on
     2026-09-10; both are re-probed every run (and listed in
     `health.json` / the source ledger) so if a keyless route works again the
     badge upgrades automatically. Until then every X entry is explicitly a
     one-click manual-review link, never labelled machine-verified.
3. **Beat writers (per club).** Aggregated **only from bylines actually
   printed in the live ESPN injury feed** (≥2 attributed updates): name and
   outlet verbatim, the clubs whose players they are quoted on, update count,
   latest timestamp, and the ESPN player-page evidence URLs. **No social
   handles are asserted** for beat writers; rows provide exact-name X and
   Google News *search* deep links so the genuine account can be confirmed by
   hand. National seed names appearing in the feed are excluded (they live on
   tier 2). Writers attributed across multiple clubs raise a
   `DIRECTORY_CROSS_TEAM_BEAT` low-severity flag (usually shared-wire
   attribution — kept as observed, never reassigned); near-identical names
   raise `DIRECTORY_NAME_VARIANT`.

CLI: `python3 -m collectors.directory build [--offline] [--force-refresh-teams]`.
It also runs inside `pipeline collect` (failures are recorded as a source
error and never abort the injury snapshot). Output:
`data/latest/directory.json`, covered by `tests/test_directory.py`.

---

## 9. Latency — what to actually expect

* **GitHub Actions cron floors at 5 minutes** and is best-effort. The workflow
  now carries **four staggered cron entries** instead of one, and while a game is
  in progress it **re-arms itself** with a `workflow_dispatch` call (depth-capped
  at 12, so a stuck game cannot loop forever). `workflow_dispatch` is the
  documented exception to the usual "GITHUB_TOKEN cannot trigger workflows" rule;
  the job asks for `actions: write` to do it.
* **Measured, not promised.** Every run computes the cadence it actually achieved
  from `data/archive/` (runs in the last 24 h, median and worst gap, time since
  the previous run) and publishes it in `meta.json`; a gap over 60 minutes raises
  `COLLECTOR_CADENCE_DEGRADED` on the Flags tab. Before this change the measured
  median gap was **188 minutes** against a 10-minute schedule (section 4).
* The page also attempts **direct browser fetches** of ESPN's injuries JSON and
  the Bluesky author feeds of the verified directory on load and every 30 s. On
  success the feed is fresher than the last CI run and the header dot turns green
  with a `LIVE` tag on each item. `searchPosts` is deliberately not called: it
  answered 403 on every probe.
* Those calls depend on upstream **CORS**, which this project cannot promise. On
  failure the committed snapshot is used and the UI says so. It never claims to
  be live when it is not.
* In-game alerts are the one thing that does not depend on the schedule being on
  time: they are timestamped at the source's publication instant and kept for
  72 hours, so a late run still reports the honest times (`reported_at` vs
  `first_seen_at`, and the difference between them, are both on every event).

True sub-second push needs a persistent process (a Jetstream WebSocket consumer
for Bluesky plus a paid X feed). That is documented as the upgrade path rather
than faked inside a static site.

---

## 10. Running it

```bash
python3 -m unittest discover -s tests -t .   # 182 tests
python3 -m collectors.pipeline verify        # probe every source -> health.json
python3 -m collectors.pipeline collect       # fetch, reconcile, score, publish
python3 -m collectors.pipeline collect --with-rotowire
python3 -m collectors.pipeline bootstrap     # labelled sample data for UI preview
python3 -m collectors.pipeline status
python3 -m http.server 8000                  # preview at http://localhost:8000/
```

---

## 11. Repository layout

```
collectors/
  http.py        stdlib fetch + per-source probe accounting
  models.py      canonical model, status vocabulary, provenance
  nfl_com.py     official nfl.com report parser (class-name agnostic)
  espn.py        ESPN injuries JSON parser + attribution extractor
                 (also scoreboard + news API collectors)
  ingame.py      in-game availability vocabulary: out / questionable to
                 return / returned, event merge, verbatim evidence
  rotowire.py    reverse-engineered lineups parser + news RSS (optional)
  matching.py    club+name player index (ambiguous matches return nothing)
  reporters.py   reporter registry, Wilson scoring, auto-tiering
  reconcile.py   precedence merge, alerts, irregularity detection
  scoring.py     claim building and resolution against the official report
  social.py      Bluesky / Mastodon / Google News / Reddit adapters + X links
  directory.py   official/national/beat directory builder + live verification
  directory_seed.json  explicit, reviewable seed of 14 national insiders
  pipeline.py    CLI: collect | verify | bootstrap | status
index.html       GitHub Pages site root (plain HTML/CSS/JS, no build step)
directory.html   reporter & official-source directory subpage
assets/          app.css + app.js + directory.js
data/latest/     committed snapshot the site reads (incl. directory.json and
                 ingame.json — in-game events, hot clubs, watched handles)
data/state/      roster, reporter registry, parsed club-directory and
                 social-verification caches (7-day / 24-hour TTLs)
data/archive/    per-run history, pruned after 14 days by CI
tests/           182 tests; fixtures reproduce shapes captured live
                 (the 2026-09-18 captures are verbatim, incl. the
                 Bluesky author feed and the ESPN scoreboard payload)
```

---

## 12. Verification log

| Check | Action | Result |
|-------|--------|--------|
| Unit + integration | `python3 -m unittest discover -s tests -t .` | **Ran 182 tests — OK** (2026-09-18; adds in-game classification, alert-log persistence and pruning, ESPN news + RotoWire RSS wire parsers, the mocked Bluesky author-feed path, and the end-to-end regression for the 2026-09-17 miss) |
| Club social directory | fetch of all 32 `nfl.com/teams/<slug>/` pages, 2026-09-10 | All 32 fetched and reviewed; official sites + X/FB/IG/Snap handles recorded (Washington lists no Snapchat); parsed cache refreshed weekly by CI |
| Bluesky identity verification | public AppView `getProfile`/`searchActors`, 2026-09-10 | Rapoport `rapsheet.bsky.social` verified (`verifiedStatus=valid`); Pelissero/Schultz/Glazer exact-name candidates without badges; Schefter search returns only mirrors/parodies and two accounts Bluesky itself labels `impersonation` → Fraud warnings |
| X keyless verification | `publish.twitter.com/oembed` + syndication widget | HTTP 403 / empty body 2026-09-10 → X stays one-click manual-review; re-probed every build and auto-upgraded if a free route returns |
| Directory page serving | `python3 -m http.server` + `curl` | `directory.html`, `assets/directory.js`, `data/latest/directory.json` all 200 |
| Live collection | `collect.yml` on a GitHub runner, run 34539075886 | 823 players, 32/32 clubs, 0 source errors, 5 flags |
| Live probe ledger | `pipeline verify` in CI | 8 sources probed; 6 reachable, 2 documented failures (401 NFL API, 404 policy PDFs) |
| Live social probes | `probe_platforms()` in CI | Mastodon 200, Google News 200, **Bluesky search 403**, **Reddit 403** |
| Bluesky author feed | `getAuthorFeed` for the verified directory, 2026-09-18 | **200, keyless**; three Rapoport posts captured verbatim, including the DJ Moore shoulder post at `01:37:12.807Z` that the 2026-09-17 run never saw |
| Verbatim fixtures | 4 new captures 2026-09-18 | `bsky_author_feed.json`, `espn_news.json`, `espn_scoreboard.json`, `rotowire_news.xml` — the parsers are tested against bytes that really came back |
| RotoWire RSS dates | `tests/test_news_wires.py` | `"Thu, 17 Sep 2026 9:54:00 PM PDT"` → `2026-09-18T04:54:00Z`. Caught a real bug: `email.utils` read the non-padded 12-hour clock as AM, so the strict regex now runs first |
| Parser correctness | `tests/test_nfl_com.py` | Correct club attribution across 4 tables, correct designations, correct provenance tagging |
| ESPN parser | `tests/test_espn.py` on verbatim live values | `Jeremiyah Love / ARI / QUESTIONABLE / ankle`, attribution `Dani Sureck` → `Cardinals' official site` |
| End-to-end | `tests/test_pipeline.py` (network stubbed) | All 7 JSON files written; official beats ESPN; second run diffs and emits a `cleared` alert; single-source outage exits 0; total outage exits 2 |
| Cross-source catch | Live run | `Byron Young` LAR (nfl.com) vs PHI (ESPN) flagged; independent table confirms LAR |
| Site serving | `python3 -m http.server` + `curl` | `/` 200, `assets/app.js` 200, `assets/app.css` 200, `data/latest/*.json` 200 |
| Live deployment | `gh api …/pages/builds/latest` | `status: built` at commit `d9ceaf2` = `main` HEAD |
| Scorecard on live data | first live run | 145 reporters auto-discovered from ESPN attribution, 303 claims, 302 resolved, 94.7% blended accuracy — no manual curation |

---

## 13. Legal / terms notes

* Designations are quoted from nfl.com and attributed to their sources; nothing is
  republished as this project's own reporting.
* RotoWire scraping may be restricted by their Terms of Use, so that source is
  **off by default** behind `--with-rotowire`.
* X, Instagram and Facebook are **not scraped** — excluded rather than worked around.
* Not affiliated with the NFL, ESPN or RotoWire.
