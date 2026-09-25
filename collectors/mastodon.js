'use strict';

const fs = require('fs');
const path = require('path');
const { classifyStatus, createAlert } = require('../models/alert');
const { extractPlayerName } = require('./espn');
const { detectTeam } = require('./bluesky');

const MASTODON_TAG_BASE = 'https://mastodon.social/api/v1/timelines/tag';

function stripHtml(html) {
  if (!html) return '';
  return html
    .replace(/<br\s*\/?>/gi, ' ')
    .replace(/<\/p>/gi, ' ')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/\s+/g, ' ')
    .trim();
}

function parseMastodonStatuses(statuses, sport = 'nfl', activeClubs = null) {
  if (!Array.isArray(statuses)) return [];

  const alerts = [];
  const nowIso = new Date().toISOString();

  for (const st of statuses) {
    const rawContent = st.content || '';
    const text = stripHtml(rawContent);
    if (!text) continue;

    const status = classifyStatus(text);
    if (!status) continue;

    const team = detectTeam(text, activeClubs);

    // If active clubs filter is provided and team doesn't match any active club, skip
    if (activeClubs && activeClubs.size > 0 && !activeClubs.has(team)) {
      const mentionsActive = Array.from(activeClubs).some(club =>
        new RegExp(`\\b${club}\\b`, 'i').test(text)
      );
      if (!mentionsActive) continue;
    }

    const playerName = extractPlayerName(text);
    const timestampSource = st.created_at || nowIso;
    const sourceUrl = st.url || '';

    const alert = createAlert({
      source: 'mastodon',
      sport,
      team,
      player_name: playerName,
      status,
      timestamp_source: timestampSource,
      timestamp_first_seen: nowIso,
      verbatim_text: text,
      source_url: sourceUrl,
      verified: false,
      game_id: null
    });

    alerts.push(alert);
  }

  return alerts;
}

class MastodonCollector {
  constructor(options = {}) {
    this.name = 'mastodon';
    this.timeout = options.timeout || 8000;
    this.lastRun = null;
    this.lastStatus = 'initialized';
    this.lastError = null;
    this.fixturesDir = options.fixturesDir || path.join(__dirname, '../fixtures');
  }

  async fetchTag(tag) {
    const url = `${MASTODON_TAG_BASE}/${encodeURIComponent(tag)}?limit=20`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': 'NFL-NBA-Injury-Alerts/1.0' },
        signal: controller.signal
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} from Mastodon`);
      }
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  loadFixture(filename = 'mastodon_tag.json') {
    const filepath = path.join(this.fixturesDir, filename);
    if (fs.existsSync(filepath)) {
      return JSON.parse(fs.readFileSync(filepath, 'utf8'));
    }
    return null;
  }

  /**
   * Queries Mastodon hashtag timelines.
   */
  async getAlerts(sport = 'nfl', activeClubs = null) {
    const tags = sport === 'nba' ? ['nba', 'nbainjuries'] : ['nfl', 'nflinjuries'];
    const allAlerts = [];
    let fetchErrors = 0;

    for (const tag of tags) {
      try {
        const statuses = await this.fetchTag(tag);
        const alerts = parseMastodonStatuses(statuses, sport, activeClubs);
        allAlerts.push(...alerts);
      } catch (err) {
        fetchErrors++;
      }
    }

    if (allAlerts.length > 0 || fetchErrors < tags.length) {
      this.lastStatus = fetchErrors > 0 ? 'degraded' : 'ok';
      this.lastRun = new Date().toISOString();
      return allAlerts;
    }

    // Graceful fallback to fixture
    this.lastStatus = 'fallback';
    this.lastRun = new Date().toISOString();
    this.lastError = 'Network unavailable, using fixture';

    const fixtureData = this.loadFixture('mastodon_tag.json');
    if (fixtureData) {
      return parseMastodonStatuses(fixtureData, sport, activeClubs);
    }

    return [];
  }
}

module.exports = {
  MastodonCollector,
  parseMastodonStatuses,
  stripHtml
};
