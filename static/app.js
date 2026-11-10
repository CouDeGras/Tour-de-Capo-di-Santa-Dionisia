'use strict';

const I18N = window.I18N || {};
let refreshBusy = false;
let refreshQueued = false;
let refreshFeedback = false;

async function triggerFullRefresh({ silent = false } = {}) {
  const btn = document.getElementById('refresh-btn');
  if (!silent) {
    refreshFeedback = true;
    if (btn) btn.disabled = true;
    window.refreshPopup?.open();
  }
  if (refreshBusy) { refreshQueued = true; return; }
  refreshBusy = true;
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
    if (refreshFeedback) window.refreshPopup?.settle(ok);
    refreshFeedback = false;
  }
}

(() => {
  const btn = document.getElementById('refresh-btn');
  if (!btn) return;
  btn.addEventListener('click', () => { triggerFullRefresh(); });
})();
window.addEventListener('DOMContentLoaded', () => {
  const afterCacheClear = sessionStorage.getItem('weather-update-after-clear') === '1';
  sessionStorage.removeItem('weather-update-after-clear');
  void triggerFullRefresh({ silent: !afterCacheClear });
}, { once: true });
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
(() => {
  const btn = document.getElementById('quit-btn');
  if (!btn || !window.electronAPI) return;
  btn.classList.remove('hidden');
  btn.addEventListener('click', () => {
    if (window.confirm(I18N.quit_confirm)) window.electronAPI.quit();
  });
})();
(() => {
  const overlay = document.getElementById('config-overlay');
  const openBtn = document.getElementById('config-btn');
  const closeBtn = document.getElementById('config-close');
  const cancelBtn = document.getElementById('config-cancel');
  const saveBtn = document.getElementById('config-save');
  if (!overlay || !openBtn) return;
  const fields = {
    station: document.getElementById('cfg-station'),
    lang: document.getElementById('cfg-lang'),
    sun_projection: document.getElementById('cfg-sun-projection'),
    sun_view: document.getElementById('cfg-sun-view'),
    trinity_mode: document.getElementById('cfg-trinity-mode'),
  };
  for (const key of Object.keys(fields)) {
    if (!fields[key]) delete fields[key];
  }

  const stationSlots = [...overlay.querySelectorAll('.station-code-slot')];
  const stationLetters = text => text.toUpperCase().replace(/[^A-Z]/g, '');
  const syncStationValue = () => {
    fields.station.value = stationSlots.map(slot => slot.value).join('');
  };
  const loadStationSlots = () => {
    const code = stationLetters(fields.station.value).slice(0, 4);
    stationSlots.forEach((slot, index) => { slot.value = code[index] || ''; });
    syncStationValue();
  };
  const focusStationSlot = index => {
    stationSlots[index].focus();
    stationSlots[index].select();
  };
  const insertStationLetters = (text, index) => {
    const letters = stationLetters(text).slice(0, 4);
    if (!letters) return;
    const start = letters.length === 4 ? 0 : index;
    [...letters].slice(0, 4 - start).forEach((letter, offset) => {
      stationSlots[start + offset].value = letter;
    });
    syncStationValue();
    focusStationSlot(Math.min(3, start + letters.length));
  };
  stationSlots.forEach((slot, index) => {
    slot.addEventListener('focus', () => slot.select());
    slot.addEventListener('input', () => {
      const letters = stationLetters(slot.value);
      slot.value = '';
      if (letters) insertStationLetters(letters, index);
      else syncStationValue();
    });
    slot.addEventListener('paste', event => {
      event.preventDefault();
      insertStationLetters(event.clipboardData.getData('text'), index);
    });
    slot.addEventListener('keydown', event => {
      if (event.key === 'Backspace' && !slot.value && index > 0) {
        event.preventDefault();
        stationSlots[index - 1].value = '';
        syncStationValue();
        focusStationSlot(index - 1);
      } else if (event.key === 'ArrowLeft' && index > 0) {
        event.preventDefault();
        focusStationSlot(index - 1);
      } else if (event.key === 'ArrowRight' && index < 3) {
        event.preventDefault();
        focusStationSlot(index + 1);
      }
    });
  });
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
  overlay.querySelector('.config-icon-row')?.addEventListener('click', (e) => {
    e.target.closest('button')?.blur();
  });

  const close = () => overlay.classList.add('hidden');
  let openedStation = '';

  const open = async () => {
    overlay.classList.remove('hidden');
    try {
      const res = await fetch('/api/config');
      const cfg = await res.json();
      for (const key of Object.keys(fields)) {
        fields[key].value = cfg[key] || '';
      }
      loadStationSlots();
      setLangDisplay(fields.lang.value || 'it');
      for (const btn of cycleBtns) syncCycleBtn(btn);
      syncTrinityBtn();
      openedStation = (cfg.station || '').trim().toUpperCase();
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
    for (const slot of stationSlots) {
      if (!slot.reportValidity()) { slot.focus(); return; }
    }
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
      if (
        fields.lang.value !== document.documentElement.lang
      ) {
        window.location.reload();
        return;
      }
      const stationChanged = fields.station ? fields.station.value.trim().toUpperCase() !== openedStation : false;
      close();
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
      sessionStorage.setItem('weather-update-after-clear', '1');
      window.location.reload();
    } catch (err) {
      console.error('Error clearing cache:', err);
      window.alert(I18N.clear_cache_failed);
      btn.disabled = false;
    }
  });
})();
