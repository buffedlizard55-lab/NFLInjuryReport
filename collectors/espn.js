'use strict';

const fs = require('fs');
const path = require('path');
const { classifyStatus, createAlert } = require('../models/alert');
const { createGame } = require('../models/game');

const ENDPOINTS = {
  nfl: {
    scoreboard: 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
    summary: 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=',
    injuries: 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries'
  },
  nba: {
    scoreboard: 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
    summary: 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=',
    injuries: 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries'
  }
};

const INJURY_KEYWORDS_RE = /\b(injury|injuries|injured|out|questionable|carted|evaluated|evaluation|locker\s+room|x-?ray|mri|hurt|sprain|strain|concussion)\b/i;

/**
 * Helper to extract player name from sentence.
 * Looks for patterns like "Patrick Mahomes (ankle)..." or "Stephen Curry leaves game..."
 */
function extractPlayerName(text, knownPlayers = []) {
  if (!text) return 'Unknown Player';

  // 0. Remove trailing news publisher attribution (e.g. " - Democrat and Chronicle")
  const cleaned = text.split(/\s+-\s+[A-Z]/)[0].trim();

  // 1. Check known players first if provided
  for (const name of knownPlayers) {
    if (name && cleaned.toLowerCase().includes(name.toLowerCase())) {
      return name;
    }
  }

  // 2. Pattern: "Player Name injury update: ..." or "Player Name Injury Update: ..."
  const headlineMatch = cleaned.match(/^([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){1,2})\s+(?:injury|injuries)\b/i);
  if (headlineMatch && headlineMatch[1]) {
    return headlineMatch[1].trim();
  }

  // 3. Pattern: "Name (injury)..." e.g., "Love (ankle) is warming up" or "Patrick Mahomes (ankle)"
  const parenMatch = cleaned.match(/([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+)*)\s*\([^)]*(?:ankle|knee|shoulder|foot|hamstring|groin|injury|head|illness)[^)]*\)/i);
  if (parenMatch && parenMatch[1]) {
    return parenMatch[1].trim();
  }

  // 4. Pattern: "Team POS Player Name, who..." e.g. "Bills WR DJ Moore, who landed..."
  const posMatch = cleaned.match(/\b(?:WR|QB|RB|TE|CB|LB|DE|DT|OT|OG|S|K|P|G|F|C)\s+([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+)+)/);
  if (posMatch && posMatch[1]) {
    return posMatch[1].trim();
  }

  // 5. Pattern: Capitalized full name before action verb
  const nameActionMatch = cleaned.match(/^([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+)+)\s+(?:leaves|exits|ruled|was|is|has|headed|taken|went|suffered|undergoing)/i);
  if (nameActionMatch && nameActionMatch[1]) {
    return nameActionMatch[1].trim();
  }

  // 6. Fallback regex for 2-3 capitalized words
  const capMatch = cleaned.match(/\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b/);
  if (capMatch && capMatch[1]) {
    return capMatch[1].trim();
  }

  return 'Unknown Player';
}

/**
 * Parses ESPN Scoreboard API response.
 * Returns array of Game objects.
 */
function parseScoreboard(payload, sport = 'nfl') {
  if (!payload || !Array.isArray(payload.events)) {
    return [];
  }

  const games = [];
  for (const ev of payload.events) {
    const comp = ev.competitions?.[0];
    if (!comp) continue;

    const competitors = comp.competitors || [];
    const home = competitors.find(c => c.homeAway === 'home') || competitors[0];
    const away = competitors.find(c => c.homeAway === 'away') || competitors[1];

    const state = comp.status?.type?.state || 'pre';
    const game = createGame({
      sport,
      game_id: ev.id,
      club_1: away?.team?.abbreviation || 'AWAY',
      club_2: home?.team?.abbreviation || 'HOME',
      club_1_name: away?.team?.displayName || away?.team?.name || '',
      club_2_name: home?.team?.displayName || home?.team?.name || '',
      state,
      kickoff_time: ev.date || comp.date || null,
      last_checked: new Date().toISOString()
    });

    games.push(game);
  }

  return games;
}

/**
 * Extracts plays from ESPN summary payload.
 */
function extractPlaysFromSummary(summary) {
  if (!summary) return [];

  const plays = [];

  // 1. Direct plays array (e.g. NBA summary or direct playbyplay)
  if (Array.isArray(summary.plays)) {
    plays.push(...summary.plays);
  }

  // 2. Football drives structure
  if (summary.drives) {
    if (summary.drives.current && Array.isArray(summary.drives.current.plays)) {
      plays.push(...summary.drives.current.plays);
    }
    if (Array.isArray(summary.drives.previous)) {
      for (const d of summary.drives.previous) {
        if (Array.isArray(d.plays)) {
          plays.push(...d.plays);
        }
      }
    }
  }

  return plays;
}

/**
 * Parses ESPN play-by-play summary for injury keywords and constructs alert objects.
 */
function parsePlayByPlay(summaryPayload, game) {
  if (!summaryPayload || !game) return [];

  const sport = (game.sport || 'nfl').toLowerCase();
  const plays = extractPlaysFromSummary(summaryPayload);
  const alerts = [];
  const nowIso = new Date().toISOString();

  // Extract known roster player names if boxscore has them
  const knownPlayers = [];
  if (summaryPayload.boxscore?.players) {
    for (const teamBlock of summaryPayload.boxscore.players) {
      for (const statGroup of (teamBlock.statistics || [])) {
        for (const ath of (statGroup.athletes || [])) {
          if (ath.athlete?.displayName) {
            knownPlayers.push(ath.athlete.displayName);
          }
        }
      }
    }
  }

  for (const play of plays) {
    const text = play.text || play.alternativeText || '';
    if (!text || !INJURY_KEYWORDS_RE.test(text)) {
      continue;
    }

    const status = classifyStatus(text);
    if (!status) continue;

    const playerName = extractPlayerName(text, knownPlayers);
    const team = play.team?.abbreviation || game.club_1 || 'NFL';
    const sourceUrl = `https://www.espn.com/${sport}/game/_/gameId/${game.game_id}`;

    const alert = createAlert({
      source: 'play-by-play',
      sport,
      team,
      player_name: playerName,
      status,
      timestamp_source: play.wallclock || nowIso,
      timestamp_first_seen: nowIso,
      verbatim_text: text,
      source_url: sourceUrl,
      verified: true,
      game_id: game.game_id
    });

    alerts.push(alert);
  }

  return alerts;
}

/**
 * Parses ESPN Injuries API response.
 */
function parseInjuries(injuriesPayload, sport = 'nfl', activeClubs = null) {
  if (!injuriesPayload || !Array.isArray(injuriesPayload.injuries)) {
    return [];
  }

  const alerts = [];
  const nowIso = new Date().toISOString();

  for (const teamBlock of injuriesPayload.injuries) {
    const teamAbbr = teamBlock.displayName?.split(' ').pop().slice(0, 3).toUpperCase() || 'NFL';

    for (const inj of (teamBlock.injuries || [])) {
      const text = inj.shortComment || inj.longComment || '';
      if (!text) continue;

      const athlete = inj.athlete || {};
      const team = athlete.team?.abbreviation || teamAbbr;

      // Filter by active clubs if specified
      if (activeClubs && activeClubs.size > 0 && !activeClubs.has(team)) {
        continue;
      }

      let status = classifyStatus(text);
      if (!status || status === 'INJURY_REPORTED') {
        const injStatus = (inj.status || '').toLowerCase();
        if (injStatus.includes('out')) {
          status = 'OUT_FOR_GAME';
        } else if (injStatus.includes('questionable')) {
          status = 'QUESTIONABLE_TO_RETURN';
        } else if (!status) {
          status = 'INJURY_REPORTED';
        }
      }
      const playerName = athlete.displayName || extractPlayerName(text);
      const timestampSource = inj.date || nowIso;
      const sourceUrl = athlete.links?.[0]?.href || ENDPOINTS[sport]?.injuries || '';

      const alert = createAlert({
        source: 'play-by-play',
        sport,
        team,
        player_name: playerName,
        status,
        timestamp_source: timestampSource,
        timestamp_first_seen: nowIso,
        verbatim_text: text,
        source_url: sourceUrl,
        verified: true,
        game_id: null
      });

      alerts.push(alert);
    }
  }

  return alerts;
}

/**
 * ESPN Collector Class
 */
class EspnCollector {
  constructor(options = {}) {
    this.name = 'espn';
    this.timeout = options.timeout || 8000;
    this.lastRun = null;
    this.lastStatus = 'initialized';
    this.lastError = null;
    this.fixturesDir = options.fixturesDir || path.join(__dirname, '../fixtures');
  }

  async fetchJson(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      const res = await fetch(url, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' },
        signal: controller.signal
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} from ${url}`);
      }
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  loadFixture(filename) {
    const filepath = path.join(this.fixturesDir, filename);
    if (fs.existsSync(filepath)) {
      return JSON.parse(fs.readFileSync(filepath, 'utf8'));
    }
    return null;
  }

  /**
   * Fetches scoreboard for sport ('nfl' or 'nba').
   */
  async getScoreboard(sport = 'nfl') {
    const url = ENDPOINTS[sport]?.scoreboard;
    try {
      const data = await this.fetchJson(url);
      this.lastStatus = 'ok';
      this.lastRun = new Date().toISOString();
      return parseScoreboard(data, sport);
    } catch (err) {
      this.lastError = err.message;
      this.lastStatus = 'fallback';
      this.lastRun = new Date().toISOString();

      // Graceful fallback to fixture when network is restricted
      const fixtureName = sport === 'nba' ? 'nba_scoreboard.json' : 'espn_scoreboard.json';
      const fixtureData = this.loadFixture(fixtureName);
      if (fixtureData) {
        return parseScoreboard(fixtureData, sport);
      }
      return [];
    }
  }

  /**
   * Fetches summary/play-by-play for a specific game.
   */
  async getPlayByPlay(game) {
    const sport = (game.sport || 'nfl').toLowerCase();
    const url = `${ENDPOINTS[sport]?.summary}${game.game_id}`;
    try {
      const data = await this.fetchJson(url);
      this.lastStatus = 'ok';
      this.lastRun = new Date().toISOString();
      return parsePlayByPlay(data, game);
    } catch (err) {
      this.lastError = err.message;
      this.lastStatus = 'fallback';
      this.lastRun = new Date().toISOString();

      const fixtureName = sport === 'nba'
        ? 'nba_summary.json'
        : (fs.existsSync(path.join(this.fixturesDir, 'espn_playbyplay.json')) ? 'espn_playbyplay.json' : 'espn_summary.json');
      const fixtureData = this.loadFixture(fixtureName);
      if (fixtureData) {
        return parsePlayByPlay(fixtureData, game);
      }
      return [];
    }
  }

  /**
   * Fetches injuries list.
   */
  async getInjuries(sport = 'nfl', activeClubs = null) {
    const url = ENDPOINTS[sport]?.injuries;
    try {
      const data = await this.fetchJson(url);
      this.lastStatus = 'ok';
      this.lastRun = new Date().toISOString();
      return parseInjuries(data, sport, activeClubs);
    } catch (err) {
      this.lastError = err.message;
      this.lastStatus = 'fallback';
      this.lastRun = new Date().toISOString();

      const fixtureData = this.loadFixture('espn_injuries.json');
      if (fixtureData) {
        return parseInjuries(fixtureData, sport, activeClubs);
      }
      return [];
    }
  }
}

module.exports = {
  EspnCollector,
  parseScoreboard,
  parsePlayByPlay,
  parseInjuries,
  extractPlayerName,
  ENDPOINTS
};
