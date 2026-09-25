'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { parseMastodonStatuses, stripHtml } = require('../collectors/mastodon');

test('Mastodon parser extracts statuses and cleans HTML', () => {
  const fixture = require('../fixtures/mastodon_tag.json');
  const alerts = parseMastodonStatuses(fixture, 'nfl');

  assert.strictEqual(alerts.length, 2);

  const first = alerts[0];
  assert.strictEqual(first.player_name, 'DJ Moore');
  assert.strictEqual(first.team, 'BUF');
  assert.strictEqual(first.status, 'QUESTIONABLE_TO_RETURN');
  assert.strictEqual(first.source, 'mastodon');
  assert.strictEqual(first.verified, false);
  assert.ok(first.source_url.includes('mastodon.social'));
});

test('stripHtml removes HTML tags and entities cleanly', () => {
  assert.strictEqual(stripHtml('<p>Hello <b>world</b>!</p>'), 'Hello world!');
  assert.strictEqual(stripHtml('Injury: &amp; out'), 'Injury: & out');
});
