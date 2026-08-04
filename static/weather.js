'use strict';

// I18N is declared once in app.js (loaded first, same global scope) -- not
// redeclared here.

// ── Local clock (station's timezone, from /api/status's location.tz) ──────────

let stationTz = null;

function pad(n) { return String(Math.floor(n)).padStart(2, '0'); }

function tickLocalClock() {
  const timeEl = document.getElementById('local-time');
  const dateEl = document.getElementById('local-date');
  if (!timeEl) return;
  if (!stationTz) { timeEl.textContent = '—'; if (dateEl) dateEl.textContent = '—'; return; }
  const now = new Date();
  try {
    timeEl.textContent = new Intl.DateTimeFormat('en-GB', {
      timeZone: stationTz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).format(now);
    if (dateEl) {
      dateEl.textContent = new Intl.DateTimeFormat(undefined, {
        timeZone: stationTz, weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
      }).format(now);
    }
  } catch (err) {
    // Unknown/invalid IANA tz name -- leave the last good value on screen
    // rather than blank it every tick.
    console.error('Error formatting local time:', err);
  }
}

setInterval(tickLocalClock, 1000);

// ── Location widget ─────────────────────────────────────────────────────────

function renderLocation(location) {
  const loc = location || {};
  const nameEl = document.getElementById('loc-name');
  const coordsEl = document.getElementById('loc-coords');
  if (!nameEl || !coordsEl) return;
  nameEl.textContent = loc.city || loc.station_name || loc.station || '—';
  coordsEl.textContent = (loc.lat != null && loc.lon != null)
    ? `${loc.lat.toFixed(4)}°, ${loc.lon.toFixed(4)}°`
    : '—';
}

// ── Sun path widget ──────────────────────────────────────────────────────────
//
// Entirely client-side (Date.now() + the station's lat/lon, no server round
// trip) -- same NOAA/Spencer declination + equation-of-time series
// weather_mqtt.py already uses for sunrise/sunset quantization, extended to
// full elevation/azimuth. Projected orthographically from directly overhead
// (r = cos(elevation), angle = azimuth from North) rather than
// stereographically: that's what makes the day's path degenerate to a true
// circle only in the polar-latitude limit and a non-circular arc everywhere
// else, matching how the sky actually looks from above -- a stereographic
// projection would stay a perfect circle at every latitude instead, which
// would misrepresent it. cos(elevation) alone can't distinguish above- from
// below-horizon (cos is symmetric), so below-horizon samples are dropped
// explicitly rather than relying on the projected radius to exclude them.
//
// Both radius mappings (elevToRadiusFraction below) share one more choice,
// independent of them: `view` ('down', the default, or 'up'). Every azimuth
// placement in this file goes through azUnit() so the whole widget flips
// together. 'down' is the standard map convention -- N top, E right, as if
// looking down at the sky dome from outside it. 'up' mirrors E/W (N stays
// top, S stays bottom) to match the convention planetarium/architectural sun
// charts actually use when you're meant to hold the chart overhead and
// compare it to the real sky: standing on the ground looking up, East is on
// your left when North is ahead of you. The current-sun marker encodes the
// same choice as a ray direction, borrowing the physics convention for a
// vector along the viewing axis: a dot (⊙) under 'up' means the sun's ray is
// travelling toward this viewpoint (down out of the sky onto the ground
// you're standing on); a cross (⊗) under 'down' means it's travelling away
// from this viewpoint (down out of the sky, past you, to the ground below).

function sunDeclEqtime(date) {
  const start = Date.UTC(date.getUTCFullYear(), 0, 1);
  const n = Math.floor((date.getTime() - start) / 86400000) + 1;
  const gamma = (2 * Math.PI / 365) * (n - 1 + 0.5);
  const eqtime = 229.18 * (
    0.000075
    + 0.001868 * Math.cos(gamma)
    - 0.032077 * Math.sin(gamma)
    - 0.014615 * Math.cos(2 * gamma)
    - 0.040849 * Math.sin(2 * gamma)
  );
  const decl = (
    0.006918
    - 0.399912 * Math.cos(gamma)
    + 0.070257 * Math.sin(gamma)
    - 0.006758 * Math.cos(2 * gamma)
    + 0.000907 * Math.sin(2 * gamma)
    - 0.002697 * Math.cos(3 * gamma)
    + 0.00148 * Math.sin(3 * gamma)
  );
  return { decl, eqtime }; // decl in radians, eqtime in minutes
}

// Elevation/azimuth (both degrees) for a given hour angle (degrees, 0 at
// solar noon). Azimuth measured clockwise from North.
function elevAzFromHourAngle(latDeg, decl, haDeg) {
  const lat = latDeg * Math.PI / 180;
  const ha = haDeg * Math.PI / 180;
  let cosZenith = Math.sin(lat) * Math.sin(decl) + Math.cos(lat) * Math.cos(decl) * Math.cos(ha);
  cosZenith = Math.max(-1, Math.min(1, cosZenith));
  const zenith = Math.acos(cosZenith);
  const elevation = 90 - zenith * 180 / Math.PI;
  const sinZenith = Math.sin(zenith);
  let az;
  if (sinZenith < 1e-9) {
    az = 180;
  } else {
    let cosAz = (Math.sin(decl) - Math.sin(lat) * cosZenith) / (Math.cos(lat) * sinZenith);
    cosAz = Math.max(-1, Math.min(1, cosAz));
    az = Math.acos(cosAz) * 180 / Math.PI;
    if (haDeg > 0) az = 360 - az;
  }
  return { elevation, az };
}

function hourAngleNow(lonDeg, eqtime, date) {
  const utcMin = date.getUTCHours() * 60 + date.getUTCMinutes() + date.getUTCSeconds() / 60;
  const tst = ((utcMin + eqtime + 4 * lonDeg) % 1440 + 1440) % 1440;
  return tst / 4 - 180;
}

// Inverse of hourAngleNow: the station-local clock time (HH:MM) for a given
// hour angle on refDate's UTC calendar day. Only the time-of-day is used by
// callers (sunrise/sunset labels), never the reconstructed date -- a fixed
// UTC offset makes the extracted hour:minute correct regardless of whether
// this lands the instant on refDate's actual UTC day or the one next to it.
function hourAngleToLocalClock(haDeg, lonDeg, eqtime, refDate, tzName) {
  if (!tzName) return null;
  const utcMin = (((haDeg + 180) * 4 - eqtime - 4 * lonDeg) % 1440 + 1440) % 1440;
  const dayStart = Date.UTC(refDate.getUTCFullYear(), refDate.getUTCMonth(), refDate.getUTCDate());
  const instant = new Date(dayStart + utcMin * 60000);
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: tzName, hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(instant);
  } catch (err) {
    return null;
  }
}

// Fills a bg-colored box behind `text` before drawing it in fg, so it stays
// legible regardless of whatever curve/ring/other label happens to be
// underneath -- computing non-overlapping positions for a dynamic day-path
// curve that changes shape with date/latitude isn't worth it when simply
// masking out the small patch behind each label is enough. Assumes the
// caller already set textAlign='center'/textBaseline='middle'.
function haloText(ctx, text, x, y, fg, bg) {
  const w = ctx.measureText(text).width;
  ctx.fillStyle = bg;
  ctx.fillRect(x - w / 2 - 1.5, y - 4.5, w + 3, 9);
  ctx.fillStyle = fg;
  ctx.fillText(text, x, y);
}

// Radius (as a 0..1 fraction of the horizon radius) for a given elevation.
// 'linear' (equidistant/polar-equal-angle: 90deg-elev at the center down to
// 0deg at the horizon) keeps equal elevation steps evenly spaced.
// 'orthographic' (cos(elevation), literal bird's-eye view of the sky)
// crowds space near the horizon and stretches it near the zenith instead.
// Both degenerate to a true circle in the polar-latitude limit and a
// non-circular arc otherwise -- see the config panel's "Sun path style".
function elevToRadiusFraction(elevation, projection) {
  return projection === 'orthographic'
    ? Math.cos(elevation * Math.PI / 180)
    : (1 - elevation / 90);
}

// Unit direction vector (canvas x/y, N up) for a compass azimuth, under
// either viewing convention -- see the widget's top comment. Every azimuth
// placement in drawSunPath (the day-path curve, ring labels, compass
// labels, sunrise/sunset ticks, the current-sun marker) goes through this so
// 'up' flips the whole picture consistently instead of just the curve.
function azUnit(azDeg, view) {
  const azRad = azDeg * Math.PI / 180;
  const ewSign = view === 'up' ? -1 : 1;
  return [ewSign * Math.sin(azRad), -Math.cos(azRad)];
}

// Shared by every stroke in the sun-path widget -- horizon, elevation
// rings, the day-path curve, sunrise/sunset ticks, and the ray glyphs below
// -- so nothing in the diagram reads as heavier or lighter than anything
// else, regardless of how big the shape it's drawing is.
const SUN_WIDGET_LINE_WIDTH = 1.5;

// Radius shared by the current-sun marker and the zenith reticule -- also
// used to size the current-position label's clearance from the marker (see
// the offset calc in drawSunPath) so that clearance stays correct if this
// ever changes.
const SUN_GLYPH_RADIUS = 6;

// The ray-direction glyph (see the widget's top comment): a disc filled in
// the canvas's own background color -- opaque, so it occludes whatever's
// drawn underneath it (the day-path curve very often runs right behind the
// current-sun marker) by painting over it with the same color as the
// canvas around it -- outlined in `color`, with a dot at its center under
// 'up' (ray toward this viewpoint) or a cross under 'down' (ray away from
// it), also in `color` so they read against the background-filled disc.
// Shared by the current-sun marker and the zenith reticule so "match the
// sun style" is enforced by construction rather than two copies that can
// drift -- callers only vary position/color/size. `ringRadius` sets the
// disc's radius; everything else is scaled off SUN_WIDGET_LINE_WIDTH (the
// same width used everywhere else in the widget) so the mark stays
// proportional to the rest of the diagram no matter how big the disc is:
//   - the outline stroke uses that width;
//   - the dot's diameter equals that width;
//   - the cross's diagonal reach is set so each arm's tip lands exactly on
//     the disc's edge (distance reach*sqrt(2) from center), so there's no
//     gap between the cross and the rim.
function drawRayGlyph(ctx, px, py, view, color, ringRadius, bgColor) {
  // Snapped to the nearest half-pixel, same hairline-crispening idiom used
  // for the grid chart's tick marks -- at this glyph's small radius, an
  // un-snapped (sub-pixel) center spreads the disc/mark's anti-aliasing
  // across an extra row of pixels, which is what was reading as a faint,
  // partly-transparent mark instead of a solid opaque one.
  px = Math.round(px) + 0.5;
  py = Math.round(py) + 0.5;
  const lineWidth = SUN_WIDGET_LINE_WIDTH;

  ctx.fillStyle = bgColor;
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.beginPath();
  ctx.arc(px, py, ringRadius, 0, 2 * Math.PI);
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = color;
  ctx.strokeStyle = color;
  if (view === 'up') {
    ctx.beginPath();
    ctx.arc(px, py, lineWidth * 0.75, 0, 2 * Math.PI);
    ctx.fill();
  } else {
    const reach = ringRadius / Math.SQRT2;
    ctx.beginPath();
    ctx.moveTo(px - reach, py - reach); ctx.lineTo(px + reach, py + reach);
    ctx.moveTo(px - reach, py + reach); ctx.lineTo(px + reach, py - reach);
    ctx.stroke();
  }
}

function drawSunPath(canvasId, latDeg, lonDeg, projection, view) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const pw = canvas.parentElement ? (canvas.parentElement.clientWidth || 200) : 200;
  const size = Math.max(200, Math.min(pw, 350));
  const PAD = 18;
  const R = size / 2 - PAD;
  const cx = size / 2, cy = size / 2;

  canvas.width  = size * dpr;
  canvas.height = size * dpr;
  canvas.style.width  = size + 'px';
  canvas.style.height = size + 'px';

  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const { fg, bg, hist } = getColors();
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, size, size);

  if (latDeg == null || lonDeg == null) return;

  const toXY = (elevation, az) => {
    const r = R * elevToRadiusFraction(elevation, projection);
    const [ux, uy] = azUnit(az, view);
    return [cx + r * ux, cy + r * uy];
  };

  // All strokes (horizon, elevation rings, day-path curve, sunrise/sunset
  // ticks) are drawn first, and every text label goes on top afterward in
  // one final pass -- otherwise a label drawn early (e.g. the "30°" ring
  // label) could get a later stroke (the day-path curve very often crosses
  // that same NE-diagonal spot at some point in the year) drawn right
  // through it, defeating its own halo. Halos only protect against what's
  // already on the canvas, not what gets drawn afterward.
  const labels = []; // { text, x, y, font }

  // Horizon (outer boundary) + faint elevation reference rings.
  ctx.strokeStyle = fg;
  ctx.lineWidth = SUN_WIDGET_LINE_WIDTH;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.stroke();
  ctx.setLineDash([2, 3]);
  for (const elev of [30, 60]) {
    ctx.beginPath();
    ctx.arc(cx, cy, R * elevToRadiusFraction(elev, projection), 0, 2 * Math.PI);
    ctx.stroke();
    const [lx, ly] = toXY(elev, 45);
    labels.push({ text: elev + '°', x: lx, y: ly, font: '9px monospace' });
  }
  ctx.setLineDash([]);

  // Compass labels -- via azUnit so 'up' swaps E/W screen positions along
  // with everything else, instead of just the curve flipping underneath
  // fixed N/S/E/W text.
  for (const [text, az] of [['N', 0], ['S', 180], ['E', 90], ['W', 270]]) {
    const [ux, uy] = azUnit(az, view);
    labels.push({ text, x: cx + ux * (R + 7), y: cy + uy * (R + 7), font: '10px monospace' });
  }

  const now = new Date();
  const { decl, eqtime } = sunDeclEqtime(now);

  // Today's path: one declination reused across the sweep (it barely moves
  // in a day), only stroked where the sun is actually above the horizon --
  // that's what truncates the curve at the horizon, and what leaves nothing
  // drawn at all through a polar night, or a closed loop through a polar day.
  // Also tracks the two horizon crossings (elevation sign change) along the
  // way -- the unimodal elevation-vs-hour-angle curve crosses upward at most
  // once (sunrise) and downward at most once (sunset) per day, so this
  // single pass is enough to find both, interpolated to the exact azimuth
  // and hour angle where elevation hits zero rather than snapping to the
  // nearest sample.
  ctx.strokeStyle = fg;
  ctx.lineWidth = SUN_WIDGET_LINE_WIDTH;
  ctx.beginPath();
  let drawing = false;
  let prevElev = null, prevAz = null, prevHa = null;
  let riseAz = null, setAz = null, riseHa = null, setHa = null;
  for (let ha = -180; ha <= 180; ha += 2) {
    const { elevation, az } = elevAzFromHourAngle(latDeg, decl, ha);
    if (prevElev != null) {
      if (prevElev < 0 && elevation >= 0) {
        const t = -prevElev / (elevation - prevElev);
        riseAz = prevAz + t * (az - prevAz);
        riseHa = prevHa + t * (ha - prevHa);
      } else if (prevElev >= 0 && elevation < 0) {
        const t = prevElev / (prevElev - elevation);
        setAz = prevAz + t * (az - prevAz);
        setHa = prevHa + t * (ha - prevHa);
      }
    }
    prevElev = elevation; prevAz = az; prevHa = ha;
    if (elevation < 0) { drawing = false; continue; }
    const [x, y] = toXY(elevation, az);
    if (drawing) ctx.lineTo(x, y); else { ctx.moveTo(x, y); drawing = true; }
  }
  ctx.stroke();

  // Sunrise/sunset azimuth ticks + labels (degrees + local clock time),
  // right at the horizon.
  for (const [rawAz, rawHa] of [[riseAz, riseHa], [setAz, setHa]]) {
    if (rawAz == null) continue;
    const azDeg = ((rawAz % 360) + 360) % 360;
    const [ux, uy] = azUnit(azDeg, view);
    ctx.strokeStyle = fg;
    ctx.lineWidth = SUN_WIDGET_LINE_WIDTH;
    ctx.beginPath();
    ctx.moveTo(cx + ux * R, cy + uy * R);
    ctx.lineTo(cx + ux * (R + 5), cy + uy * (R + 5));
    ctx.stroke();
    const lx = cx + ux * (R + 12), ly = cy + uy * (R + 12);
    const timeStr = hourAngleToLocalClock(rawHa, lonDeg, eqtime, now, stationTz);
    labels.push({ text: Math.round(azDeg) + '°', x: lx, y: ly - (timeStr ? 5 : 0), font: '9px monospace' });
    if (timeStr) labels.push({ text: timeStr, x: lx, y: ly + 5, font: '9px monospace' });
  }

  // Current sun position -- only when actually above the horizon, same
  // reasoning as the path itself. Glyph encodes the viewing convention as a
  // ray direction (see the widget's top comment and drawRayGlyph): a ring
  // with a dot under 'up' (ray toward this viewpoint), a ring with a cross
  // under 'down' (ray away from it). Its elevation/azimuth readout is
  // offset inward (towards the circle's center) rather than a fixed
  // direction, so it stays inside the
  // widget even when the sun sits right near the horizon edge.
  const haNow = hourAngleNow(lonDeg, eqtime, now);
  const nowPos = elevAzFromHourAngle(latDeg, decl, haNow);
  if (nowPos.elevation >= 0) {
    const [x, y] = toXY(nowPos.elevation, nowPos.az);
    drawRayGlyph(ctx, x, y, view, fg, SUN_GLYPH_RADIUS, bg);

    // Pulled clear of the marker itself along the same direction -- towards
    // the zenith at the widget's center -- rather than a fixed screen
    // direction, so the label always reads on the side of the marker
    // that's guaranteed to be inside the widget. A fraction of the sky
    // globe's own radius R (not the marker's radius), so the offset scales
    // with the whole widget as it resizes rather than staying pinned to the
    // marker's fixed size. Applied unconditionally, regardless of how close
    // the sun already is to the zenith -- unlike a distance-to-center cap,
    // this can't shrink toward zero and let the label land on the marker's
    // opaque, occluding disc; it just overshoots straight past the zenith
    // point when the sun is that close, which is fine since nothing else is
    // drawn there for it to land on.
    const dx = cx - x, dy = cy - y;
    const norm = Math.hypot(dx, dy) || 1;
    const offset = R * 0.3;
    const lx = x + (dx / norm) * offset, ly = y + (dy / norm) * offset;
    labels.push({ text: `${Math.round(nowPos.elevation)}°/${Math.round(nowPos.az)}°`, x: lx, y: ly, font: '9px monospace' });
  }

  // All labels last, on top of every stroke -- drawn in the order added
  // above, so the current-position readout (added last) wins any remaining
  // label-vs-label overlap, e.g. right around sunrise/sunset.
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (const l of labels) {
    ctx.font = l.font;
    haloText(ctx, l.text, l.x, l.y, fg, bg);
  }

  // Zenith reticule -- marks the exact overhead point (elevation 90, the
  // widget's center under either projection). Same ring+dot/cross glyph and
  // size as the current-sun marker (drawRayGlyph, same view-driven meaning),
  // just in the muted --hist gray already used elsewhere for
  // reference/historic marks rather than fg -- reads as a fixed landmark,
  // not another live sun position. Drawn dead last, after even the labels,
  // so it's never obscured by the day-path curve or the current-position
  // marker passing right through the center at high-elevation times.
  drawRayGlyph(ctx, cx, cy, view, hist, SUN_GLYPH_RADIUS, bg);
}

// ── Shared color helper (reads CSS vars so dark/light mode works) ─────────────

function getColors() {
  const cs = getComputedStyle(document.documentElement);
  return {
    fg:    cs.getPropertyValue('--b').trim() || '#fff',
    bg:    cs.getPropertyValue('--w').trim() || '#000',
    irrig: cs.getPropertyValue('--irrig').trim() || '#1b7a1b',
    hist:  cs.getPropertyValue('--hist').trim() || '#888',
  };
}

// Continuous (fractional) column index for an arbitrary epoch, found by
// linear interpolation between the two bracketing row epochs. Columns are
// evenly spaced in the grid regardless of the real time gap between rows, so
// this is the only way to place a mark at its true time rather than
// snapping to the nearest column. Returns null when the epoch falls outside
// the plotted range entirely -- callers must not draw a mark for it, since
// clamping to column 0 / the last column would misrepresent an event that's
// actually before or after the chart's timescale as happening right at its
// edge.
function colForEpoch(epochs, epoch) {
  if (!epochs.length) return null;
  if (epoch < epochs[0]) return null;
  const last = epochs.length - 1;
  if (epoch > epochs[last]) return null;
  for (let i = 0; i < last; i++) {
    const a = epochs[i], b = epochs[i + 1];
    if (epoch <= b) {
      const t = b > a ? (epoch - a) / (b - a) : 0;
      return i + t;
    }
  }
  return last;
}

// ── Shared column layout (used by both chart types for identical x-axis) ──────

function colLayout(parentElem, nCols) {
  const pw   = parentElem ? (parentElem.clientWidth || 400) : 400;
  const ML   = 36;
  const CELL = Math.max(4, Math.min(8, Math.floor((pw - ML) / nCols)));
  const GAP  = Math.max(1, Math.floor(CELL * 0.15));
  const colW = CELL + GAP;
  return { ML, CELL, GAP, colW, cssW: ML + nCols * colW };
}

// ── Grid chart (precipitation, single series) ─────────────────────────────────
//
// GitHub-contribution style: filled black cell = value; outlined white = empty.
// Cell size adapts to fill the container width.
//
// This is the standalone weather page, so opts.eventMarks is never passed
// here -- the irrigation-scheduling overlay this function still supports
// (see the eventMarks block below) is simply dormant on this page. Left in
// place rather than hand-trimmed out, to avoid risking a mistake in this
// carefully-tuned canvas math for a branch that's already harmless when unused.

function drawGrid(canvasId, values, opts) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const dpr   = Math.min(2, window.devicePixelRatio || 1);
  const nCols = values.length;
  const nRows = Math.max(1, Math.round((opts.maxY - opts.minY) / opts.stepY));
  // One extra always-empty row above the highest data row, purely so the
  // axis-max tick has a real row edge to land on (the same way every other
  // tick points at the boundary of the row it labels) instead of floating
  // in the padding above a grid that stops one row short.
  const totalRows = nRows + 1;

  const MB = opts.hideXLabels ? 0 : 18;
  const { ML, CELL, GAP, colW, cssW } = colLayout(canvas.parentElement, nCols);
  const rowH = CELL + 1;
  // Single label row above the bars, shared by day-boundary marks and
  // irrigation percent marks. They're laid out together with collision
  // avoidance (see the merged label pass below) rather than each owning a
  // fixed slot, so a same-row layout doesn't require a second band.
  const TOP_PAD = 3;
  const ROW_H   = 11;
  const BOT_PAD = 3;
  const LABEL_ROW_Y = TOP_PAD;
  const MT = TOP_PAD + ROW_H + BOT_PAD;
  const cssH = totalRows * rowH + MB + MT;
  const MR = opts.overlay ? 32 : 0; // room for the right-hand temperature axis
  const canvasW = cssW + MR;

  canvas.width  = canvasW * dpr;
  canvas.height = cssH * dpr;
  canvas.style.width  = canvasW + 'px';
  canvas.style.height = cssH + 'px';

  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const { fg, bg, irrig, hist } = getColors();
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, canvasW, cssH);

  // Y labels + ticks
  ctx.font = '8px monospace';
  ctx.fillStyle = fg;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  const yEvery = Math.max(1, Math.ceil(nRows / 7));
  for (let r = 0; r <= nRows; r += yEvery) {
    const val = opts.minY + r * opts.stepY;
    // Points at the row's bottom edge (an extension of the square's
    // undermost side), not the square's vertical middle -- that edge is the
    // actual value boundary between "this row filled" and "this row not".
    const y   = cssH - MB - r * rowH;
    ctx.fillText(val.toFixed(opts.decimals ?? 0) + (opts.unit || ''), ML - 4, y);
    ctx.strokeStyle = fg;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(ML - 2, Math.round(y) + 0.5);
    ctx.lineTo(ML,     Math.round(y) + 0.5);
    ctx.stroke();
  }

  // Cells
  //
  // Same quantized-square grid as always (each row is its own CELLxCELL
  // block with a 1px gap, so bars still read as stacked unit cells, not a
  // smooth bar). Columns flagged `interpolated` carry a `block` id
  // identifying which single coarse Yr.no reading they were split from (set
  // by the backend, not guessed from the rendered value -- guessing
  // merged/failed to merge runs unpredictably). Columns sharing a block id
  // widen their row-cells to span the whole run. Columns flagged `historic`
  // (the last HISTORIC_DAYS of real METAR data prepended before the
  // forecast, see dashboard/services.py's _historic_rows) draw in a muted
  // gray instead of fg, to read as observed-past rather than forecast --
  // there's currently no cached historic precipitation total though, so
  // those columns are always v == null here and just render blank/padded via
  // the guard below, same as any other missing reading.
  for (let c = 0; c < nCols; c++) {
    const v = values[c];
    if (v == null || isNaN(v)) continue;
    const coarse = !!(opts.interpolated && opts.interpolated[c]);
    const isHist = !!(opts.historic && opts.historic[c]);
    const barColor = isHist ? hist : fg;
    const filled = Math.max(0, Math.min(nRows, Math.round((v - opts.minY) / opts.stepY)));
    const blockId = opts.block && opts.block[c];

    let end = c;
    let w = CELL;
    if (coarse && blockId != null) {
      while (
        end + 1 < nCols && opts.interpolated[end + 1] &&
        values[end + 1] != null && !isNaN(values[end + 1]) &&
        opts.block && opts.block[end + 1] === blockId
      ) {
        end++;
      }
      w = (end - c + 1) * colW - GAP;
    }

    const cx = ML + c * colW;

    for (let r = 0; r < totalRows; r++) {
      const cy = cssH - MB - (r + 1) * rowH + 1;
      if (r < filled) {
        ctx.fillStyle = barColor;
        ctx.fillRect(cx, cy, w, CELL);
      } else {
        ctx.fillStyle = bg;
        ctx.fillRect(cx, cy, w, CELL);
        ctx.strokeStyle = barColor;
        ctx.lineWidth = 0.5;
        ctx.strokeRect(cx + 0.5, cy + 0.5, w - 1, CELL - 1);
      }
    }
    c = end;
  }

  // Day marks: dotted vertical line at each day change. Thicker than the
  // line chart's version -- at 0.5px it got lost against the dense cell grid.
  const labelCandidates = []; // { x, text, color } -- laid out below as one row
  if (opts.dateMarks && opts.dateMarks.length) {
    const botY = cssH - MB;
    ctx.strokeStyle = fg;
    ctx.lineWidth   = 1.5;
    ctx.setLineDash([5, 2]);
    for (const mark of opts.dateMarks) {
      if (!mark.isBoundary) continue;
      const x = Math.round(ML + mark.col * colW) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, MT); ctx.lineTo(x, botY); ctx.stroke();
    }
    ctx.setLineDash([]);

    for (const mark of opts.dateMarks) {
      labelCandidates.push({ x: ML + mark.col * colW + 2, text: mark.label, color: fg });
    }
  }

  // Scheduled irrigation marks: dark green dotted vertical line at the
  // (fractional, interpolated) column position of each upcoming event -- a
  // bit heavier than the day-boundary marks so a specific scheduled event
  // outweighs a generic day change. Its percent label joins the same row
  // as the date labels below. (Dormant on the weather page -- see the note
  // above drawGrid: opts.eventMarks is never populated here.)
  if (opts.eventMarks && opts.eventMarks.length && opts.epochs && opts.epochs.length && opts.epochs.every(Number.isFinite)) {
    const botY = cssH - MB;
    ctx.strokeStyle = irrig;
    ctx.lineWidth   = 2.5;
    ctx.setLineDash([5, 2]);
    for (const mark of opts.eventMarks) {
      const col = colForEpoch(opts.epochs, mark.epoch);
      if (col == null) continue;
      const x = Math.round(ML + col * colW + CELL / 2) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, MT); ctx.lineTo(x, botY); ctx.stroke();
      labelCandidates.push({ x: x + 2, text: `${mark.percent}%`, color: irrig });
    }
    ctx.setLineDash([]);
  }

  // Merged label row: date marks and irrigation percent marks share one
  // horizontal line. Laid out left-to-right in x order, pushing each label
  // past the previous one's right edge (+ a small gap) whenever their
  // natural positions would collide -- e.g. a day boundary falling right on
  // top of a late-night irrigation event -- instead of letting them overlap.
  if (labelCandidates.length) {
    ctx.font = '8px monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    const GAP_PX = 3;
    labelCandidates.sort((a, b) => a.x - b.x);
    let cursor = -Infinity;
    for (const item of labelCandidates) {
      const w = ctx.measureText(item.text).width;
      let drawX = Math.max(item.x, cursor);
      drawX = Math.min(drawX, canvasW - 2 - w); // keep the last label on-canvas
      item.drawX = drawX;
      cursor = drawX + w + GAP_PX;
    }
    for (const item of labelCandidates) {
      ctx.fillStyle = item.color;
      ctx.fillText(item.text, item.drawX, LABEL_ROW_Y);
    }
  }

  if (!opts.hideXLabels) {
    ctx.font         = '8px monospace';
    ctx.textAlign    = 'center';
    ctx.textBaseline = 'alphabetic';

    // Pack labels as densely as their own text width allows, rather than a
    // fixed "~9 labels total" -- colW can be just a few px on a long chart,
    // but a "14h"-style label needs several px of its own, so the safe
    // spacing depends on measured font metrics, not column count.
    const labelGapPx = 4;
    const maxLabelW = Math.max(8, ...opts.labels.map(l => ctx.measureText(l || '').width));
    const colsPerTick = Math.max(1, Math.ceil((maxLabelW + labelGapPx) / colW));

    const tickTopY = cssH - MB;
    const tickBotY = tickTopY + 3;
    const drawTick = (c) => {
      const x = Math.round(ML + c * colW + CELL / 2) + 0.5;
      ctx.strokeStyle = fg;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, tickTopY);
      ctx.lineTo(x, tickBotY);
      ctx.stroke();
      ctx.fillStyle = fg;
      ctx.fillText(opts.labels[c] || '', x, cssH - 2);
    };

    for (let c = 0; c < nCols; c += colsPerTick) drawTick(c);
    if ((nCols - 1) % colsPerTick !== 0) drawTick(nCols - 1);
  }

  // Overlay: right-axis line series (temperature), drawn on top of the bars.
  if (opts.overlay) {
    const ov = opts.overlay;
    const cH   = cssH - MT - MB; // same vertical span as the bar area
    const toYOv = v => MT + cH - ((v - ov.minY) / (ov.maxY - ov.minY)) * cH;
    const axisX = Math.round(cssW) + 0.5;

    // Right axis border + ticks + labels
    ctx.strokeStyle = fg;
    ctx.lineWidth   = 1;
    ctx.beginPath(); ctx.moveTo(axisX, MT); ctx.lineTo(axisX, MT + cH); ctx.stroke();

    ctx.font = '8px monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    const OV_STEPS = 4;
    for (let i = 0; i <= OV_STEPS; i++) {
      const val = ov.minY + i * (ov.maxY - ov.minY) / OV_STEPS;
      const y   = Math.round(toYOv(val)) + 0.5;
      ctx.strokeStyle = fg;
      ctx.lineWidth   = 1;
      ctx.beginPath(); ctx.moveTo(axisX, y); ctx.lineTo(axisX + 3, y); ctx.stroke();
      ctx.fillStyle = fg;
      ctx.fillText(val.toFixed(0) + (ov.unit || ''), axisX + 5, y);
    }

    // Temperature line + × markers, matching the OWM series style used
    // previously in the standalone temperature chart.
    //
    // The line should render bg-colored (black) wherever it crosses an
    // actual filled/data bar, and plain fg everywhere else. Canvas blend
    // modes (difference/destination-in) react to any fg pixel underneath,
    // including empty-cell border outlines, and their anti-aliased edges
    // don't line up cleanly with those borders, producing visible notches.
    // Simpler and exact: pick each small piece's color directly by comparing
    // its position to that column's bar height -- the same numbers used to
    // draw the bars -- and stroke it plainly. No compositing, so it can't
    // interact with border pixels at all.
    const toXOv = c => ML + c * colW + CELL / 2;
    const barTopY = c => {
      const v = values[c];
      if (v == null || isNaN(v)) return cssH - MB;
      const filled = Math.max(0, Math.min(nRows, Math.round((v - opts.minY) / opts.stepY)));
      return filled <= 0 ? (cssH - MB) : (cssH - MB - filled * rowH + 1);
    };
    const colAt = x => Math.max(0, Math.min(nCols - 1, Math.round((x - ML - CELL / 2) / colW)));
    // Historic segment of the line renders flat gray, no bg/fg contrast
    // switching needed -- those columns' bars are always empty (see the
    // cell-loop comment above), so there's nothing for the line to blend
    // into there.
    const isHistCol = c => !!(opts.historic && opts.historic[c]);

    ctx.lineWidth = 1.5;
    ctx.setLineDash([]);
    const SUBSTEPS = 6; // per inter-column segment, for smooth color transitions
    let prevX = null, prevY = null;
    for (let c = 0; c < nCols; c++) {
      const v = ov.values[c];
      if (v == null || isNaN(v)) { prevX = null; continue; }
      const x = toXOv(c), y = toYOv(v);
      if (prevX != null) {
        for (let s = 0; s < SUBSTEPS; s++) {
          const t0 = s / SUBSTEPS, t1 = (s + 1) / SUBSTEPS;
          const x0 = prevX + (x - prevX) * t0, y0 = prevY + (y - prevY) * t0;
          const x1 = prevX + (x - prevX) * t1, y1 = prevY + (y - prevY) * t1;
          const midX = (x0 + x1) / 2, midY = (y0 + y1) / 2;
          const midCol = colAt(midX);
          ctx.strokeStyle = isHistCol(midCol) ? hist : (midY >= barTopY(midCol) ? bg : fg);
          ctx.beginPath();
          ctx.moveTo(x0, y0); ctx.lineTo(x1, y1);
          ctx.stroke();
        }
      }
      prevX = x; prevY = y;
    }

    const a = 3;
    for (let c = 0; c < nCols; c++) {
      const v = ov.values[c];
      if (v == null || isNaN(v)) continue;
      const x = toXOv(c), y = toYOv(v);
      ctx.strokeStyle = isHistCol(c) ? hist : (y >= barTopY(c) ? bg : fg);
      ctx.beginPath();
      ctx.moveTo(x - a, y - a); ctx.lineTo(x + a, y + a);
      ctx.moveTo(x + a, y - a); ctx.lineTo(x - a, y + a);
      ctx.stroke();
    }
  }
}

// ── Render helpers ────────────────────────────────────────────────────────────

function rv(v, d = 1) { return v == null ? '—' : Number(v).toFixed(d); }

function formatObsAge(ageMinutes) {
  if (ageMinutes == null) return '—';
  if (ageMinutes < 1) return I18N.just_now || '—';
  if (ageMinutes < 60) return `${Math.round(ageMinutes)}m`;
  const h = Math.floor(ageMinutes / 60);
  const m = Math.round(ageMinutes % 60);
  return `${h}h ${pad(m)}m`;
}

// Metric tiles show live airport ground truth (the latest METAR observation)
// rather than an average across the Yr.no/OWM/Open-Meteo forecast sources --
// those remain forecast-only inputs to the irrigation decision. tMin/tMax
// come from the irrigation schedule's forecast temperature stats (still a
// weather quantity, just computed alongside the irrigation decision).
function renderMetrics(current, schedule) {
  const cur  = current || {};
  const tMin = schedule.tmin24_c;
  const tMax = schedule.tmax24_c;

  document.getElementById('m-temp').textContent       = cur.temp_c   != null ? `${rv(cur.temp_c, 0)}°` : '—';
  document.getElementById('m-temp-range').textContent = `${rv(tMin)}° / ${rv(tMax)}°`;
  document.getElementById('m-rh').textContent         = cur.rh_pct   != null ? `${rv(cur.rh_pct, 0)}%` : '—';
  document.getElementById('m-rh-sub').textContent     = cur.age_minutes != null ? formatObsAge(cur.age_minutes) : (I18N.metric_rh_sub || '—');
  document.getElementById('m-wind').textContent       = cur.wind_mps != null ? rv(cur.wind_mps) : '—';
  document.getElementById('m-vpd').textContent        = cur.vpd_kpa != null ? rv(cur.vpd_kpa, 2) : '—';
}

function hLabel(row) {
  return parseInt(row.local_time.slice(-5), 10) + 'h';
}

// One mark per calendar day present in rows: { col, label, isBoundary }.
// col 0 always gets a mark (the first day, no vertical line needed since the
// axis border already marks the left edge); every day change after that adds
// a mark with isBoundary = true (dotted vertical line + date label).
// local_time is "MM-DD HH:MM"; first 5 chars are the date part.
function dateMarks(rows) {
  const fmt = s => s.slice(0, 5).replace('-', '/');
  const marks = [{ col: 0, label: fmt(rows[0].local_time), isBoundary: false }];
  for (let i = 1; i < rows.length; i++) {
    if (rows[i].local_time.slice(0, 5) !== rows[i - 1].local_time.slice(0, 5)) {
      marks.push({ col: i, label: fmt(rows[i].local_time), isBoundary: true });
    }
  }
  return marks;
}

// Shared step: one row of chart height is worth this many mm of rain AND
// this many degrees C -- deliberately the same number for both, so 1mm and
// 1°C occupy identical vertical space instead of each axis independently
// stretching to fill the same height regardless of its own real range (a
// mild multi-day temperature swing used to get visually exaggerated into a
// dramatic-looking spike whenever a heavy-rain day forced a tall chart).
const AXIS_STEP = 2;
const RAIN_MIN_MAX_MM = 20; // floor for the rain axis top, so a dry spell doesn't flatten the grid

// Shared across all three panels (rain AND temp) so a given row means the
// same mm/°C on every chart, and the chart height is whichever of the two
// series actually needs more rows at the shared scale -- not always the
// rain axis, the way it used to be when temp had its own independent
// full-height normalization.
function combinedAxis(rainSeriesList, tempSeriesList) {
  const rainMaxRaw = Math.max(...rainSeriesList.flat(), 0.01);
  const rainRows = Math.max(RAIN_MIN_MAX_MM / AXIS_STEP, Math.ceil(rainMaxRaw / AXIS_STEP));

  const temps = tempSeriesList.flat().filter(v => v != null);
  let tempRange = null;
  if (temps.length) {
    const min = Math.floor(Math.min(...temps) / AXIS_STEP) * AXIS_STEP;
    const max = Math.ceil(Math.max(...temps) / AXIS_STEP) * AXIS_STEP;
    tempRange = { min, max, rows: (max - min) / AXIS_STEP };
  }

  const rows = Math.max(rainRows, tempRange ? tempRange.rows : 0);
  const span = rows * AXIS_STEP;

  let temp = null;
  if (tempRange) {
    // Center the temperature's real range within the shared span, rather
    // than anchoring its bottom to rain's 0-baseline -- on a heavy-rain day
    // the chart grows tall to fit the rain, and without this the temp line
    // would just hug the bottom of all that extra height (min at row 0)
    // instead of sitting roughly in the middle of it.
    const padding = span - (tempRange.max - tempRange.min);
    temp = { minY: tempRange.min - padding / 2, maxY: tempRange.max + padding / 2, unit: '°' };
  }

  return {
    rain: { minY: 0, maxY: span, stepY: AXIS_STEP, decimals: 0, unit: 'mm' },
    temp,
  };
}

// No eventMarks passed here -- this is the standalone weather page,
// deliberately decoupled from irrigation scheduling.
function renderRainCharts(rows) {
  const labels       = rows.map(hLabel);
  const epochs       = rows.map(r => r.epoch);
  const historic     = rows.map(r => r.historic ?? false);
  // Historic rows' rain fields are always null (no cached historic
  // precipitation total, see dashboard/services.py's _historic_rows) --
  // left as null rather than defaulted to 0 like a forecast gap would be,
  // so the chart draws them as blank/padded, not a false "0mm observed".
  const owmRain      = rows.map(r => r.historic ? null : (r.owm_rain_3h_mm ?? 0));
  const yrRain       = rows.map(r => r.historic ? null : (r.yr_rain_3h_mm  ?? 0));
  const omRain       = rows.map(r => r.historic ? null : (r.om_rain_3h_mm  ?? 0));
  const yrInterp     = rows.map(r => r.yr_rain_interpolated ?? false);
  const yrBlock      = rows.map(r => r.yr_rain_block ?? null);
  const owmTemps     = rows.map(r => r.owm_temp_c);
  const yrTemps      = rows.map(r => r.yr_temp_c);
  const omTemps      = rows.map(r => r.om_temp_c);

  const dm = dateMarks(rows);
  const axis = combinedAxis([owmRain, yrRain, omRain], [owmTemps, yrTemps, omTemps]);

  drawGrid('chart-rain-owm', owmRain, {
    labels, ...axis.rain, hideXLabels: false, dateMarks: dm, epochs, historic,
    overlay: axis.temp && { values: owmTemps, ...axis.temp },
  });
  drawGrid('chart-rain-yr', yrRain, {
    labels, ...axis.rain, hideXLabels: false, interpolated: yrInterp, block: yrBlock, dateMarks: dm, epochs, historic,
    overlay: axis.temp && { values: yrTemps, ...axis.temp },
  });
  drawGrid('chart-rain-om', omRain, {
    labels, ...axis.rain, hideXLabels: false, dateMarks: dm, epochs, historic,
    overlay: axis.temp && { values: omTemps, ...axis.temp },
  });
}

// ── Main refresh ─────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    stationTz = (data.location || {}).tz || null;
    tickLocalClock();
    renderLocation(data.location);
    drawSunPath('chart-sun', (data.location || {}).lat, (data.location || {}).lon, data.sun_projection, data.sun_view);

    const irrig   = data.irrigation || {};
    const sched   = irrig.schedule  || {};
    const current = data.current    || null;
    const frows   = (data.comparison || {}).rows || [];

    renderMetrics(current, sched);
    if (frows.length) renderRainCharts(frows);

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
