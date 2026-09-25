'use strict';

const fs = require('fs');
const path = require('path');

class DatabaseClient {
  constructor(options = {}) {
    this.supabaseUrl = options.supabaseUrl || process.env.SUPABASE_URL || null;
    this.supabaseKey = options.supabaseKey || process.env.SUPABASE_KEY || process.env.SUPABASE_ANON_KEY || null;

    // In-memory fallback tables
    this._inMemoryAlerts = [];
    this._inMemoryGames = new Map();
    this._inMemoryHealth = new Map();
    this._alertSequence = 1;

    // 30 days retention in ms
    this.retentionMs = 30 * 24 * 60 * 60 * 1000;
  }

  isSupabaseConfigured() {
    return Boolean(this.supabaseUrl && this.supabaseKey);
  }

  async _supabaseRequest(endpoint, method = 'GET', body = null, extraHeaders = {}) {
    if (!this.isSupabaseConfigured()) {
      throw new Error('Supabase is not configured');
    }

    const url = `${this.supabaseUrl.replace(/\/+$/, '')}/rest/v1${endpoint}`;
    const headers = {
      apikey: this.supabaseKey,
      Authorization: `Bearer ${this.supabaseKey}`,
      'Content-Type': 'application/json',
      ...extraHeaders
    };

    const options = { method, headers };
    if (body) {
      options.body = JSON.stringify(body);
    }

    const res = await fetch(url, options);
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Supabase error ${res.status}: ${errText}`);
    }

    const text = await res.text();
    return text ? JSON.parse(text) : null;
  }

  /**
   * Health check on DB connection
   */
  async checkConnection() {
    if (!this.isSupabaseConfigured()) {
      return { connected: true, type: 'in-memory (local standby)' };
    }
    try {
      await this._supabaseRequest('/alerts?select=id&limit=1');
      return { connected: true, type: 'supabase' };
    } catch (err) {
      return { connected: false, type: 'supabase', error: err.message };
    }
  }

  /**
   * Insert a single alert
   */
  async insertAlert(alert) {
    return (await this.insertAlerts([alert]))[0];
  }

  /**
   * Insert multiple alerts
   */
  async insertAlerts(alerts) {
    if (!Array.isArray(alerts) || alerts.length === 0) {
      return [];
    }

    const nowIso = new Date().toISOString();
    const rows = alerts.map(a => ({
      sport: a.sport,
      team: a.team,
      player_name: a.player_name,
      status: a.status,
      source: a.source,
      timestamp_source: a.timestamp_source || nowIso,
      timestamp_first_seen: a.timestamp_first_seen || nowIso,
      latency_ms: a.latency_ms || 0,
      verbatim_text: a.verbatim_text || '',
      source_url: a.source_url || '',
      verified: Boolean(a.verified),
      game_id: a.game_id || null,
      created_at: a.created_at || nowIso
    }));

    if (this.isSupabaseConfigured()) {
      try {
        const inserted = await this._supabaseRequest(
          '/alerts',
          'POST',
          rows,
          { Prefer: 'return=representation' }
        );
        return inserted;
      } catch (err) {
        // Fall back to in-memory store if network/db error occurs
      }
    }

    // In-memory fallback
    const result = [];
    for (const row of rows) {
      const record = {
        id: this._alertSequence++,
        ...row
      };
      this._inMemoryAlerts.unshift(record); // newest first
      result.push(record);
    }

    this.pruneOldAlerts();
    return result;
  }

  /**
   * Query alerts with filtering and pagination
   */
  async getAlerts(options = {}) {
    const { sport, team, limit = 20, offset = 0 } = options;
    const maxLimit = Math.min(Math.max(1, parseInt(limit, 10) || 20), 100);

    if (this.isSupabaseConfigured()) {
      try {
        const queryParams = new URLSearchParams();
        queryParams.set('select', '*');
        queryParams.set('order', 'created_at.desc');
        queryParams.set('limit', String(maxLimit));
        if (offset > 0) queryParams.set('offset', String(offset));
        if (sport) queryParams.set('sport', `eq.${sport.toLowerCase()}`);
        if (team) queryParams.set('team', `eq.${team.toUpperCase()}`);

        const alerts = await this._supabaseRequest(`/alerts?${queryParams.toString()}`);
        return alerts || [];
      } catch (err) {
        // Fallback to in-memory
      }
    }

    // In-memory query
    let filtered = this._inMemoryAlerts;
    if (sport) {
      const s = sport.toLowerCase();
      filtered = filtered.filter(a => a.sport === s);
    }
    if (team) {
      const t = team.toUpperCase();
      filtered = filtered.filter(a => a.team === t);
    }

    return filtered.slice(offset, offset + maxLimit);
  }

  /**
   * Upsert game details
   */
  async upsertGame(game) {
    const nowIso = new Date().toISOString();
    const row = {
      sport: game.sport,
      game_id: String(game.game_id),
      club_1: game.club_1,
      club_2: game.club_2,
      state: game.state,
      kickoff_time: game.kickoff_time,
      last_checked: game.last_checked || nowIso
    };

    if (this.isSupabaseConfigured()) {
      try {
        await this._supabaseRequest(
          '/games?on_conflict=game_id',
          'POST',
          [row],
          { Prefer: 'resolution=merge-duplicates' }
        );
        return row;
      } catch (err) {
        // Fallback to in-memory
      }
    }

    this._inMemoryGames.set(row.game_id, {
      id: this._inMemoryGames.size + 1,
      ...row
    });
    return row;
  }

  /**
   * Get active games ('in')
   */
  async getActiveGames(sport = null) {
    if (this.isSupabaseConfigured()) {
      try {
        let endpoint = '/games?state=eq.in';
        if (sport) {
          endpoint += `&sport=eq.${sport.toLowerCase()}`;
        }
        const games = await this._supabaseRequest(endpoint);
        return games || [];
      } catch (err) {
        // Fallback to in-memory
      }
    }

    const active = [];
    for (const g of this._inMemoryGames.values()) {
      if (g.state === 'in') {
        if (!sport || g.sport === sport.toLowerCase()) {
          active.push(g);
        }
      }
    }
    return active;
  }

  /**
   * Upsert collector health check
   */
  async recordHealthCheck(collectorName, status, errorMsg = null) {
    const nowIso = new Date().toISOString();
    const row = {
      collector_name: collectorName,
      last_run: nowIso,
      status,
      error_msg: errorMsg
    };

    if (this.isSupabaseConfigured()) {
      try {
        await this._supabaseRequest(
          '/health_check?on_conflict=collector_name',
          'POST',
          [row],
          { Prefer: 'resolution=merge-duplicates' }
        );
        return row;
      } catch (err) {
        // Fallback to in-memory
      }
    }

    this._inMemoryHealth.set(collectorName, row);
    return row;
  }

  /**
   * Get all collector health checks
   */
  async getHealthChecks() {
    if (this.isSupabaseConfigured()) {
      try {
        const rows = await this._supabaseRequest('/health_check?select=*');
        if (rows && rows.length > 0) {
          const map = {};
          for (const r of rows) {
            map[r.collector_name] = r;
          }
          return map;
        }
      } catch (err) {
        // Fallback to in-memory
      }
    }

    const map = {};
    for (const [name, row] of this._inMemoryHealth.entries()) {
      map[name] = row;
    }
    return map;
  }

  /**
   * Database statistics
   */
  async getStats() {
    const alerts = await this.getAlerts({ limit: 1 });
    const count = this._inMemoryAlerts.length;

    return {
      alerts_total: count,
      last_alert: alerts.length > 0 ? alerts[0] : null
    };
  }

  /**
   * Prunes alerts older than 30 days
   */
  pruneOldAlerts() {
    const cutoff = Date.now() - this.retentionMs;
    this._inMemoryAlerts = this._inMemoryAlerts.filter(a => {
      const t = new Date(a.created_at).getTime();
      return Number.isNaN(t) || t >= cutoff;
    });
  }
}

const db = new DatabaseClient();

module.exports = {
  DatabaseClient,
  db
};
