# Service Limitations & Platform Analysis

This document details the boundary conditions, platform restrictions, and engineering trade-offs of the Real-Time NFL & NBA Injury Alert Backend Service.

---

## 1. Source Availability & Platform Status

| Platform | Access Status | Cost | Mechanism | Limitations / Workarounds |
|---|---|---|---|---|
| **ESPN (Scoreboard, Summary, PBP, Injuries)** | ✅ **Fully open** | Free | Keyless REST JSON API | Subject to standard IP rate limits; throttled to 5s intervals per active game to ensure longevity. |
| **Bluesky Public AppView (`getAuthorFeed`)** | ✅ **Fully open** | Free | Keyless AT Protocol API | `searchPosts` returns HTTP 403; mitigated by directly polling verified national insiders via `getAuthorFeed`. |
| **Google News RSS** | ✅ **Fully open** | Free | Keyless XML RSS | News headlines lag social insider tweets by 5–15 minutes; used as secondary confirmation. |
| **Mastodon Public Timeline** | ✅ **Fully open** | Free | Keyless REST API | Lower signal-to-noise ratio; tagged with `#nfl` / `#nba` and filtered strictly by player entity match. |
| **Twitter / X** | ❌ **Blocked / Paid** | ~$0.005 / post | Deprecated free tiers | X closed free API access for new developers (Feb 2026). Scraping violates TOS and triggers CAPTCHAs. **Emits 1-click search deep links instead.** |
| **Instagram / Facebook** | ❌ **No public API** | N/A | Closed ecosystem | No free unauthenticated JSON or RSS feeds available. |
| **Reddit API** | ❌ **Blocked** | Paid | Unauthenticated endpoints return 403 | Reddit blocked unauthenticated `.json` requests from cloud IPs (Render, AWS, DigitalOcean). |

---

## 2. In-Depth Platform Constraints

### Twitter / X Blockade
- **Issue:** Prior to 2023, Twitter was the fastest medium for sports reporters like Ian Rapoport and Adam Schefter. In February 2026, X finalized the removal of all legacy free and low-cost developer tiers, moving to high-tier enterprise subscriptions or paid credits.
- **Mitigation:**
  1. Major insiders (Ian Rapoport, Tom Pelissero, Jordan Schultz, Jay Glazer, Dan Graziano, Shams Charania, Marc Stein) actively publish or syndicate breaking injury posts to **Bluesky** (`.bsky.social`).
  2. The service verifies the cryptographic DID / platform verification badge on Bluesky (`verification.verifiedStatus === 'valid'`).
  3. For players without Bluesky mentions, the service generates deep links to X search queries (`https://x.com/search?q=${player}+injury&f=live`) for manual verification.

### Bluesky `searchPosts` vs. `getAuthorFeed`
- **Issue:** Bluesky's unauthenticated global full-text search endpoint `app.bsky.feed.searchPosts` returns HTTP 403 Forbidden to automated scrapers and cloud IP addresses.
- **Mitigation:**
  - `app.bsky.feed.getAuthorFeed` remains 100% keyless and returns HTTP 200.
  - The collector maintains a curated roster of **14 NFL insiders** and **6 NBA reporters**, polling them every 10 seconds during active games.

---

## 3. Free Hosting Constraints

### Render Free Tier Web Service
- **Inactivity Sleep:** Render free tier instances sleep after 15 minutes of inactivity if no incoming HTTP requests arrive.
- **Mitigation:**
  - The main loop runs continuously while games are active.
  - GitHub Pages frontend polls `/api/alerts` every 2 seconds, which keeps the service hot throughout game hours.
  - An external cron or uptime ping (e.g. UptimeRobot or GitHub Actions) can ping `/api/health` every 10 minutes when no games are on.

### Supabase Free Tier PostgreSQL
- **Database Pause:** Supabase projects pause after 7 consecutive days of zero database reads or writes.
- **Mitigation:**
  - The service records collector health checks in table `health_check` on every loop cycle.
  - Automatic 30-day retention pruning cleans up old records without exceeding the 500MB storage limit of the free tier.

---

## 4. Linguistic & Entity Disambiguation Challenges

- **Common Names:** Players with common first/last names (e.g., "Love", "Brown", "Jones") require context verification against team abbreviations or positions.
- **Body Part vs. Idiom:** Phrases like *"took a shot to the pride"* or *"shot in the dark"* are filtered out by requiring explicit anatomical body parts or recognized status terminology (`OUT`, `QUESTIONABLE`, `EVALUATED`, `CARTED`).
- **Roster Axis vs. In-Game Axis:**
  - Roster axis (`OUT`, `DOUBTFUL`, `QUESTIONABLE`, `ACTIVE`) is published on official league timelines (Wed/Thu/Fri practice reports).
  - In-game axis (`OUT_FOR_GAME`, `QUESTIONABLE_TO_RETURN`, `INJURY_REPORTED`) occurs live and is parsed immediately from play descriptions and insider statements.
