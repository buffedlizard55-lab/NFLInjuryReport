'use strict';

/**
 * Status hierarchy for in-game injury alerts:
 * INJURY_REPORTED (1) -> QUESTIONABLE_TO_RETURN (2) -> OUT_FOR_GAME (3)
 */
const STATUS_RANK = {
  INJURY_REPORTED: 1,
  QUESTIONABLE_TO_RETURN: 2,
  OUT_FOR_GAME: 3
};

const OUT_PATTERNS = [
  /\bruled\s+out\b/i,
  /\bout\s+for\s+(?:the\s+)?(?:remainder|rest)\b/i,
  /\bout\s+for\s+(?:the\s+)?(?:game|half|night|contest)\b/i,
  /\b(?:will|would|is)\s+not\s+return\b/i,
  /\bwon'?t\s+return\b/i,
  /\bnot\s+expected\s+to\s+return\b/i,
  /\bno\s+longer\s+in\s+the\s+game\b/i,
  /\bdone\s+for\s+the\s+(?:game|night)\b/i,
  /\b(?:listed|declared|ruled|marked)\s+(?:as\s+)?inactive\b/i,
  /\bis\s+inactive\b/i,
  /\binactive\s+(?:for|on)\s+(?:tonight|today)\b/i
];

const QUESTIONABLE_PATTERNS = [
  /\bquestionable\s+to\s+return\b/i,
  /\buncertain\s+to\s+return\b/i,
  /\bstatus\s+(?:is\s+)?questionable\b/i,
  /\bquestionable\b/i,
  /\b(?:being|under)\s+evaluat(?:ed|ion)\b/i,
  /\bevaluat(?:ed|ing)\s+for\b/i,
  /\bcarted?\s+off\b/i,
  /\bcarried\s+off\b/i,
  /\bhelped\s+off\s+the\s+field\b/i,
  /\btaken\s+(?:in)?to\s+the\s+locker\s+room\b/i,
  /\bwent\s+(?:in)?to\s+the\s+locker\s+room\b/i,
  /\bheaded\s+(?:back\s+)?to\s+the\s+locker\s+room\b/i,
  /\bx-?rays?\b/i,
  /\bmri\b/i
];

const INJURY_PATTERNS = [
  /\b(?:suffer(?:ed|s)?|injured|injur(?:y|ies)|hurt|exits?|exited|leaves?|left)\b/i,
  /\binjur(?:y|ies)\s+update\b/i,
  /\bsprain(?:ed)?\b/i,
  /\bstrain(?:ed)?\b/i,
  /\bconcussion\b/i,
  /\bdislocated\b/i,
  /\btorn\b/i,
  /\bacl\b/i,
  /\bmcl\b/i,
  /\bachilles\b/i
];

/**
 * Classifies verbatim text into an in-game alert status.
 * Returns null if the text does not indicate an injury.
 */
function classifyStatus(text) {
  if (!text || typeof text !== 'string') return null;

  for (const pattern of OUT_PATTERNS) {
    if (pattern.test(text)) return 'OUT_FOR_GAME';
  }

  for (const pattern of QUESTIONABLE_PATTERNS) {
    if (pattern.test(text)) return 'QUESTIONABLE_TO_RETURN';
  }

  for (const pattern of INJURY_PATTERNS) {
    if (pattern.test(text)) return 'INJURY_REPORTED';
  }

  return null;
}

/**
 * Creates and normalizes an Alert object.
 */
function createAlert(fields) {
  const nowIso = new Date().toISOString();
  const timestampFirstSeen = fields.timestamp_first_seen || nowIso;
  const timestampSource = fields.timestamp_source || timestampFirstSeen;

  const tFirst = new Date(timestampFirstSeen).getTime();
  const tSource = new Date(timestampSource).getTime();
  const latencyMs = Number.isFinite(fields.latency_ms)
    ? fields.latency_ms
    : Math.max(0, tFirst - (Number.isNaN(tSource) ? tFirst : tSource));

  return {
    source: fields.source || 'play-by-play',
    sport: (fields.sport || 'nfl').toLowerCase(),
    team: (fields.team || '').toUpperCase(),
    player_name: (fields.player_name || '').trim(),
    status: fields.status || 'INJURY_REPORTED',
    timestamp_source: timestampSource,
    timestamp_first_seen: timestampFirstSeen,
    latency_ms: latencyMs,
    verbatim_text: (fields.verbatim_text || '').trim(),
    source_url: fields.source_url || '',
    verified: Boolean(fields.verified),
    game_id: fields.game_id ? String(fields.game_id) : null
  };
}

module.exports = {
  STATUS_RANK,
  classifyStatus,
  createAlert
};
