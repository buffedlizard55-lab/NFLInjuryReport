'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { parseGoogleNewsRss, unescapeXml } = require('../collectors/google-news');

test('Google News RSS parser extracts injury articles and metadata', () => {
  const xml = fs.readFileSync(path.join(__dirname, '../fixtures/google_news_coleman.xml'), 'utf8');
  const alerts = parseGoogleNewsRss(xml, 'nfl');

  assert.ok(alerts.length >= 4);

  const first = alerts[0];
  assert.strictEqual(first.player_name, 'Keon Coleman');
  assert.strictEqual(first.team, 'BUF');
  assert.strictEqual(first.source, 'google-news');
  assert.strictEqual(first.verified, false);
  assert.ok(first.timestamp_source.startsWith('2026-09-18'));
  assert.ok(first.source_url.includes('news.google.com/rss/articles'));
});

test('unescapeXml decodes HTML/XML entities', () => {
  assert.strictEqual(unescapeXml('Bills&#8217; game &amp; Chiefs'), "Bills' game & Chiefs");
  assert.strictEqual(unescapeXml('&quot;Hello&quot; &lt;tag&gt;'), '"Hello" <tag>');
});
