'use strict';

// I18N is declared once in app.js (loaded first, same global scope) -- not
// redeclared here.

// ── Local clock (station's timezone, from /api/status's location.tz) ──────────

let stationTz = null;

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

// ── Wind barb (drawn on the sun path widget, one end at the zenith) ───────
// Standard meteorological wind-barb convention, rescaled from knots to m/s:
// the shaft points along the compass direction the wind is blowing FROM,
// each full feather worth WIND_BARB_MPS_PER_FEATHER m/s (10kt ~= 5.14 m/s,
// close enough that 5 m/s/feather stays recognizable to anyone who already
// reads real wind barbs), a half feather for a remainder >= half that, and
// a filled pennant triangle once five full feathers' worth stacks up (the
// same 50kt-pennant-equals-five-10kt-barbs relationship, just rescaled).
// One-sided, not a diameter through the center: the shaft starts at the
// zenith point (no arrowhead there, just a plain line) and extends outward
// toward the "from" direction, where the feathers are. The speed/direction
// readout sits on the OPPOSITE side of the zenith from the barb -- its own
// point, not the shaft's far end, with no line connecting it. Direction
// goes through azUnit() like everything else in this widget, so 'up'/'down'
// view flips it consistently with the day-path curve, compass labels, etc.
const WIND_BARB_MPS_PER_FEATHER = 5;

function drawWindBarb(ctx, cx, cy, ringR, view, fg, bg, windMps, windDirDeg) {
  // windDirDeg can be "VRB" (METAR's variable-direction report, a real
  // value real stations return -- see formatWindDir) or missing entirely;
  // neither has a compass direction to point the shaft at, so there's
  // nothing to draw. Number(null) is 0 (a real, wrong direction), which is
  // why this checks == null explicitly before the numeric coercion rather
  // than just Number.isFinite(Number(windDirDeg)).
  if (windMps == null || isNaN(windMps) || windDirDeg == null) return;
  const dir = Number(windDirDeg);
  if (!Number.isFinite(dir)) return;

  const [ux, uy] = azUnit(dir, view);
  const px = -uy, py = ux; // perpendicular unit vector, for the feather ticks

  // Sized off the smallest concentric elevation ring (ringR, the 60deg
  // ring -- see drawSunPath) rather than the full horizon radius, and
  // scaled to sit strictly inside it, so the barb always reads as smaller
  // than every ring on the widget regardless of size or projection.
  const shaftLen = ringR * 0.8;
  const bx = cx + ux * shaftLen, by = cy + uy * shaftLen; // barb tip ("from" direction)
  const rx = cx - ux * shaftLen, ry = cy - uy * shaftLen; // readout, opposite side of the zenith

  // Shaft: a plain line from the zenith out to the barb tip, no arrowhead.
  // Nothing is drawn on the readout side -- it's a bare label, not a shaft
  // end.
  ctx.strokeStyle = fg;
  ctx.lineWidth = SUN_WIDGET_LINE_WIDTH;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(bx, by);
  ctx.stroke();

  // Feathers, walking from the barb tip inward toward the zenith. Same
  // proportions relative to shaftLen as before it was rescaled to ringR.
  ctx.fillStyle = fg; // pennants (below) are filled, not just stroked
  const step = shaftLen * 0.1364;
  const featherLen = shaftLen * 0.2909;
  const halfLen = shaftLen * 0.1455;
  const pennantLen = shaftLen * 0.2364;
  const at = (dist) => ({ x: bx - ux * dist, y: by - uy * dist }); // dist measured from the barb tip

  let units = Math.round((windMps / WIND_BARB_MPS_PER_FEATHER) * 2) / 2; // nearest half-feather
  let d = 0;
  while (units >= 5) {
    const base = at(d), edge = at(d + step * 0.9);
    ctx.beginPath();
    ctx.moveTo(base.x, base.y);
    ctx.lineTo(edge.x, edge.y);
    ctx.lineTo(base.x + px * pennantLen, base.y + py * pennantLen);
    ctx.closePath();
    ctx.fill();
    d += step;
    units -= 5;
  }
  while (units >= 1) {
    const p = at(d);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    ctx.lineTo(p.x + px * featherLen, p.y + py * featherLen);
    ctx.stroke();
    d += step;
    units -= 1;
  }
  if (units >= 0.5) {
    const p = at(d);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    ctx.lineTo(p.x + px * halfLen, p.y + py * halfLen);
    ctx.stroke();
  }

  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = '9px monospace';
  haloText(ctx, `${windMps.toFixed(1)} m/s`, rx, ry - 5, fg, bg);
  haloText(ctx, `${Math.round(dir)}°`, rx, ry + 5, fg, bg);
}

function drawSunPath(canvasId, latDeg, lonDeg, projection, view, windMps, windDirDeg) {
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
    // Two stacked labels, not one string with '\n' -- canvas fillText has no
    // concept of line breaks, it'd just draw straight through the newline
    // glyph-less. haloText already halos each line's own measured width
    // independently, which reads better here anyway (az. is usually the
    // wider line, alt. would otherwise sit inside an oversized shared halo).
    labels.push({ text: `alt. ${Math.round(nowPos.elevation)}°`, x: lx, y: ly - 5, font: '9px monospace' });
    labels.push({ text: `az. ${Math.round(nowPos.az)}°`, x: lx, y: ly + 5, font: '9px monospace' });
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

  // Wind barb, drawn last of all so its shaft reads clearly crossing right
  // through the zenith reticule above rather than being obscured by it.
  // Sized off the smallest concentric ring (elev=60, always the smaller of
  // the two drawn rings since elevToRadiusFraction is monotonically
  // decreasing in elevation under either projection) so the barb stays
  // visibly shorter than every ring on the widget.
  const smallestRingR = R * elevToRadiusFraction(60, projection);
  drawWindBarb(ctx, cx, cy, smallestRingR, view, fg, bg, windMps, windDirDeg);
}

// ── Render helpers ────────────────────────────────────────────────────────────
// getColors/colForEpoch/colLayout/drawGrid now live in charts.js (loaded
// before this file -- see weather.html), shared with irrigation.js's own
// mean-of-sources chart.

function rv(v, d = 1) { return v == null ? '—' : Number(v).toFixed(d); }

// METAR's wind direction is a string, not always numeric -- "VRB" (variable
// direction) is a real value real stations report, straight from
// aviationweather.gov's API (see dashboard/models.py's MetarReading.wind_dir_deg
// and weather_mqtt.py's latest.get("wdir")), not an edge case to coerce
// away.
function formatWindDir(dir) {
  if (dir == null || dir === '') return '—';
  if (String(dir).toUpperCase() === 'VRB') return 'VRB';
  const deg = Math.round(Number(dir));
  return Number.isFinite(deg) ? `${deg}°` : '—';
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
  const rhMin = schedule.rhmin24_pct;
  const rhMax = schedule.rhmax24_pct;

  document.getElementById('m-temp').textContent       = cur.temp_c   != null ? `${rv(cur.temp_c, 0)}°` : '—';
  document.getElementById('m-temp-range').textContent = `${rv(tMin)}° / ${rv(tMax)}°`;
  document.getElementById('m-rh').textContent         = cur.rh_pct   != null ? `${rv(cur.rh_pct, 0)}%` : '—';
  document.getElementById('m-rh-sub').textContent     = `${rv(rhMin, 0)}% / ${rv(rhMax, 0)}%`;
  document.getElementById('m-wind').textContent       = cur.wind_mps != null ? `${rv(cur.wind_mps)} m/s` : '—';
  document.getElementById('m-wind-dir').textContent   = formatWindDir(cur.wind_dir_deg);
  document.getElementById('m-vpd').textContent        = cur.vpd_kpa != null ? rv(cur.vpd_kpa, 2) : '—';
}

// hLabel/dateMarks/AXIS_STEP/RAIN_MIN_MAX_MM/combinedAxis/renderRainCharts/
// renderMeanChart/applyTrinityMode now live in charts.js (loaded before this
// file -- see weather.html), shared with irrigation.js.

// ── Main refresh ─────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    stationTz = (data.location || {}).tz || null;
    tickLocalClock();
    renderLocation(data.location);

    const irrig   = data.irrigation || {};
    const sched   = irrig.schedule  || {};
    const current = data.current    || null;
    const frows   = (data.comparison || {}).rows || [];

    drawSunPath('chart-sun', (data.location || {}).lat, (data.location || {}).lon, data.sun_projection, data.sun_view, (current || {}).wind_mps, (current || {}).wind_dir_deg);

    renderMetrics(current, sched);
    applyTrinityMode(frows, data.trinity_mode !== 'off');

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
