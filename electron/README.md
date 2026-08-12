# Capo di Santa Dionisia — Electron builds

The Electron shell supports both the original Linux AppImage and a native
64-bit Windows installer. Each package includes its own Python runtime and
backend dependencies, so target machines do not need Python, Django, or Node
installed.

## Windows build

From PowerShell on a 64-bit Windows machine:

```powershell
cd electron
npm install
npm run build:win
```

This stages a clean backend snapshot plus the official Python 3.11 embeddable
runtime, then produces:

```text
electron/dist/capo-di-santa-dionisia-26.8.12-setup.exe
```

Both Windows and Linux builds generate their version automatically from the
build machine's local calendar date. GitHub releases can use the readable
`vYY.MM.DD` form, such as `v26.08.12`. Electron receives the semver-compatible
equivalent without leading zeroes, such as `26.8.12`, because semantic version
numeric fields cannot begin with zero. The generated version is passed to
electron-builder at build time; `package.json` is not rewritten.

The installer offers an install-location chooser and creates Start Menu and
desktop shortcuts. Runtime data is stored in Electron's per-user application
data directory, normally `%APPDATA%\Capo di Santa Dionisia\`.

The dashboard supports English, French, Italian, Spanish, Traditional Chinese,
and Japanese. Space Grotesk/Space Mono remain the primary theme fonts for
alphanumerics; bundled Noto Sans TC and Noto Sans JP supply only the missing
CJK glyphs so regional text renders consistently without changing the Latin
typography.

Use `npm run stage:win` followed by `npm start` for Windows development mode.
The staging command downloads its pinned Python runtime and installs the Python
requirements, so it needs internet access when run. If npm has `proxy` or
`https-proxy` configured, the Windows build wrapper also passes that setting to
electron-builder's downloader.

Windows dependency caching is persistent: after the first build,
`build-backend-win.ps1` reuses the staged embedded-Python runtime without
running pip or downloading wheels. Python archives, pip bootstrap files, and
pip's wheel/HTTP cache live under `electron/.build-cache/windows/`. The runtime
is rebuilt automatically only when `requirements.txt`, the pinned Python
version, or the cache schema changes. Use
`./build-backend-win.ps1 -RefreshRuntime` only when a manual refresh is needed.

## Linux AppImage build

Packages the Django dashboard + `weather_mqtt.py --service` (the same code
the `saignes-dashboard.service`/`saignes-weather.service` systemd units run)
into a single self-contained `.AppImage`, with its own bundled Python venv
so the target machine doesn't need Python/Django/etc. pre-installed. Runs
standalone — no separate always-on service needed elsewhere. The existing
systemd deployment is untouched by any of this; this is an additional
packaging target, not a replacement.

### Build

```sh
cd electron
npm install
npm run build        # stages the backend (build-backend.sh) then runs electron-builder
```

Produces `electron/dist/capo-di-santa-dionisia-<version>.AppImage` (the
`artifactName` build config pins this to the hyphenated npm `name`, not the
spaced `productName` "Capo di Santa Dionisia" -- keeps the actual filename
easy to `chmod`/execute without quoting).

`npm run build` always wipes and rebuilds `electron/resources/` from
scratch first (via `build-backend.sh`) — it copies a clean snapshot of
`core/`, `dashboard/`, `static/`, `templates/`, `weather_mqtt.py`,
`manage.py`, `requirements.txt` from the parent project, and builds a fresh
venv from `requirements.txt`. Nothing from this dev machine's own
`data/`/`db.sqlite3` is ever included.

**Glibc caveat**: the bundled venv's Python is whatever `python3 -m venv`
resolves to on the *build* machine — build on a reasonably old/compatible
base (e.g. Ubuntu 20.04/22.04) if you want the AppImage to run on a wide
range of target distros, and spot-check on a couple before distributing.

### Run

```sh
chmod +x capo-di-santa-dionisia-*.AppImage
./capo-di-santa-dionisia-*.AppImage
```

First launch: runs `manage.py migrate`, starts the dashboard on
`127.0.0.1:8090` (loopback only, and deliberately not 8080 — the existing
systemd deployment's `saignes-dashboard.service` already binds `0.0.0.0:8080`
there, so this needs its own port to run alongside it; also loopback-only
since every `/api/*` endpoint including config writes is unauthenticated)
and `weather_mqtt.py --service`, then opens a window once the dashboard
responds. On a completely fresh data directory, `--service` runs its first
full fetch immediately rather than waiting for the next scheduled boundary
(see `run_service()`'s bootstrap check), so a new install shows real data
within moments instead of sitting in demo mode for up to 3 hours. All
runtime data (the SQLite DB, weather cache, site config, etc.) lives under
`~/.config/Capo di Santa Dionisia/` (`app.getPath('userData')`, derived from
`productName` -- yes, with a literal space; quote it in shell commands),
not inside the AppImage itself. This path changed with the rename (it used
to be `~/.config/Saignes-en-Padaine/`) -- an existing install's data does
not carry over automatically; copy the old directory's contents across if
you want to keep it.

**Closing the window does not stop the app** — it hides to the tray icon,
because `weather_mqtt.py --service` is what actually publishes irrigation
commands on its own schedule and should keep running whether or not the
window is open. Use the tray icon's "Quit" to actually stop both backend
processes.

### Dev mode (without packaging)

```sh
cd electron
bash build-backend.sh   # stage resources/app + resources/venv once
npm install
npm start                # electron . -- reads from ./resources/ directly
```

## Not yet done
- Auto-launch on login (would need a `.desktop` file in
  `~/.config/autostart/` — not wired up by this build).
- macOS packaging.
