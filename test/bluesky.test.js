'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { parseBlueskyFeed, detectTeam } = require('../collectors/bluesky');

test('Bluesky parser parses verified insider posts with correct attributes', () => {
  const fixture = require('../fixtures/bsky_author_feed.json');
  const alerts = parseBlueskyFeed(fixture, 'nfl');

  assert.strictEqual(alerts.length, 3);

  // First alert: DJ Moore
  const dj = alerts[0];
  assert.strictEqual(dj.player_name, 'DJ Moore');
  assert.strictEqual(dj.team, 'BUF');
  assert.strictEqual(dj.status, 'QUESTIONABLE_TO_RETURN');
  assert.strictEqual(dj.source, 'bluesky');
  assert.strictEqual(dj.verified, true);
  assert.ok(dj.source_url.includes('rapsheet.bsky.social'));

  // Second alert: Ed Oliver out for game
  const ed = alerts[1];
  assert.strictEqual(ed.player_name, 'Ed Oliver');
  assert.strictEqual(ed.team, 'BUF');
  assert.strictEqual(ed.status, 'OUT_FOR_GAME');
  assert.strictEqual(ed.verified, true);
});

test('Bluesky detectTeam detects teams correctly from text', () => {
  assert.strictEqual(detectTeam('Bills WR DJ Moore'), 'BUF');
  assert.strictEqual(detectTeam('Chiefs QB Patrick Mahomes'), 'KC');
  assert.strictEqual(detectTeam('Lakers star LeBron James'), 'LAL');
  assert.strictEqual(detectTeam('Warriors guard Stephen Curry'), 'GSW');
});
