'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');
const {
  EspnCollector,
  parseScoreboard,
  parsePlayByPlay,
  parseInjuries,
  extractPlayerName
} = require('../collectors/espn');

test('ESPN Scoreboard parser extracts active and completed games', () => {
  const fixture = require('../fixtures/espn_scoreboard.json');
  const games = parseScoreboard(fixture, 'nfl');

  assert.strictEqual(games.length, 1);
  const game = games[0];
  assert.strictEqual(game.sport, 'nfl');
  assert.strictEqual(game.game_id, '401872932');
  assert.strictEqual(game.club_1, 'DET');
  assert.strictEqual(game.club_2, 'BUF');
  assert.strictEqual(game.state, 'post');
});

test('ESPN NBA Scoreboard parses live game with state="in"', () => {
  const fixture = require('../fixtures/nba_scoreboard.json');
  const games = parseScoreboard(fixture, 'nba');

  assert.strictEqual(games.length, 1);
  const game = games[0];
  assert.strictEqual(game.sport, 'nba');
  assert.strictEqual(game.game_id, '401585601');
  assert.strictEqual(game.club_1, 'GSW');
  assert.strictEqual(game.club_2, 'LAL');
  assert.strictEqual(game.state, 'in');
});

test('ESPN Play-by-play extracts player injury alert and status', () => {
  const fixture = require('../fixtures/nba_summary.json');
  const game = { sport: 'nba', game_id: '401585601', club_1: 'GSW', club_2: 'LAL' };
  const alerts = parsePlayByPlay(fixture, game);

  assert.strictEqual(alerts.length, 1);
  const a = alerts[0];
  assert.strictEqual(a.player_name, 'Stephen Curry');
  assert.strictEqual(a.status, 'OUT_FOR_GAME');
  assert.strictEqual(a.team, 'GSW');
  assert.strictEqual(a.verified, true);
  assert.strictEqual(a.source, 'play-by-play');
  assert.strictEqual(a.game_id, '401585601');
});

test('ESPN NFL Play-by-play extracts in-game questionable return', () => {
  const fixture = require('../fixtures/espn_playbyplay.json');
  const game = { sport: 'nfl', game_id: '401872932', club_1: 'DET', club_2: 'BUF' };
  const alerts = parsePlayByPlay(fixture, game);

  assert.strictEqual(alerts.length, 1);
  const a = alerts[0];
  assert.strictEqual(a.player_name, 'DJ Moore');
  assert.strictEqual(a.status, 'QUESTIONABLE_TO_RETURN');
  assert.strictEqual(a.verified, true);
});

test('ESPN Injuries parses team injuries', () => {
  const fixture = require('../fixtures/espn_injuries.json');
  const alerts = parseInjuries(fixture, 'nfl');

  assert.ok(alerts.length > 0);
  const love = alerts.find(a => a.player_name.includes('Love'));
  assert.ok(love);
  assert.strictEqual(love.team, 'ARI');
  assert.strictEqual(love.status, 'QUESTIONABLE_TO_RETURN');
});

test('extractPlayerName parses different text patterns correctly', () => {
  assert.strictEqual(extractPlayerName('Love (ankle) is warming up'), 'Love');
  assert.strictEqual(extractPlayerName('Bills WR DJ Moore, who landed hard on his shoulder'), 'DJ Moore');
  assert.strictEqual(extractPlayerName('Patrick Mahomes ruled out with ankle injury'), 'Patrick Mahomes');
  assert.strictEqual(extractPlayerName('Keon Coleman injury update: Bills WR returns'), 'Keon Coleman');
});
