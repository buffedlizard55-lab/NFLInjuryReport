'use strict';

const { db } = require('../db');

/**
 * Handler for GET /api/alerts
 */
async function handleAlerts(req, res, urlObj, collectorState = {}) {
  const sport = urlObj.searchParams.get('sport') || null;
  const team = urlObj.searchParams.get('team') || null;
  const limit = parseInt(urlObj.searchParams.get('limit') || '20', 10);

  const alerts = await db.getAlerts({ sport, team, limit });
  const activeGames = await db.getActiveGames(sport);

  const activeClubsSet = new Set();
  for (const g of activeGames) {
    if (g.club_1) activeClubsSet.add(g.club_1);
    if (g.club_2) activeClubsSet.add(g.club_2);
  }

  const dbHealthChecks = await db.getHealthChecks();
  const collectorStatus = {};

  const collectors = ['espn', 'bluesky', 'google-news', 'mastodon'];
  for (const name of collectors) {
    if (dbHealthChecks[name]) {
      collectorStatus[name] = {
        status: dbHealthChecks[name].status,
        last_run: dbHealthChecks[name].last_run,
        error_msg: dbHealthChecks[name].error_msg || null
      };
    } else if (collectorState[name]) {
      collectorStatus[name] = {
        status: collectorState[name].lastStatus || 'initialized',
        last_run: collectorState[name].lastRun || null,
        error_msg: collectorState[name].lastError || null
      };
    } else {
      collectorStatus[name] = {
        status: 'initialized',
        last_run: null,
        error_msg: null
      };
    }
  }

  const payload = {
    alerts,
    game_window: {
      active_games: activeGames,
      active_clubs: Array.from(activeClubsSet)
    },
    collector_status: collectorStatus
  };

  res.writeHead(200, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'no-cache, no-store, must-revalidate'
  });
  res.end(JSON.stringify(payload, null, 2));
}

module.exports = {
  handleAlerts
};
