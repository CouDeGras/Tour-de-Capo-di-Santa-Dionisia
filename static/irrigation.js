'use strict';

// I18N is declared once in app.js (loaded first, same global scope) -- not
// redeclared here.

// Display labels for decision codes, in the site's configured language
const DECISION_LABEL = {
  DRENCH:      I18N.decision_drench,
  WAIT:        I18N.decision_wait,
  FREEZE_HOLD: I18N.decision_freeze,
};

const ANCHOR_LABEL = { sunrise: I18N.anchor_sunrise, sunset: I18N.anchor_sunset };

// ── Countdown ─────────────────────────────────────────────────────────────────

let nextEpoch = null;

function pad(n) { return String(Math.floor(n)).padStart(2, '0'); }

function tickCountdown() {
  const el = document.getElementById('countdown');
  if (nextEpoch === null) { el.textContent = '—'; return; }
  const diff = nextEpoch - Date.now() / 1000;
  if (diff <= 0) { el.textContent = I18N.now; return; }
  const d = Math.floor(diff / 86400);
  const h = Math.floor((diff % 86400) / 3600);
  const m = Math.floor((diff % 3600) / 60);
  const s = Math.floor(diff % 60);
  el.textContent = d > 0
    ? `${d}d ${pad(h)}h ${pad(m)}m ${pad(s)}s`
    : `${pad(h)}:${pad(m)}:${pad(s)}`;
}

setInterval(tickCountdown, 1000);

function setRing(pct) {
  const label = document.getElementById('ring-pct');
  if (!label) return;
  label.textContent = Math.max(0, Math.min(100, pct)) + '%';
}

// ── Render helpers ────────────────────────────────────────────────────────────

function rv(v, d = 1) { return v == null ? '—' : Number(v).toFixed(d); }

function renderCountdown(schedule) {
  const events = (schedule.json_payload || {}).events || [];
  const active = events.filter(ev => (ev.percent || 0) > 0);

  if (active.length) {
    nextEpoch = active[0].epoch;
    setRing(active[0].percent ?? 0);
    tickCountdown();
    document.getElementById('event-list').innerHTML = active.map(ev => {
      const anchor = (ev.solar_anchor || '').toLowerCase();
      const label  = ANCHOR_LABEL[anchor] || anchor.toUpperCase();
      const time   = String(ev.iso_local || '').replace('T', ' ').slice(0, 16);
      return `<div class="event-row">
        <span class="ev-seq">#${ev.sequence}</span>
        <span class="ev-anchor">${label}</span>
        <span class="ev-time">${time}</span>
        <span class="ev-pct">${ev.percent}%</span>
        <span class="ev-dur">${ev.pump_seconds}s</span>
      </div>`;
    }).join('');
  } else {
    nextEpoch = null;
    setRing(0);
    tickCountdown();
    document.getElementById('event-list').innerHTML = '';
  }
}

// Estimated 0-24h water demand feeding the irrigation percent decision --
// shown alongside the countdown/events since it's what they're computed from.
function renderDemand(ensemble) {
  const el = document.getElementById('demand-value');
  if (!el) return;
  const d = (ensemble.demand_mm || {}).d0_24;
  el.textContent = d != null ? `${rv(d)} mm/d` : '—';
}

function agoLabel(epoch) {
  if (epoch == null) return '—';
  const diff = Date.now() / 1000 - epoch;
  if (diff < 0) return I18N.just_now;
  if (diff < 60) return `${Math.floor(diff)}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

function renderPumpNodes(devices) {
  const tbody = document.getElementById('acks-body');
  if (!tbody) return;
  const ids = Object.keys(devices || {});
  if (!ids.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="tbl-empty">${I18N.no_acks}</td></tr>`;
    return;
  }
  tbody.innerHTML = ids
    .sort((a, b) => (devices[b].received_at_epoch || 0) - (devices[a].received_at_epoch || 0))
    .map(id => {
      const ack = devices[id] || {};
      const code = ack.ack_decision || '—';
      const label = DECISION_LABEL[code] || code;
      const executed = ack.executed
        ? `<span class="dec-pill dec-pill-inv">${I18N.yes}</span>`
        : `<span class="dec-pill">${I18N.no}</span>`;
      const armed = ack.armed ? I18N.yes : I18N.no;
      return `<tr>
        <td><strong>${id}</strong></td>
        <td>${agoLabel(ack.received_at_epoch)}</td>
        <td>${executed}</td>
        <td>${ack.executed_pump_s ?? 0}s</td>
        <td><span class="dec-pill">${label}</span></td>
        <td>${armed}</td>
      </tr>`;
    }).join('');
}

function renderHistory(rows) {
  const tbody = document.getElementById('history-body');
  if (!rows || !rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="tbl-empty">${I18N.no_history}</td></tr>`;
    return;
  }
  tbody.innerHTML = [...rows].reverse().map(row => {
    const code = row.decision_code || 'WAIT';
    const inv  = code === 'DRENCH';
    const pill = inv ? 'dec-pill dec-pill-inv' : 'dec-pill';
    const label = DECISION_LABEL[code] || code;
    return `<tr>
      <td><strong>${row.local_date || '—'}</strong></td>
      <td><span class="${pill}">${label}</span></td>
      <td>${row.commanded_percent ?? 0}%</td>
      <td>${row.commanded_pump_seconds ?? 0}s</td>
      <td>${Number(row.forecast_precip_local_day_mm || 0).toFixed(1)}</td>
      <td>${Number(row.forecast_tmin24_c || 0).toFixed(1)}°</td>
      <td>${Number(row.forecast_tmax24_c || 0).toFixed(1)}°</td>
    </tr>`;
  }).join('');
}

// renderRainCharts/renderMeanChart/applyTrinityMode now live in charts.js
// (loaded before this file -- see irrigation.html), shared with weather.js.

// ── Main refresh ─────────────────────────────────────────────────────────────

let lastAckDevices = {};

async function refresh() {
  try {
    const [statusRes, histRes, acksRes] = await Promise.all([
      fetch('/api/status'),
      fetch('/api/history?n=14'),
      fetch('/api/acks'),
    ]);
    const data = await statusRes.json();
    const hist = await histRes.json();
    const acks = await acksRes.json();

    const irrig = data.irrigation || {};
    const sched = irrig.schedule  || {};
    const ens   = data.ensemble   || {};
    const frows = (data.comparison || {}).rows || [];

    renderCountdown(sched);
    renderDemand(ens);
    renderHistory(hist.rows || []);
    applyTrinityMode(frows, data.trinity_mode !== 'off');

    lastAckDevices = acks.devices || {};
    renderPumpNodes(lastAckDevices);

  } catch (err) {
    console.error('Error refreshing:', err);
  }
}

// Registered so the shared header's refresh button (app.js) can trigger a
// re-render of whichever page is actually loaded.
window.dashboardRefresh = refresh;

// Defer first paint until layout is settled
requestAnimationFrame(() => { refresh(); });
setInterval(refresh, 60_000);
setInterval(() => renderPumpNodes(lastAckDevices), 15_000);
