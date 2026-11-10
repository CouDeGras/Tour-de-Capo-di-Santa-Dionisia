'use strict';

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
