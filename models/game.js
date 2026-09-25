'use strict';

/**
 * Normalizes game representation from ESPN Scoreboard or Summary.
 */
function createGame(fields) {
  return {
    sport: (fields.sport || 'nfl').toLowerCase(),
    game_id: String(fields.game_id || fields.id),
    club_1: (fields.club_1 || '').toUpperCase(),
    club_2: (fields.club_2 || '').toUpperCase(),
    club_1_name: fields.club_1_name || '',
    club_2_name: fields.club_2_name || '',
    state: fields.state || 'pre', // 'pre', 'in', 'post'
    kickoff_time: fields.kickoff_time || null,
    last_checked: fields.last_checked || new Date().toISOString()
  };
}

module.exports = {
  createGame
};
