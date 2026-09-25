'use strict';

const fs = require('fs');
const path = require('path');
const { classifyStatus, createAlert } = require('../models/alert');
const { extractPlayerName } = require('./espn');

const BSKY_AUTHOR_FEED_ENDPOINT = 'https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed';

const NFL_INSIDERS = [
  'rapsheet.bsky.social',       // Ian Rapoport (Verified)
  'tompelissero.bsky.social',   // Tom Pelissero
  'schultzreport.bsky.social',  // Jordan Schultz
  'jglazer.bsky.social',        // Jay Glazer
  'dangraziano.bsky.social',    // Dan Graziano
  'jeffdarlington.bsky.social', // Jeff Darlington
  'profootballtalk.bsky.social',// Mike Florio
  'adamschefter.bsky.social',   // Adam Schefter
  'mikegarafolo.bsky.social',   // Mike Garafolo
  'jfowlerespn.bsky.social',    // Jeremy Fowler
  'diannaspn.bsky.social',      // Dianna Russini
  'pschrags.bsky.social',       // Peter Schrager
  'josinaanderson.bsky.social', // Josina Anderson
  'kimberleymartin.bsky.social' // Kimberley A. Martin
];

const NBA_REPORTERS = [
  'shamscharania.bsky.social',  // Shams Charania
  'marcstein.bsky.social',      // Marc Stein
  'chrishaynes.bsky.social',    // Chris Haynes
  'timmacmahon.bsky.social',    // Tim MacMahon
  'davemcmenamin.bsky.social',  // Dave McMenamin
  'windhorstespn.bsky.social'   // Brian Windhorst
];

const TEAM_MAP = {
  // NFL
  bills: 'BUF', chiefs: 'KC', lions: 'DET', '49ers': 'SF', eagles: 'PHI',
  ravens: 'BAL', cowboys: 'DAL', packers: 'GB', dolphins: 'MIA', texans: 'HOU',
  bengals: 'CIN', steelers: 'PIT', browns: 'CLE', jaguars: 'JAX', colts: 'IND',
  jets: 'NYJ', bears: 'CHI', vikings: 'MIN', falcons: 'ATL', saints: 'NO',
  buccaneers: 'TB', bucs: 'TB', cardinals: 'ARI', rams: 'LAR', seahawks: 'SEA',
  broncos: 'DEN', raiders: 'LV', chargers: 'LAC', patriots: 'NE', panthers: 'CAR',
  giants: 'NYG', commanders: 'WAS', titans: 'TEN',
  // NBA
  lakers: 'LAL', warriors: 'GSW', celtics: 'BOS', bucks: 'MIL', nuggets: 'DEN',
  suns: 'PHX', clippers: 'LAC', heat: 'MIA', knicks: 'NYK', '76ers': 'PHI',
  sixers: 'PHI', mavericks: 'DAL', mavs: 'DAL', timberwolves: 'MIN', wolves: 'MIN',
  thunder: 'OKC', pacers: 'IND', cavaliers: 'CLE', cavs: 'CLE', magic: 'ORL',
  pelicans: 'NOP', bulls: 'CHI', hawks: 'ATL', nets: 'BKN', raptors: 'TOR',
  grizzlies: 'MEM', kings: 'SAC', rockets: 'HOU', jazz: 'UTA', spurs: 'SAS',
  trailblazers: 'POR', blazers: 'POR', hornets: 'CHA', pistons: 'DET', wizards: 'WAS'
};

function detectTeam(text, activeClubs = null) {
  if (!text) return 'NFL';
  const lower = text.toLowerCase();

  // 1. Check against active clubs first
  if (activeClubs && activeClubs.size > 0) {
    for (const club of activeClubs) {
      const re = new RegExp(`\\b${club}\\b`, 'i');
      if (re.test(text)) return club;
    }
  }

  // 2. Check team names and abbreviations
  for (const [name, abbr] of Object.entries(TEAM_MAP)) {
    const re = new RegExp(`\\b${name}\\b`, 'i');
    if (re.test(lower)) {
      return abbr;
    }
  }

  // 3. Fallback: check 2-3 letter uppercase abbreviations
  const abbrMatch = text.match(/\b([A-Z]{2,3})\b/);
  if (abbrMatch && abbrMatch[1] && Object.values(TEAM_MAP).includes(abbrMatch[1])) {
    return abbrMatch[1];
  }

  return 'NFL';
}

function parseBlueskyFeed(payload, sport = 'nfl', activeClubs = null) {
  if (!payload || !Array.isArray(payload.feed)) {
    return [];
  }

  const alerts = [];
  const nowIso = new Date().toISOString();

  for (const item of payload.feed) {
    const post = item.post;
    if (!post) continue;

    const text = post.record?.text || '';
    if (!text) continue;

    const status = classifyStatus(text);
    if (!status) continue;

    const author = post.author || {};
    const isVerified = author.verification?.verifiedStatus === 'valid' ||
      author.verification?.verifications?.some(v => v.isValid) || false;

    const team = detectTeam(text, activeClubs);

    // If active clubs filter is provided and team doesn't match any active club, skip
    if (activeClubs && activeClubs.size > 0 && !activeClubs.has(team)) {
      // Check if sport keyword or player matches active clubs
      const mentionsActive = Array.from(activeClubs).some(club =>
        new RegExp(`\\b${club}\\b`, 'i').test(text)
      );
      if (!mentionsActive) continue;
    }

    const playerName = extractPlayerName(text);
    const postId = post.uri ? post.uri.split('/').pop() : '';
    const sourceUrl = author.handle && postId
      ? `https://bsky.app/profile/${author.handle}/post/${postId}`
      : 'https://bsky.app';

    const timestampSource = post.record?.createdAt || post.indexedAt || nowIso;

    const alert = createAlert({
      source: 'bluesky',
      sport,
      team,
      player_name: playerName,
      status,
      timestamp_source: timestampSource,
      timestamp_first_seen: nowIso,
      verbatim_text: text,
      source_url: sourceUrl,
      verified: Boolean(isVerified),
      game_id: null
    });

    alerts.push(alert);
  }

  return alerts;
}

class BlueskyCollector {
  constructor(options = {}) {
    this.name = 'bluesky';
    this.timeout = options.timeout || 8000;
    this.lastRun = null;
    this.lastStatus = 'initialized';
    this.lastError = null;
    this.fixturesDir = options.fixturesDir || path.join(__dirname, '../fixtures');
    this.nflInsiders = options.nflInsiders || NFL_INSIDERS;
    this.nbaReporters = options.nbaReporters || NBA_REPORTERS;
  }

  async fetchFeed(handle) {
    const url = `${BSKY_AUTHOR_FEED_ENDPOINT}?actor=${encodeURIComponent(handle)}&limit=15`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': 'NFL-NBA-Injury-Alerts/1.0' },
        signal: controller.signal
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} for ${handle}`);
      }
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  loadFixture(filename = 'bsky_author_feed.json') {
    const filepath = path.join(this.fixturesDir, filename);
    if (fs.existsSync(filepath)) {
      return JSON.parse(fs.readFileSync(filepath, 'utf8'));
    }
    return null;
  }

  /**
   * Fetches feeds for verified reporters.
   * If sport is specified, filters target reporters.
   */
  async getAlerts(sport = 'nfl', activeClubs = null) {
    const handles = sport === 'nba' ? this.nbaReporters : this.nflInsiders;
    const allAlerts = [];
    let fetchErrors = 0;

    // In production, poll the top handles
    const targetHandles = handles.slice(0, 6); // batch to stay within rate bounds
    for (const handle of targetHandles) {
      try {
        const feed = await this.fetchFeed(handle);
        const alerts = parseBlueskyFeed(feed, sport, activeClubs);
        allAlerts.push(...alerts);
      } catch (err) {
        fetchErrors++;
      }
    }

    if (allAlerts.length > 0 || fetchErrors < targetHandles.length) {
      this.lastStatus = fetchErrors > 0 ? 'degraded' : 'ok';
      this.lastRun = new Date().toISOString();
      return allAlerts;
    }

    // Graceful fallback to fixture
    this.lastStatus = 'fallback';
    this.lastRun = new Date().toISOString();
    this.lastError = 'Network unavailable, using fixture';

    const fixtureData = this.loadFixture('bsky_author_feed.json');
    if (fixtureData) {
      return parseBlueskyFeed(fixtureData, sport, activeClubs);
    }

    return [];
  }
}

module.exports = {
  BlueskyCollector,
  parseBlueskyFeed,
  detectTeam,
  NFL_INSIDERS,
  NBA_REPORTERS
};
