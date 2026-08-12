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
  if (btn) btn.disabled = true;
  window.refreshPopup?.open();
  let ok = true;
  try {
    do {
      refreshQueued = false;
      try {
        const res = await fetch('/api/refresh', { method: 'POST' });
        if (!res.ok) { ok = false; console.error('Manual refresh failed:', await res.text()); }
      } catch (err) {
        ok = false;
        console.error('Error triggering manual refresh:', err);
      }
      if (typeof window.dashboardRefresh === 'function') await window.dashboardRefresh();
    } while (refreshQueued);
  } finally {
    refreshBusy = false;
    if (btn) btn.disabled = false;
    window.refreshPopup?.settle(ok);
  }
}

(() => {
  const btn = document.getElementById('refresh-btn');
  if (!btn) return;
  btn.addEventListener('click', () => { triggerFullRefresh(); });
})();

// ── Refresh popup ────────────────────────────────────────────────────────
// Up for the whole triggerFullRefresh round trip above, not just while a
// fetch is in flight -- open() fires before the first request, settle()
// only once the retry loop (refreshQueued) has actually finished. No close
// button and no click-outside/Escape handler: the only way this hides is
// its own timer in settle(), and that timer only ever starts once the
// request has actually settled (success or failure) -- there's no way to
// dismiss it, by accident or otherwise, while a refresh is still running.
(() => {
  const overlay = document.getElementById('refresh-overlay');
  const statusText = document.getElementById('refresh-status-text');
  if (!overlay || !statusText) return;

  const AUTO_CLOSE_MS = 1500;
  let closeTimer = null;

  window.refreshPopup = {
    open() {
      clearTimeout(closeTimer);
      statusText.textContent = I18N.refresh_updating;
      overlay.classList.remove('hidden');
    },
    settle(ok) {
      statusText.textContent = ok ? I18N.refresh_success : I18N.refresh_failed;
      closeTimer = setTimeout(() => overlay.classList.add('hidden'), AUTO_CLOSE_MS);
    },
  };
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

  // Broker/topic/mqtt_pub_password/pump_* (not station -- that's the
  // weather source, stays visible regardless) only render at all when
  // citrus mode is "on" (see base.html/dashboard/views.py's citrus_on) --
  // document.getElementById returns null for whichever of these aren't in
  // the DOM this load, and the prune step right after drops those keys
  // entirely so every loop below (load/save) just never sees them, rather
  // than needing its own null check.
  const fields = {
    station: document.getElementById('cfg-station'),
    broker: document.getElementById('cfg-broker'),
    root_topic: document.getElementById('cfg-topic'),
    lang: document.getElementById('cfg-lang'),
    sun_projection: document.getElementById('cfg-sun-projection'),
    sun_view: document.getElementById('cfg-sun-view'),
    mqtt_pub_password: document.getElementById('cfg-aes-key'),
    pump_flow_rate_lpm: document.getElementById('cfg-pump-flow-rate'),
    pump_target_volume_l: document.getElementById('cfg-pump-target-volume'),
    citrus_mode: document.getElementById('cfg-citrus-mode'),
    trinity_mode: document.getElementById('cfg-trinity-mode'),
  };
  for (const key of Object.keys(fields)) {
    if (!fields[key]) delete fields[key];
  }

  // Airport codes are always 4 uppercase letters -- normalize as the user
  // types so what they see matches what the backend will validate/store.
  fields.station?.addEventListener('input', () => {
    const start = fields.station.selectionStart;
    fields.station.value = fields.station.value.toUpperCase().replace(/[^A-Z]/g, '').slice(0, 4);
    fields.station.setSelectionRange(start, start);
  });

  // Language: a globe button cycling through every supported locale instead of a
  // dropdown. fields.lang (a hidden input, see base.html) still holds the
  // actual code and goes through the same load/save loop as every other
  // field in `fields` above -- only the visible label next to the button
  // needs its own sync, on open and on every cycle.
  const LANGS = ['en', 'fr', 'it', 'es', 'zh-Hant', 'ja'];
  const LANG_LABELS = {
    en: 'English', fr: 'Français', it: 'Italiano', es: 'Español',
    'zh-Hant': '繁體中文', ja: '日本語',
  };
  const langBtn = document.getElementById('cfg-lang-btn');
  const langLabel = document.getElementById('cfg-lang-label');
  const setLangDisplay = (code) => { langLabel.textContent = LANG_LABELS[code] || code; };
  langBtn.addEventListener('click', () => {
    const next = LANGS[(LANGS.indexOf(fields.lang.value) + 1) % LANGS.length];
    fields.lang.value = next;
    setLangDisplay(next);
  });

  // Icon-cycle buttons (sun path style, sun path view): a single button per
  // field, same interaction as the language globe button and citrus mode
  // toggle -- one click steps to the next value in data-values (a small
  // fixed comma-separated list, e.g. "linear,orthographic") and wraps
  // around. Which icon shows is pure CSS ([data-value=...] .icon-value-...,
  // see style.css), driven by the button's own data-value attribute. The
  // hover label's .icon-hover-value line (a child of the button, not a
  // sibling -- base.html; .icon-hover-title next to it is the field name
  // and never changes) shows what that value actually means --
  // "Linear"/"Ortho", say -- taken from data-labels (parallel to
  // data-values, same order).
  const cycleBtns = overlay.querySelectorAll('.icon-cycle-btn');
  const syncCycleBtn = (btn) => {
    const values = btn.dataset.values.split(',');
    const value = fields[btn.dataset.field].value || values[0];
    btn.dataset.value = value;
    const label = btn.querySelector('.icon-hover-value');
    if (label && btn.dataset.labels) {
      label.textContent = btn.dataset.labels.split(',')[values.indexOf(value)] ?? label.textContent;
    }
  };
  for (const btn of cycleBtns) {
    btn.addEventListener('click', () => {
      const values = btn.dataset.values.split(',');
      const input = fields[btn.dataset.field];
      input.value = values[(values.indexOf(input.value) + 1) % values.length];
      syncCycleBtn(btn);
    });
  }

  // Citrus mode: an on/off toggle, not a data-values cycle button (see
  // .icon-cycle-btn above) since there are only two states -- but the same
  // "fields.citrus_mode (hidden input) is the source of truth, sync the
  // visible bits from it" shape as everything else here. Its real effect
  // (the irrigation-decision/pump-command pipeline, and the station/
  // Irrigation nav visibility that goes with it) lives entirely server-side
  // -- see weather_mqtt.py's CITRUS_MODE and dashboard/views.py's
  // citrus_on -- so a change here only takes effect after the save-time
  // reload below, same as language.
  const citrusBtn = document.getElementById('cfg-citrus-toggle');
  const citrusLabel = document.getElementById('cfg-citrus-label');
  const syncCitrusBtn = () => {
    if (!citrusBtn || !fields.citrus_mode) return;
    const on = fields.citrus_mode.value === 'on';
    citrusBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (citrusLabel) citrusLabel.textContent = on ? I18N.citrus_on_label : I18N.citrus_off_label;
  };
  citrusBtn?.addEventListener('click', () => {
    fields.citrus_mode.value = fields.citrus_mode.value === 'on' ? 'off' : 'on';
    syncCitrusBtn();
  });

  // Trinity mode: same on/off toggle shape as citrus mode above, but purely
  // a display preference -- see base.html's comment on cfg-trinity-toggle
  // and weather.js's/irrigation.js's applyTrinityMode. Doesn't need the
  // save-time reload citrus mode does; window.dashboardRefresh() below
  // already re-reads /api/status (which carries trinity_mode) and
  // re-renders whichever chart layout that calls for.
  const trinityBtn = document.getElementById('cfg-trinity-toggle');
  const trinityLabel = document.getElementById('cfg-trinity-label');
  const syncTrinityBtn = () => {
    if (!trinityBtn || !fields.trinity_mode) return;
    const on = fields.trinity_mode.value === 'on';
    trinityBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (trinityLabel) trinityLabel.textContent = on ? I18N.trinity_on_label : I18N.trinity_off_label;
  };
  trinityBtn?.addEventListener('click', () => {
    fields.trinity_mode.value = fields.trinity_mode.value === 'on' ? 'off' : 'on';
    syncTrinityBtn();
  });

  // Most browsers keep a button :focus'd after a mouse click (not just
  // keyboard :focus-visible), which kept .icon-hover-label expanded via
  // :focus long after the mouse moved on to a different icon -- both
  // labels open at once, wide enough to push the row past the panel's
  // edge. One delegated listener covers all six icons (lang, sun path
  // style/view, citrus, trinity, clear cache -- each one of these buttons
  // lives directly under .config-icon-row) since by the time this fires the
  // click has already updated whatever value it needed to (button-specific
  // listeners run first, being closer to the actual target). A Tab-focused
  // button still shows its label via :focus same as before -- this only
  // clears focus after a click.
  overlay.querySelector('.config-icon-row')?.addEventListener('click', (e) => {
    e.target.closest('button')?.blur();
  });

  const close = () => overlay.classList.add('hidden');

  // Captured on open so save can tell whether the station actually changed
  // -- that's the one field whose cached weather_cache.json data is
  // genuinely stale/wrong for the new value until a real fetch runs (unlike
  // e.g. sun path style, which just needs a re-read of already-cached data).
  let openedStation = '';
  let openedCitrusMode = '';

  const open = async () => {
    overlay.classList.remove('hidden');
    try {
      const res = await fetch('/api/config');
      const cfg = await res.json();
      for (const key of Object.keys(fields)) {
        fields[key].value = cfg[key] || '';
      }
      setLangDisplay(fields.lang.value || 'en');
      for (const btn of cycleBtns) syncCycleBtn(btn);
      syncCitrusBtn();
      syncTrinityBtn();
      openedStation = (cfg.station || '').trim().toUpperCase();
      openedCitrusMode = cfg.citrus_mode || '';
    } catch (err) {
      console.error('Error loading config:', err);
    }
  };

  openBtn.addEventListener('click', open);
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
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await res.text());
      // Static, server-rendered text only reflects the new language after a
      // full reload -- the modal fields themselves already show it live.
      // Same deal for citrus mode -- it gates server-rendered nav links and
      // config fields (see dashboard/views.py's citrus_on), which only
      // change on the next full page render.
      if (
        fields.lang.value !== document.documentElement.lang ||
        (fields.citrus_mode && fields.citrus_mode.value !== openedCitrusMode)
      ) {
        window.location.reload();
        return;
      }
      const stationChanged = fields.station ? fields.station.value.trim().toUpperCase() !== openedStation : false;
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
