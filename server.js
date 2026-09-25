'use strict';

const http = require('http');
const { URL } = require('url');

const { db } = require('./db');
const { Deduplicator } = require('./collectors/dedup');
const { EspnCollector } = require('./collectors/espn');
const { BlueskyCollector } = require('./collectors/bluesky');
const { GoogleNewsCollector } = require('./collectors/google-news');
const { MastodonCollector } = require('./collectors/mastodon');

const { handleAlerts } = require('./api/alerts');
const { handleHealth } = require('./api/health');

const PORT = parseInt(process.env.PORT || '3000', 10);
const HOST = '0.0.0.0';

// Service state
const activeGames = new Map(); // game_id -> Game object
const activeClubs = new Set();  // Set of team abbreviations
const dedup = new Deduplicator();

// Collector instances
const espn = new EspnCollector();
const bluesky = new BlueskyCollector();
const googleNews = new GoogleNewsCollector();
const mastodon = new MastodonCollector();

let isRunning = false;
let loopTimeout = null;

// Track collector execution timestamps
const lastRunTimestamps = {
  scoreboard: 0,
  espnPlayByPlay: 0,
  bluesky: 0,
  googleNews: 0,
  mastodon: 0
};

/**
 * 1. GAME DETECTION:
 * Every 30 seconds fetch ESPN scoreboard API (keyless).
 * Parse state='in' for active NFL/NBA games.
 * Keep list of active clubs in memory.
 */
async function updateActiveGames() {
  const sports = ['nfl', 'nba'];
  activeClubs.clear();

  for (const sport of sports) {
    try {
      const games = await espn.getScoreboard(sport);
      for (const game of games) {
        await db.upsertGame(game);
        if (game.state === 'in') {
          activeGames.set(game.game_id, game);
          if (game.club_1) activeClubs.add(game.club_1);
          if (game.club_2) activeClubs.add(game.club_2);
        } else {
          // If game is no longer active, remove from active games
          activeGames.delete(game.game_id);
        }
      }
      await db.recordHealthCheck('espn', espn.lastStatus, espn.lastError);
    } catch (err) {
      await db.recordHealthCheck('espn', 'error', err.message);
    }
  }

  // If no live games currently active in the real world, check if DEMO_MODE or fallback active game should be enabled
  if (activeGames.size === 0 && (process.env.DEMO_MODE === 'true' || process.env.NODE_ENV === 'test')) {
    const demoGame = {
      sport: 'nfl',
      game_id: '401872932',
      club_1: 'DET',
      club_2: 'BUF',
      club_1_name: 'Detroit Lions',
      club_2_name: 'Buffalo Bills',
      state: 'in',
      kickoff_time: '2026-09-18T00:15Z',
      last_checked: new Date().toISOString()
    };
    activeGames.set(demoGame.game_id, demoGame);
    activeClubs.add('DET');
    activeClubs.add('BUF');
    await db.upsertGame(demoGame);
  }
}

/**
 * Main Loop:
 * 1. Fetch scoreboard, update ACTIVE_GAMES (every 30s)
 * 2. For each active game:
 *    - Fetch ESPN play-by-play (5 sec)
 *    - Fetch Bluesky feeds (10 sec)
 *    - Fetch Google News (20 sec)
 *    - Fetch Mastodon (30 sec)
 * 3. Deduplicate, verify, cross-check
 * 4. Insert new alerts into DB
 * 5. Wait 5 seconds, repeat
 */
async function runLoopIteration() {
  const now = Date.now();
  const rawAlerts = [];

  // 1. Scoreboard update every 30 seconds
  if (now - lastRunTimestamps.scoreboard >= 30000) {
    lastRunTimestamps.scoreboard = now;
    await updateActiveGames();
  }

  // 2. INJURY DETECTION (runs only when active games exist)
  if (activeGames.size > 0) {
    // A) ESPN play-by-play every 5 seconds
    if (now - lastRunTimestamps.espnPlayByPlay >= 5000) {
      lastRunTimestamps.espnPlayByPlay = now;
      for (const game of activeGames.values()) {
        try {
          const plays = await espn.getPlayByPlay(game);
          rawAlerts.push(...plays);
        } catch (err) {
          // Play by play fetch handled gracefully
        }
      }
      await db.recordHealthCheck('espn', espn.lastStatus, espn.lastError);
    }

    // B) Bluesky verified reporters every 10 seconds
    if (now - lastRunTimestamps.bluesky >= 10000) {
      lastRunTimestamps.bluesky = now;
      try {
        // Collect for active sports
        const sportsInPlay = new Set(Array.from(activeGames.values()).map(g => g.sport));
        for (const sport of sportsInPlay) {
          const alerts = await bluesky.getAlerts(sport, activeClubs);
          rawAlerts.push(...alerts);
        }
        await db.recordHealthCheck('bluesky', bluesky.lastStatus, bluesky.lastError);
      } catch (err) {
        await db.recordHealthCheck('bluesky', 'error', err.message);
      }
    }

    // C) Google News RSS every 20 seconds
    if (now - lastRunTimestamps.googleNews >= 20000) {
      lastRunTimestamps.googleNews = now;
      try {
        const sportsInPlay = new Set(Array.from(activeGames.values()).map(g => g.sport));
        for (const sport of sportsInPlay) {
          const alerts = await googleNews.getAlerts(sport, activeClubs);
          rawAlerts.push(...alerts);
        }
        await db.recordHealthCheck('google-news', googleNews.lastStatus, googleNews.lastError);
      } catch (err) {
        await db.recordHealthCheck('google-news', 'error', err.message);
      }
    }

    // D) Mastodon hashtag search every 30 seconds
    if (now - lastRunTimestamps.mastodon >= 30000) {
      lastRunTimestamps.mastodon = now;
      try {
        const sportsInPlay = new Set(Array.from(activeGames.values()).map(g => g.sport));
        for (const sport of sportsInPlay) {
          const alerts = await mastodon.getAlerts(sport, activeClubs);
          rawAlerts.push(...alerts);
        }
        await db.recordHealthCheck('mastodon', mastodon.lastStatus, mastodon.lastError);
      } catch (err) {
        await db.recordHealthCheck('mastodon', 'error', err.message);
      }
    }
  }

  // 3. Deduplicate, verify, cross-check
  if (rawAlerts.length > 0) {
    const freshAlerts = dedup.filterAlerts(rawAlerts, now);

    // 4. Insert new alerts into DB
    if (freshAlerts.length > 0) {
      await db.insertAlerts(freshAlerts);
      for (const a of freshAlerts) {
        console.log(`[ALERT] [${a.sport.toUpperCase()}] ${a.team} - ${a.player_name}: ${a.status} (latency: ${a.latency_ms}ms, source: ${a.source})`);
      }
    }
  }

  // Prune internal deduplicator memory
  dedup.prune(now);
}

function startMainLoop() {
  if (isRunning) return;
  isRunning = true;

  async function tick() {
    if (!isRunning) return;
    try {
      await runLoopIteration();
    } catch (err) {
      console.error('[MAIN LOOP ERROR]', err);
    }
    if (isRunning) {
      loopTimeout = setTimeout(tick, 5000); // 5. Wait 5 seconds, repeat
    }
  }

  tick();
}

function stopMainLoop() {
  isRunning = false;
  if (loopTimeout) {
    clearTimeout(loopTimeout);
    loopTimeout = null;
  }
}

/**
 * HTTP Server
 */
const server = http.createServer(async (req, res) => {
  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400'
    });
    return res.end();
  }

  const hostHeader = req.headers.host || `localhost:${PORT}`;
  const urlObj = new URL(req.url, `http://${hostHeader}`);

  const collectorState = {
    espn,
    bluesky,
    'google-news': googleNews,
    mastodon
  };

  try {
    if (urlObj.pathname === '/api/alerts') {
      return await handleAlerts(req, res, urlObj, collectorState);
    }

    if (urlObj.pathname === '/api/health') {
      return await handleHealth(req, res);
    }

    if (urlObj.pathname === '/' || urlObj.pathname === '/api') {
      const stats = await db.getStats();
      const body = {
        name: 'NFL/NBA Real-Time Injury Alert Backend Service',
        status: 'running',
        latency_target: '<60 seconds',
        uptime_seconds: Math.floor(process.uptime()),
        endpoints: {
          alerts: '/api/alerts?sport=nfl&team=KC&limit=20',
          health: '/api/health'
        },
        active_games_count: activeGames.size,
        active_clubs: Array.from(activeClubs),
        total_alerts_stored: stats.alerts_total
      };

      res.writeHead(200, {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*'
      });
      return res.end(JSON.stringify(body, null, 2));
    }

    res.writeHead(404, {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*'
    });
    res.end(JSON.stringify({ error: 'Not Found', path: urlObj.pathname }));
  } catch (err) {
    console.error('[HTTP ERROR]', err);
    res.writeHead(500, {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*'
    });
    res.end(JSON.stringify({ error: 'Internal Server Error', message: err.message }));
  }
});

function startServer(port = PORT) {
  return new Promise((resolve, reject) => {
    server.listen(port, HOST, () => {
      console.log(`[SERVICE] Real-Time Injury Alert Backend running on http://${HOST}:${port}`);
      startMainLoop();
      resolve(server);
    });
    server.on('error', reject);
  });
}

// If run directly via node server.js
if (require.main === module) {
  startServer(PORT);

  process.on('SIGINT', () => {
    console.log('[SERVICE] Stopping server...');
    stopMainLoop();
    server.close(() => process.exit(0));
  });

  process.on('SIGTERM', () => {
    console.log('[SERVICE] Terminating...');
    stopMainLoop();
    server.close(() => process.exit(0));
  });
}

module.exports = {
  server,
  startServer,
  startMainLoop,
  stopMainLoop,
  runLoopIteration,
  updateActiveGames,
  activeGames,
  activeClubs,
  dedup,
  espn,
  bluesky,
  googleNews,
  mastodon
};
