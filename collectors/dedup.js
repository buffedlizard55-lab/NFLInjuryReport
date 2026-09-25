'use strict';

const { STATUS_RANK } = require('../models/alert');

const DEDUP_WINDOW_MS = 5 * 60 * 1000;       // 5 minutes
const STALE_THRESHOLD_MS = 30 * 60 * 1000;   // 30 minutes

class Deduplicator {
  constructor(options = {}) {
    this.dedupWindowMs = options.dedupWindowMs || DEDUP_WINDOW_MS;
    this.staleThresholdMs = options.staleThresholdMs || STALE_THRESHOLD_MS;
    // Map key -> { alert, seenAt, status, rank }
    this.history = new Map();
  }

  _getKey(alert) {
    const sport = (alert.sport || '').toLowerCase();
    const team = (alert.team || '').toUpperCase();
    const player = (alert.player_name || '').toLowerCase().trim();
    return `${sport}:${team}:${player}`;
  }

  /**
   * Evaluates a single alert for deduplication.
   * Returns { emit: boolean, reason?: string, alert?: object }
   */
  evaluate(alert, now = Date.now()) {
    if (!alert || !alert.player_name) {
      return { emit: false, reason: 'invalid_alert' };
    }

    const tSource = new Date(alert.timestamp_source || alert.timestamp_first_seen || now).getTime();
    if (!Number.isNaN(tSource) && (now - tSource > this.staleThresholdMs)) {
      return { emit: false, reason: 'stale (>30min old)' };
    }

    const key = this._getKey(alert);
    const existing = this.history.get(key);
    const alertRank = STATUS_RANK[alert.status] || 1;

    if (existing) {
      const timeSincePrev = now - existing.seenAt;

      // Same status within the 5 minute window -> skip
      if (existing.status === alert.status && timeSincePrev < this.dedupWindowMs) {
        return { emit: false, reason: 'duplicate_status_within_5min' };
      }

      // Check status upgrade (e.g. REPORTED -> QUESTIONABLE -> OUT)
      if (alertRank > existing.rank) {
        // Status upgrade -> emit new alert!
        this.history.set(key, {
          alert,
          seenAt: now,
          status: alert.status,
          rank: alertRank
        });
        return { emit: true, reason: 'status_upgrade' };
      }

      // If not an upgrade and within 5 min window -> skip
      if (timeSincePrev < this.dedupWindowMs) {
        return { emit: false, reason: 'no_status_upgrade_within_5min' };
      }
    }

    // New incident or after 5-minute window
    this.history.set(key, {
      alert,
      seenAt: now,
      status: alert.status,
      rank: alertRank
    });

    return { emit: true, reason: 'new_incident' };
  }

  /**
   * Filters an array of candidate alerts, returning only those to emit.
   */
  filterAlerts(alerts, now = Date.now()) {
    const passed = [];
    for (const alert of alerts) {
      const res = this.evaluate(alert, now);
      if (res.emit) {
        passed.push(alert);
      }
    }
    return passed;
  }

  /**
   * Prune history older than 30 minutes to conserve memory.
   */
  prune(now = Date.now()) {
    for (const [key, value] of this.history.entries()) {
      if (now - value.seenAt > this.staleThresholdMs) {
        this.history.delete(key);
      }
    }
  }

  clear() {
    this.history.clear();
  }
}

module.exports = {
  Deduplicator,
  DEDUP_WINDOW_MS,
  STALE_THRESHOLD_MS
};
