'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { Deduplicator } = require('../collectors/dedup');

test('Deduplication skips identical alert within 5 minutes', () => {
  const dedup = new Deduplicator();
  const now = Date.now();

  const alert1 = {
    sport: 'nfl',
    team: 'KC',
    player_name: 'Patrick Mahomes',
    status: 'INJURY_REPORTED',
    timestamp_source: new Date(now - 5000).toISOString(),
    timestamp_first_seen: new Date(now).toISOString()
  };

  const res1 = dedup.evaluate(alert1, now);
  assert.strictEqual(res1.emit, true);

  const res2 = dedup.evaluate(alert1, now + 60000); // 1 minute later
  assert.strictEqual(res2.emit, false);
  assert.strictEqual(res2.reason, 'duplicate_status_within_5min');
});

test('Deduplication allows status upgrade (REPORTED -> OUT)', () => {
  const dedup = new Deduplicator();
  const now = Date.now();

  const alertReported = {
    sport: 'nfl',
    team: 'KC',
    player_name: 'Patrick Mahomes',
    status: 'INJURY_REPORTED',
    timestamp_source: new Date(now - 10000).toISOString(),
    timestamp_first_seen: new Date(now).toISOString()
  };

  const alertOut = {
    sport: 'nfl',
    team: 'KC',
    player_name: 'Patrick Mahomes',
    status: 'OUT_FOR_GAME',
    timestamp_source: new Date(now - 5000).toISOString(),
    timestamp_first_seen: new Date(now + 20000).toISOString()
  };

  const res1 = dedup.evaluate(alertReported, now);
  assert.strictEqual(res1.emit, true);

  const res2 = dedup.evaluate(alertOut, now + 20000);
  assert.strictEqual(res2.emit, true);
  assert.strictEqual(res2.reason, 'status_upgrade');
});

test('Deduplication discards stale posts > 30 minutes old', () => {
  const dedup = new Deduplicator();
  const now = Date.now();

  const staleAlert = {
    sport: 'nfl',
    team: 'KC',
    player_name: 'Travis Kelce',
    status: 'OUT_FOR_GAME',
    timestamp_source: new Date(now - 35 * 60 * 1000).toISOString(),
    timestamp_first_seen: new Date(now).toISOString()
  };

  const res = dedup.evaluate(staleAlert, now);
  assert.strictEqual(res.emit, false);
  assert.ok(res.reason.includes('stale'));
});
