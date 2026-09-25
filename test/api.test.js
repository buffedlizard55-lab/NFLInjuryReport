'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { startServer, server, stopMainLoop } = require('../server');
const { db } = require('../db');

const TEST_PORT = 3388;

test('API endpoints: /api/alerts and /api/health return 200 and schema compliant bodies', async (t) => {
  await startServer(TEST_PORT);

  // Insert a test alert
  await db.insertAlert({
    sport: 'nfl',
    team: 'KC',
    player_name: 'Patrick Mahomes',
    status: 'QUESTIONABLE_TO_RETURN',
    source: 'play-by-play',
    timestamp_source: new Date().toISOString(),
    timestamp_first_seen: new Date().toISOString(),
    latency_ms: 1500,
    verbatim_text: 'Patrick Mahomes is questionable to return.',
    source_url: 'https://espn.com',
    verified: true,
    game_id: '401872932'
  });

  await db.upsertGame({
    sport: 'nfl',
    game_id: '9999999',
    club_1: 'DET',
    club_2: 'KC',
    state: 'in'
  });

  t.after(() => {
    stopMainLoop();
    server.close();
  });

  // Test /api/health
  const healthRes = await fetch(`http://127.0.0.1:${TEST_PORT}/api/health`);
  assert.strictEqual(healthRes.status, 200);
  assert.strictEqual(healthRes.headers.get('access-control-allow-origin'), '*');

  const healthData = await healthRes.json();
  assert.strictEqual(typeof healthData.uptime_seconds, 'number');
  assert.ok(healthData.alerts_total >= 1);
  assert.strictEqual(healthData.last_alert.player_name, 'Patrick Mahomes');
  assert.strictEqual(healthData.db_connection, 'connected');

  // Test /api/alerts
  const alertsRes = await fetch(`http://127.0.0.1:${TEST_PORT}/api/alerts?sport=nfl&team=KC&limit=10`);
  assert.strictEqual(alertsRes.status, 200);
  assert.strictEqual(alertsRes.headers.get('access-control-allow-origin'), '*');

  const alertsData = await alertsRes.json();
  assert.ok(Array.isArray(alertsData.alerts));
  assert.strictEqual(alertsData.alerts[0].player_name, 'Patrick Mahomes');
  assert.strictEqual(alertsData.alerts[0].team, 'KC');

  // Verify game_window
  assert.ok(alertsData.game_window);
  assert.ok(Array.isArray(alertsData.game_window.active_games));
  assert.ok(Array.isArray(alertsData.game_window.active_clubs));
  assert.ok(alertsData.game_window.active_clubs.includes('KC'));

  // Verify collector_status
  assert.ok(alertsData.collector_status.espn);
  assert.ok(alertsData.collector_status.bluesky);
  assert.ok(alertsData.collector_status['google-news']);
  assert.ok(alertsData.collector_status.mastodon);
});
