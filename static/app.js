'use strict';

const I18N = window.I18N || {};

// ── Full refresh (shared by the header button and a station change) ─────────
// Triggers a live re-fetch of all four weather sources (Yr.no, OWM,
// Open-Meteo, METAR) via POST /api/refresh -- not just a re-read of the
// already-cached dashboard data like each page's passive 60s poll does. The
// backend deliberately never recomputes the irrigation decision or publishes
// to MQTT on this path (see dashboard/services.py's api_refresh docstring),
// so calling this can't command a pump.
//
// One shared busy/queued lock, not a per-caller one -- the header button and
// the config panel's "station changed" path can both try to trigger this
// (e.g. the user clicks refresh, then switches station mid-flight before it
// resolves). Without a shared lock those two POST /api/refresh calls race,
// and whichever /api/status re-read lands last silently wins, which is what
// made a station switch mid-refresh appear to show "half old, half new"
// data. A second call arriving while one is already in flight doesn't fire
// its own overlapping request; it just marks refreshQueued so the loop below
// runs one more full cycle immediately after the current one finishes,
// picking up whatever's current (station, etc.) at that point.
let refreshBusy = false;
let refreshQueued = false;

async function triggerFullRefresh() {
  if (refreshBusy) { refreshQueued = true; return; }
  refreshBusy = true;
  const btn = document.getElementById('refresh-btn');
  if (btn) { btn.classList.add('spinning'); btn.disabled = true; }
  try {
    do {
      refreshQueued = false;
      try {
        const res = await fetch('/api/refresh', { method: 'POST' });
        if (!res.ok) console.error('Manual refresh failed:', await res.text());
      } catch (err) {
        console.error('Error triggering manual refresh:', err);
      }
      if (typeof window.dashboardRefresh === 'function') await window.dashboardRefresh();
    } while (refreshQueued);
  } finally {
    refreshBusy = false;
    if (btn) { btn.classList.remove('spinning'); btn.disabled = false; }
  }
}

(() => {
  const btn = document.getElementById('refresh-btn');
  if (!btn) return;
  btn.addEventListener('click', () => { triggerFullRefresh(); });
})();

// ── Quit (AppImage/Electron shell only) ────────────────────────────────────
// window.electronAPI only exists when preload.js ran, which only happens
// inside the packaged Electron BrowserWindow -- a normal browser hitting
// the systemd-deployed dashboard on port 8080 never gets it, so the button
// (hidden by default in the template) simply never appears there. Confirms
// before quitting since this stops weather_mqtt.py --service too, not just
// the window.
(() => {
  const btn = document.getElementById('quit-btn');
  if (!btn || !window.electronAPI) return;
  btn.classList.remove('hidden');
  btn.addEventListener('click', () => {
    if (window.confirm(I18N.quit_confirm)) window.electronAPI.quit();
  });
})();

// ── Config modal ─────────────────────────────────────────────────────────────
// Loads/saves all fields uniformly via /api/config. station/broker/
// root_topic/lang are always resolved to a real value by the backend (its
// own hardcoded defaults if unset/invalid), so those never show blank --
// they show whatever's actually in effect, whether that's a saved override
// or the default. mqtt_pub_password is the one exception: blank is a real,
// meaningful value ("publish unencrypted"), not a missing one, so it has no
// default to fall back to. sun_projection/sun_view are checkbox-as-switch
// controls (see `switches` below), not value-holding inputs, so they can't
// go through the generic `fields` loop the same way.
(() => {
  const overlay = document.getElementById('config-overlay');
  const openBtn = document.getElementById('config-btn');
  const closeBtn = document.getElementById('config-close');
  const cancelBtn = document.getElementById('config-cancel');
  const saveBtn = document.getElementById('config-save');
  if (!overlay || !openBtn) return;

  // Two tabs share one form/save button -- General (lang, sun path display)
  // and Station (the METAR/MQTT fields that actually talk to the physical
  // station). Both are only reachable from the one gear icon; the header's
  // station nav link goes to the separate /stations page instead.
  const tabBtns = overlay.querySelectorAll('.config-tab-btn');
  const tabPanels = overlay.querySelectorAll('.config-tab');
  const showTab = (name) => {
    for (const b of tabBtns) b.classList.toggle('active', b.dataset.tab === name);
    for (const p of tabPanels) p.classList.toggle('active', p.dataset.tab === name);
  };
  for (const b of tabBtns) b.addEventListener('click', () => showTab(b.dataset.tab));

  const fields = {
    station: document.getElementById('cfg-station'),
    broker: document.getElementById('cfg-broker'),
    root_topic: document.getElementById('cfg-topic'),
    lang: document.getElementById('cfg-lang'),
    mqtt_pub_password: document.getElementById('cfg-aes-key'),
  };
  const switches = {
    sun_projection: { input: document.getElementById('cfg-sun-projection'), on: 'orthographic', off: 'linear' },
    sun_view: { input: document.getElementById('cfg-sun-view'), on: 'up', off: 'down' },
  };

  // Airport codes are always 4 uppercase letters -- normalize as the user
  // types so what they see matches what the backend will validate/store.
  fields.station.addEventListener('input', () => {
    const start = fields.station.selectionStart;
    fields.station.value = fields.station.value.toUpperCase().replace(/[^A-Z]/g, '').slice(0, 4);
    fields.station.setSelectionRange(start, start);
  });

  const close = () => overlay.classList.add('hidden');

  // Captured on open so save can tell whether the station actually changed
  // -- that's the one field whose cached weather_cache.json data is
  // genuinely stale/wrong for the new value until a real fetch runs (unlike
  // e.g. sun path style, which just needs a re-read of already-cached data).
  let openedStation = '';

  const openOnTab = async (tab) => {
    showTab(tab);
    overlay.classList.remove('hidden');
    try {
      const res = await fetch('/api/config');
      const cfg = await res.json();
      for (const key of Object.keys(fields)) {
        fields[key].value = cfg[key] || '';
      }
      for (const key of Object.keys(switches)) {
        switches[key].input.checked = cfg[key] === switches[key].on;
      }
      openedStation = (cfg.station || '').trim().toUpperCase();
    } catch (err) {
      console.error('Error loading config:', err);
    }
  };

  openBtn.addEventListener('click', () => openOnTab('general'));
  closeBtn.addEventListener('click', close);
  cancelBtn.addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !overlay.classList.contains('hidden')) close();
  });

  saveBtn.addEventListener('click', async () => {
    const label = saveBtn.textContent;
    saveBtn.textContent = I18N.saving;
    saveBtn.disabled = true;
    try {
      const body = {};
      for (const key of Object.keys(fields)) body[key] = fields[key].value.trim();
      for (const key of Object.keys(switches)) {
        body[key] = switches[key].input.checked ? switches[key].on : switches[key].off;
      }
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await res.text());
      // Static, server-rendered text only reflects the new language after a
      // full reload -- the modal fields themselves already show it live.
      if (fields.lang.value !== document.documentElement.lang) {
        window.location.reload();
        return;
      }
      const stationChanged = fields.station.value.trim().toUpperCase() !== openedStation;
      close();
      // A changed station has no fresh weather_cache.json data yet -- a
      // real fetch (triggerFullRefresh, same as the header's manual refresh
      // button), not just a re-poll of the old station's still-cached
      // numbers, which would otherwise linger for however long until the
      // next scheduled cycle and look like nothing was saved. Any other
      // field (sun path style, broker, topic) only needs the already-cached
      // status re-read.
      if (stationChanged) {
        triggerFullRefresh();
      } else if (typeof window.dashboardRefresh === 'function') {
        await window.dashboardRefresh();
      }
    } catch (err) {
      console.error('Error saving config:', err);
      saveBtn.textContent = I18N.error;
      setTimeout(() => { saveBtn.textContent = label; }, 1500);
    } finally {
      saveBtn.disabled = false;
      if (saveBtn.textContent === I18N.saving) saveBtn.textContent = label;
    }
  });
})();

// ── Clear cache (settings panel's danger zone) ───────────────────────────────
// POST /api/clear-cache wipes dashboard/services.py's IrrigationDecision/
// MetarReading tables and every data/*.json cache file it writes (see that
// function's docstring for exactly what's kept, namely site_config.json) --
// this alone is enough for the systemd-deployed webUI, since there's no
// other cache in play there. Inside the Electron shell there's a second,
// separate cache this can't reach: the BrowserWindow's own session-level
// HTTP cache/localStorage (see electron/main.js's clearCache() -- the same
// thing createWindow() already clears on every load, just on demand here).
// window.electronAPI.clearCache only exists there, so this is a no-op
// addition for a normal browser. A full reload afterward either way, so the
// now-empty backend shows its demo-data fallback instead of stale numbers
// left over from before the clear.
(() => {
  const btn = document.getElementById('config-clear-cache');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!window.confirm(I18N.clear_cache_confirm)) return;
    btn.disabled = true;
    try {
      const res = await fetch('/api/clear-cache', { method: 'POST' });
      if (!res.ok) throw new Error(await res.text());
      if (window.electronAPI && window.electronAPI.clearCache) {
        await window.electronAPI.clearCache();
      }
      window.location.reload();
    } catch (err) {
      console.error('Error clearing cache:', err);
      window.alert(I18N.clear_cache_failed);
      btn.disabled = false;
    }
  });
})();
