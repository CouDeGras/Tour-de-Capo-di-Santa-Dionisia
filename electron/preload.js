'use strict';

// Runs in the renderer's isolated world (contextIsolation: true in main.js's
// BrowserWindow, so the dashboard page itself has no direct Node/Electron
// access) -- this is the only bridge between the two. Exposes exactly one
// thing: a way for the page to ask the main process to quit. Everything
// else the dashboard does (weather data, config, irrigation history) goes
// through the existing HTTP /api/* endpoints Django already serves, same as
// it would talking to the systemd deployment in a normal browser; only
// quitting needs this, since that's an Electron-process-level action no
// HTTP endpoint could reach.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  quit: () => ipcRenderer.send('app-quit'),
  // Settings panel's "Clear cache" button also needs this: POST /api/clear-cache
  // (see dashboard/services.py) only wipes the Django-side DB/JSON caches --
  // it can't reach the renderer's own session-level HTTP cache/localStorage,
  // which is a separate thing Electron keeps on top of whatever the server
  // returns. invoke (not send) since app.js awaits this before reloading.
  clearCache: () => ipcRenderer.invoke('clear-cache'),
});
