'use strict';

// AppImage's stock AppRun script does `exec "$BIN" "${args[@]}"` -- a raw
// double-click launch passes zero args, and the setuid chrome-sandbox
// helper can never be configured correctly inside an AppImage (no install
// step ever runs as root to set its required root:root/4755 ownership), so
// that raw invocation hard-crashes with "No usable sandbox!". Baking
// --no-sandbox into main.js itself doesn't help either -- Chromium's
// sandbox host makes this decision natively, before any of main.js's JS
// runs at all. electron-builder's own default (`Exec=AppRun --no-sandbox
// %U` in the bundled .desktop file) doesn't help a raw double-click either,
// since AppRun ignores that file entirely and execs the binary directly.
//
// So: rename the real Electron binary to <name>.bin and put a tiny wrapper
// script in its place (matching the name AppRun's $BIN expects) that always
// adds --no-sandbox before exec'ing the real binary. This runs after
// electron-builder stages the unpacked app (appOutDir) but before it gets
// squashfs'd into the final AppImage, so the wrapper ships as part of the
// image itself -- no CLI flag needed, ever, regardless of how it's launched.
const fs = require('fs');
const path = require('path');

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== 'linux') return;

  const exeName = context.packager.executableName;
  const realBin = path.join(context.appOutDir, exeName);
  const renamedBin = `${realBin}.bin`;

  fs.renameSync(realBin, renamedBin);
  fs.writeFileSync(
    realBin,
    `#!/bin/bash\nDIR="$(dirname "$(readlink -f "$0")")"\nexec "$DIR/${exeName}.bin" --no-sandbox "$@"\n`,
    { mode: 0o755 },
  );
};
