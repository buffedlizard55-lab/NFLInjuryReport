'use strict';

const { db } = require('../db');

/**
 * Handler for GET /api/health
 */
async function handleHealth(req, res) {
  const stats = await db.getStats();
  const conn = await db.checkConnection();

  const payload = {
    uptime_seconds: Math.floor(process.uptime()),
    alerts_total: stats.alerts_total,
    last_alert: stats.last_alert,
    db_connection: conn.connected ? 'connected' : 'error'
  };

  res.writeHead(200, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'no-cache, no-store, must-revalidate'
  });
  res.end(JSON.stringify(payload, null, 2));
}

module.exports = {
  handleHealth
};
