'use strict';

const fs = require('fs');
const path = require('path');
const { classifyStatus, createAlert } = require('../models/alert');
const { extractPlayerName } = require('./espn');
const { detectTeam } = require('./bluesky');

const GOOGLE_NEWS_RSS_BASE = 'https://news.google.com/rss/search';

/**
 * Decodes basic XML / HTML entities in RSS titles.
 */
function unescapeXml(text) {
  if (!text) return '';
  return text
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#8217;/g, "'")
    .replace(/&#8216;/g, "'")
    .replace(/&#8220;/g, '"')
    .replace(/&#8221;/g, '"');
}

/**
 * Lightweight, zero-dependency XML item parser for Google News RSS.
 */
function parseGoogleNewsRss(xmlText, sport = 'nfl', activeClubs = null) {
  if (!xmlText || typeof xmlText !== 'string') return [];

  const items = [];
  const itemRegex = /<item>([\s\S]*?)<\/item>/gi;
  let match;

  while ((match = itemRegex.exec(xmlText)) !== null) {
    const itemBlock = match[1];

    const titleMatch = itemBlock.match(/<title>([\s\S]*?)<\/title>/i);
    const linkMatch = itemBlock.match(/<link>([\s\S]*?)<\/link>/i);
    const pubDateMatch = itemBlock.match(/<pubDate>([\s\S]*?)<\/pubDate>/i);

    const rawTitle = titleMatch ? titleMatch[1] : '';
    const title = unescapeXml(rawTitle.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')).trim();
    const link = linkMatch ? linkMatch[1].trim() : '';
    const pubDateStr = pubDateMatch ? pubDateMatch[1].trim() : '';

    if (!title) continue;

    const status = classifyStatus(title);
    if (!status) continue;

    const team = detectTeam(title, activeClubs);

    // If active clubs filter is provided and team doesn't match any active club, skip
    if (activeClubs && activeClubs.size > 0 && !activeClubs.has(team)) {
      const mentionsActive = Array.from(activeClubs).some(club =>
        new RegExp(`\\b${club}\\b`, 'i').test(title)
      );
      if (!mentionsActive) continue;
    }

    const playerName = extractPlayerName(title);
    const nowIso = new Date().toISOString();
    let timestampSource = nowIso;

    if (pubDateStr) {
      const parsedDate = new Date(pubDateStr);
      if (!Number.isNaN(parsedDate.getTime())) {
        timestampSource = parsedDate.toISOString();
      }
    }

    const alert = createAlert({
      source: 'google-news',
      sport,
      team,
      player_name: playerName,
      status,
      timestamp_source: timestampSource,
      timestamp_first_seen: nowIso,
      verbatim_text: title,
      source_url: link,
      verified: false,
      game_id: null
    });

    items.push(alert);
  }

  return items;
}

class GoogleNewsCollector {
  constructor(options = {}) {
    this.name = 'google-news';
    this.timeout = options.timeout || 8000;
    this.lastRun = null;
    this.lastStatus = 'initialized';
    this.lastError = null;
    this.fixturesDir = options.fixturesDir || path.join(__dirname, '../fixtures');
  }

  async fetchRss(query) {
    const url = `${GOOGLE_NEWS_RSS_BASE}?q=${encodeURIComponent(query)}&hl=en-US&gl=US&ceid=US:en`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' },
        signal: controller.signal
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} from Google News`);
      }
      return await res.text();
    } finally {
      clearTimeout(timer);
    }
  }

  loadFixture(filename = 'google_news_coleman.xml') {
    const filepath = path.join(this.fixturesDir, filename);
    if (fs.existsSync(filepath)) {
      return fs.readFileSync(filepath, 'utf8');
    }
    return null;
  }

  /**
   * Queries Google News RSS per team.
   */
  async getAlerts(sport = 'nfl', activeClubs = null) {
    const queries = [];

    if (activeClubs && activeClubs.size > 0) {
      for (const club of activeClubs) {
        queries.push(`"${club}" ${sport.toUpperCase()} injury`);
      }
    } else {
      queries.push(`${sport.toUpperCase()} injury report`);
    }

    const allAlerts = [];
    let fetchErrors = 0;

    for (const q of queries.slice(0, 3)) {
      try {
        const xml = await this.fetchRss(q);
        const alerts = parseGoogleNewsRss(xml, sport, activeClubs);
        allAlerts.push(...alerts);
      } catch (err) {
        fetchErrors++;
      }
    }

    if (allAlerts.length > 0 || fetchErrors < queries.length) {
      this.lastStatus = fetchErrors > 0 ? 'degraded' : 'ok';
      this.lastRun = new Date().toISOString();
      return allAlerts;
    }

    // Graceful fallback to fixture
    this.lastStatus = 'fallback';
    this.lastRun = new Date().toISOString();
    this.lastError = 'Network unavailable, using fixture';

    const fixtureXml = this.loadFixture('google_news_coleman.xml');
    if (fixtureXml) {
      return parseGoogleNewsRss(fixtureXml, sport, activeClubs);
    }

    return [];
  }
}

module.exports = {
  GoogleNewsCollector,
  parseGoogleNewsRss,
  unescapeXml
};
