'use strict';

// Electron shell for the bundled AppImage build: spawns the same Django
// dashboard + weather_mqtt.py --service the systemd deployment runs, points
// them at a real writable per-user data directory
// (CAPO_DI_SANTA_DIONISIA_DATA_DIR), waits for the dashboard to answer,
// then shows it in a window. Tray-resident:
// closing the window hides it rather than quitting, since weather_mqtt.py
// --service is what actually commands pumps on a schedule -- it should keep
// running even if nobody has the window open, not stop the moment it's
// closed. See ../.claude/plans (or the PR this shipped in) for the full
// design rationale.

const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain } = require('electron');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

const DASHBOARD_HOST = '127.0.0.1';
// Deliberately NOT 8080 -- the existing systemd deployment's
// saignes-dashboard.service already binds 0.0.0.0:8080 there, which also
// claims 127.0.0.1:8080. Using the same port here meant this app's own
// `manage.py runserver` silently failed to bind whenever the systemd
// service was already running on the same machine (as it normally would
// be, for anyone testing the AppImage build without having torn the real
// deployment down first) -- and readiness-polling this URL would then just
// see the *other* server respond, making the failure invisible.
const DASHBOARD_PORT = 8090;
const DASHBOARD_URL = `http://${DASHBOARD_HOST}:${DASHBOARD_PORT}/`;

// In the packaged AppImage, electron-builder's extraResources land under
// process.resourcesPath. In dev (`electron .` from this directory without
// packaging), fall back to a sibling resources/ folder you'd stage locally
// with build-backend.sh -- same layout either way.
const RESOURCES_DIR = app.isPackaged ? process.resourcesPath : path.join(__dirname, 'resources');
const PROJECT_DIR = path.join(RESOURCES_DIR, 'app');
// Linux staging creates a conventional venv; Windows staging uses the
// official embeddable Python distribution so an installed app does not
// depend on Python (or this build machine's Python) being present.
const VENV_PYTHON = process.platform === 'win32'
  ? path.join(RESOURCES_DIR, 'venv', 'python.exe')
  : path.join(RESOURCES_DIR, 'venv', 'bin', 'python3');

let mainWindow = null;
let tray = null;
let isQuitting = false;
const childProcesses = [];

function appDataDir() {
  // app.getPath('userData') is ~/.config/<app name>/ on Linux -- a real,
  // writable, per-user location, unlike the AppImage's own read-only mount.
  return app.getPath('userData');
}

function runPython(args, { waitForExit = false } = {}) {
  const child = spawn(VENV_PYTHON, args, {
    cwd: PROJECT_DIR,
    env: { ...process.env, CAPO_DI_SANTA_DIONISIA_DATA_DIR: appDataDir() },
    // Prevent bundled Python processes from opening console windows behind
    // the Electron UI on Windows. This is ignored on other platforms.
    windowsHide: true,
  });
  child.stdout.on('data', (d) => process.stdout.write(`[${args[0]}] ${d}`));
  child.stderr.on('data', (d) => process.stderr.write(`[${args[0]}] ${d}`));

  if (!waitForExit) {
    childProcesses.push(child);
    return child;
  }
  return new Promise((resolve, reject) => {
    child.on('exit', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${args.join(' ')} exited with code ${code}`));
    });
    child.on('error', reject);
  });
}

function waitForDashboard(timeoutMs = 30000, intervalMs = 300) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const req = http.get(DASHBOARD_URL, (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (Date.now() > deadline) {
          reject(new Error('Dashboard did not come up in time'));
          return;
        }
        setTimeout(tryOnce, intervalMs);
      });
    };
    tryOnce();
  });
}

async function startBackend() {
  // Schema first -- weather_mqtt.py's ORM calls (IrrigationDecision,
  // MetarReading) and the dashboard's own queries would fail against an
  // unmigrated (or freshly-created, on first run) database otherwise.
  await runPython(['manage.py', 'migrate', '--noinput'], { waitForExit: true });

  // --noreload: same reasoning as the systemd deployment's ExecStart --
  // avoids the autoreloader forking a child process Electron wouldn't know
  // to track/kill. 127.0.0.1 only: this is a single-machine desktop app,
  // and every /api/* endpoint (including config writes) is unauthenticated.
  runPython(['manage.py', 'runserver', '--noreload', `${DASHBOARD_HOST}:${DASHBOARD_PORT}`]);
  runPython(['weather_mqtt.py', '--service']);

  await waitForDashboard();
}

function createWindow(startupError) {
  // Opt-in via CAPO_DI_SANTA_DIONISIA_KIOSK so normal windowed dev/testing
  // (e.g. on this machine) is unaffected -- a dedicated-hardware deployment
  // (a Pi next to the garden, say) would set this in whatever launches the
  // AppImage on boot. kiosk:true only removes window chrome/forces
  // fullscreen; it does NOT disable the tray "Quit", default Electron
  // accelerators (Ctrl+Q, etc.), or OS/WM-level shortcuts -- none of those
  // are touched here.
  const kiosk = process.env.CAPO_DI_SANTA_DIONISIA_KIOSK === '1';
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    icon: path.join(__dirname, 'build', 'icon.png'),
    kiosk,
    webPreferences: { contextIsolation: true, preload: path.join(__dirname, 'preload.js') },
  });

  // F11 toggles kiosk mode at runtime, on top of the CAPO_DI_SANTA_DIONISIA_KIOSK
  // launch-time default above. Scoped to this window's own input (before-input-event)
  // rather than globalShortcut.register, which would steal F11 system-wide even
  // while some other app has focus.
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.key === 'F11') {
      mainWindow.setKiosk(!mainWindow.isKiosk());
      event.preventDefault();
    }
  });

  if (startupError) {
    // Load an inline error page instead of the dashboard URL -- silently
    // loading a blank/connection-refused page here is exactly how the
    // port-8080 conflict with the systemd deployment went unnoticed
    // earlier: the failure needs to be visible, not just logged to a
    // terminal nobody's watching.
    const message = String(startupError.message || startupError).replace(/</g, '&lt;');
    mainWindow.loadURL('data:text/html,' + encodeURIComponent(
      `<body style="font-family:sans-serif;background:#111;color:#eee;padding:2rem">
        <h2>Backend failed to start</h2><pre>${message}</pre>
        <p>Check the terminal this AppImage was launched from for details.</p></body>`
    ));
  } else {
    // Clear the persistent HTTP cache before every load -- the window's
    // session survives across rebuilds (it lives in userData, not the
    // AppImage mount), so without this a rebuilt static/template file can
    // sit next to a stale cached one indefinitely (e.g. new HTML markup
    // paired with old CSS that doesn't know about it). There's no upside
    // to caching a page that only ever talks to 127.0.0.1 anyway.
    mainWindow.webContents.session.clearCache().finally(() => {
      mainWindow.loadURL(DASHBOARD_URL);
    });
  }

  // Hide, don't quit -- the backend (specifically weather_mqtt.py --service,
  // which publishes irrigation commands on its own schedule) should keep
  // running whether or not the window is open. Only the tray's "Quit"
  // actually stops it.
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function quitApp() {
  isQuitting = true;
  app.quit();
}

function createTray() {
  const icon = nativeImage.createFromPath(path.join(__dirname, 'build', 'icon.png'));
  tray = new Tray(icon.resize({ width: 32, height: 32 }));
  tray.setToolTip('Capo di Santa Dionisia');
  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: 'Open Dashboard', click: () => {
        if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
      },
    },
    { type: 'separator' },
    { label: 'Quit', click: quitApp },
  ]));
  tray.on('click', () => {
    if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
  });
}

// The dashboard page itself has no other way to reach this -- see
// preload.js's contextBridge and static/app.js's window.electronAPI check.
// Confirmation happens on the renderer side (a plain browser confirm())
// before this ever fires, so no dialog is needed here too.
ipcMain.on('app-quit', quitApp);

// Settings panel's "Clear cache" button (static/app.js): clears the
// window's own HTTP cache and storage (localStorage/IndexedDB/cookies) --
// the same session mainWindow.webContents.session.clearCache() already
// wipes on every load in createWindow() above, just triggered on demand
// instead of only at startup, plus clearStorageData for anything the page
// itself wrote client-side. Handle (not on) since the renderer awaits this
// before reloading -- clearing storage out from under a still-loaded page
// would be a bad time to skip synchronizing.
ipcMain.handle('clear-cache', async () => {
  if (!mainWindow) return;
  const ses = mainWindow.webContents.session;
  await ses.clearCache();
  await ses.clearStorageData();
});

function killBackend() {
  for (const child of childProcesses) {
    if (!child.killed) child.kill('SIGTERM');
  }
}

// Prevent a second AppImage launch from spawning duplicate backend
// processes and fighting over port 8080 -- focus the existing window
// instead.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
  });

  app.whenReady().then(async () => {
    createTray();
    let startupError = null;
    try {
      await startBackend();
    } catch (err) {
      console.error('Backend failed to start:', err);
      startupError = err;
    }
    createWindow(startupError);
  });

  // Deliberately empty: on Linux/Windows Electron's default is to quit when
  // all windows close, but this app is tray-resident -- closing the window
  // (handled above) already prevents this from firing during normal use;
  // this override just makes the tray-resident intent explicit rather than
  // relying on the close handler alone.
  app.on('window-all-closed', () => {});

  app.on('before-quit', () => {
    isQuitting = true;
    killBackend();
  });
}
