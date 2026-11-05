'use strict';

// I18N is declared once in app.js (loaded first, same global scope) -- not
// redeclared here.

// ── Local clock (station's timezone, from /api/status's location.tz) ──────────

let stationTz = null;
const UI_LOCALE = document.documentElement.lang || 'en';
const SOLAR_TIME_UNITS = /^(zh|ja)(-|$)/.test(UI_LOCALE)
  ? { hour: '時', minute: '分', second: '秒' }
  : { hour: 'h', minute: 'm', second: 's' };

function solarDuration(seconds) {
  return `${Math.floor(seconds / 3600)}${SOLAR_TIME_UNITS.hour}${String(Math.floor(seconds % 3600 / 60)).padStart(2, '0')}${SOLAR_TIME_UNITS.minute}${String(seconds % 60).padStart(2, '0')}${SOLAR_TIME_UNITS.second}`;
}


function tickLocalClock() {
  tickSolarCountdowns();
  const timeEl = document.getElementById('local-time');
  const dateEl = document.getElementById('local-date');
  if (!timeEl) return;
  if (!stationTz) { timeEl.textContent = '—'; if (dateEl) dateEl.textContent = '—'; return; }
  const now = new Date();
  try {
    timeEl.textContent = new Intl.DateTimeFormat(UI_LOCALE, {
      timeZone: stationTz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).format(now);
    if (dateEl) {
      dateEl.textContent = new Intl.DateTimeFormat(UI_LOCALE, {
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

// Signed time relative to each event: negative until it occurs, positive after.
let solarCountdowns = [];
let solarClockData = null;
let solarClockView = 'down';
let solarClockRenderedData = null;
let solarClockRenderedView = null;

// This remains a time dial, with daylight stretched into a semicircle.
// East/west use the same normal-direction mapping as the polar plot.
function solarClockPoint(epoch, noonEpoch, riseEpoch, setEpoch, view = 'down') {
  if (!(riseEpoch < noonEpoch && noonEpoch < setEpoch)) return null;
  const cycle = 86400000;
  const time = riseEpoch + ((epoch - riseEpoch) % cycle + cycle) % cycle;
  let angle;
  if (time <= noonEpoch) {
    angle = -Math.PI / 2 + (time - riseEpoch) / (noonEpoch - riseEpoch) * Math.PI / 2;
  } else if (time <= setEpoch) {
    angle = (time - noonEpoch) / (setEpoch - noonEpoch) * Math.PI / 2;
  } else {
    angle = Math.PI / 2 + (time - setEpoch) / (riseEpoch + cycle - setEpoch) * Math.PI;
  }
  return { ...solarDialPoint(angle, view), angle };
}

function solarDialPoint(angle, view = solarClockView) {
  const [x, y] = azUnit(-angle * 180 / Math.PI, view);
  return { x: 100 + 94 * x, y: 100 + 94 * y };
}

function solarSector(start, end, view = solarClockView) {
  if (end - start < 0.000001) return '';
  const a = solarDialPoint(start, view), b = solarDialPoint(end, view);
  return `M100 100 L${a.x} ${a.y} A94 94 0 ${end - start > Math.PI ? 1 : 0} ${view === 'up' ? 1 : 0} ${b.x} ${b.y} Z`;
}

function solarPhaseAt(now) {
  if (!solarClockData) return null;
  let phase = solarClockData.initial_phase;
  for (const event of solarClockData.stages) {
    if (Date.parse(event.time) > now) break;
    phase = event.phase;
  }
  return phase;
}

function setSolarPhase(element, phase, side) {
  element.dataset.phase = phase;
  element.dataset.side = side;
}

function updateSolarHighlights(now) {
  const noon = solarClockData && solarClockData.stages.find(event => event.stage === 'solar_noon');
  const phase = solarPhaseAt(now);
  const side = noon && now < Date.parse(noon.time) ? 'rising' : 'setting';
  for (const band of document.querySelectorAll('.solar-clock-band')) {
    band.dataset.current = String(band.dataset.phase === phase && (band.dataset.side === 'both' || band.dataset.side === side));
  }
  const initial = document.querySelector('.solar-stage-initial');
  if (initial) {
    initial.dataset.current = String(Boolean(solarClockData) && (!solarCountdowns.length || now < solarCountdowns[0].epoch));
    initial.querySelector('.solar-offset').dataset.before = initial.dataset.current === 'true' ? 'upcoming' : 'elapsed';
    initial.querySelector('.solar-offset').dataset.after = initial.dataset.current === 'true' ? 'upcoming' : 'elapsed';
  }
  for (const { entry, epoch, nextEpoch } of solarCountdowns) {
    const current = epoch <= now && now < nextEpoch;
    entry.dataset.current = String(current);
    entry.setAttribute('aria-current', current ? 'step' : 'false');
  }
}

function solarClockGeometry(data, view = solarClockView) {
  const noon = data.stages.find(event => event.stage === 'solar_noon');
  const rise = data.stages.find(event => event.stage === 'sunrise');
  const set = data.stages.find(event => event.stage === 'sunset');
  const noonEpoch = noon ? Date.parse(noon.time) : NaN;
  const riseEpoch = rise ? Date.parse(rise.time) : NaN;
  const setEpoch = set ? Date.parse(set.time) : NaN;
  const anchored = riseEpoch < noonEpoch && noonEpoch < setEpoch;
  const edge = (stage, side, schematic) => {
    if (!anchored) return schematic * Math.PI / 180;
    const event = data.stages.find(event => event.stage === stage && (side === 'rising'
      ? Date.parse(event.time) < noonEpoch : Date.parse(event.time) > noonEpoch));
    // At high latitude a threshold may never be crossed: its sector collapses
    // at the middle of the night rather than inventing a transition.
    return event ? solarClockPoint(Date.parse(event.time), noonEpoch, riseEpoch, setEpoch, view).angle : Math.PI;
  };
  const evening = [Math.PI / 2, edge('nautical', 'setting', 96), edge('astronomical', 'setting', 102), edge('night', 'setting', 108)];
  const morning = [edge('astronomical', 'rising', 252), edge('nautical', 'rising', 258), edge('civil', 'rising', 264), 3 * Math.PI / 2];
  const bands = [{phase: 'day', side: 'rising', start: -Math.PI / 2, end: 0},
    {phase: 'day', side: 'setting', start: 0, end: Math.PI / 2},
    {phase: 'night', side: 'both', start: evening[3], end: morning[0]}];
  for (const [index, phase] of ['civil', 'nautical', 'astronomical'].entries()) {
    bands.push({phase, side: 'setting', start: evening[index], end: evening[index + 1]});
    bands.push({phase, side: 'rising', start: morning[2 - index], end: morning[3 - index]});
  }
  const thresholds = [];
  for (let index = 0; index < 3; index++) {
    if (evening[index + 1] < Math.PI - 0.000001) thresholds.push({text: `−${(index + 1) * 6}°`, angle: evening[index + 1]});
    if (morning[2 - index] > Math.PI + 0.000001) thresholds.push({text: `−${(index + 1) * 6}°`, angle: morning[2 - index]});
  }
  return {bands, thresholds, noonEpoch, riseEpoch, setEpoch};
}

function renderSolarBands() {
  const group = document.getElementById('solar-clock-bands');
  const labels = document.getElementById('solar-clock-degree-labels');
  const boundaries = document.getElementById('solar-clock-boundaries');
  if (!group || !labels || !boundaries) return;
  group.replaceChildren();
  labels.replaceChildren();
  solarClockRenderedData = solarClockData;
  solarClockRenderedView = solarClockView;
  if (!solarClockData) {
    boundaries.setAttribute('d', 'M6 100 H194');
    return;
  }
  const geometry = solarClockGeometry(solarClockData);
  const svg = (tag, attrs) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    return element;
  };
  for (const band of geometry.bands) {
    const d = solarSector(band.start, band.end);
    if (!d) continue;
    const element = svg('g', {class: 'solar-clock-band',
      'aria-label': I18N[band.phase === 'day' ? (band.side === 'rising' ? 'solar_day_morning' : 'solar_day_afternoon') : `solar_${band.phase}`], role: 'group'});
    element.append(svg('path', {d, fill: band.phase === 'day' ? 'transparent' : `url(#twilight-${band.phase})`}),
      svg('path', {d, class: 'solar-band-highlight', fill: 'currentColor', 'pointer-events': 'none', 'vector-effect': 'non-scaling-stroke'}));
    setSolarPhase(element, band.phase, band.side);
    group.append(element);
  }
  const paths = ['M6 100 H194'];
  const sides = {left: [], right: []};
  for (const boundary of geometry.thresholds) {
    const point = solarDialPoint(boundary.angle);
    paths.push(`M100 100 L${point.x} ${point.y}`);
    sides[point.x < 100 ? 'left' : 'right'].push({...boundary, point});
  }
  for (const [side, entries] of Object.entries(sides)) {
    let previousY = 104;
    for (const boundary of entries.sort((a, b) => a.point.y - b.point.y)) {
      const y = Math.max(boundary.point.y, previousY + 15);
      previousY = y;
      const x = side === 'left' ? -5 : 205;
      const text = svg('text', {x, y, class: 'solar-clock-label', 'text-anchor': side === 'left' ? 'end' : 'start', 'dominant-baseline': 'middle'});
      text.textContent = boundary.text;
      labels.append(svg('path', {d: `M${boundary.point.x} ${boundary.point.y} L${x + (side === 'left' ? 3 : -3)} ${y}`, class: 'solar-clock-leader'}), text);
    }
  }
  boundaries.setAttribute('d', paths.join(' '));
  const rise = solarDialPoint(-Math.PI / 2), set = solarDialPoint(Math.PI / 2);
  for (const [id, point] of [['solar-clock-rise-label', rise], ['solar-clock-set-label', set]]) {
    const label = document.getElementById(id);
    label.setAttribute('x', point.x);
    label.setAttribute('text-anchor', point.x < 100 ? 'start' : 'end');
  }
}

// Use the same physical-pixel symbol geometry as the canvas plot. Cancel
// the SVG viewBox scale for the symbol, while strokes remain non-scaling.
function syncSolarClockSymbol() {
  const symbol = document.getElementById('solar-clock-sun-symbol');
  const svg = symbol.ownerSVGElement;
  const scale = svg.getBoundingClientRect().width / svg.viewBox.baseVal.width;
  if (!(scale > 0)) return;
  svg.style.setProperty('--solar-line-width', `${SUN_WIDGET_LINE_WIDTH}px`);
  symbol.setAttribute('transform', `scale(${1 / scale})`);
  symbol.querySelector('.solar-clock-ring').setAttribute('r', SUN_GLYPH_RADIUS);
  symbol.querySelector('.solar-clock-dot').setAttribute('r', SUN_WIDGET_LINE_WIDTH * 0.75);
  const reach = SUN_GLYPH_RADIUS / Math.SQRT2;
  symbol.querySelector('.solar-clock-cross').setAttribute('d', `M${-reach} ${-reach} L${reach} ${reach} M${-reach} ${reach} L${reach} ${-reach}`);
}

function tickSolarClock(now = Date.now()) {
  const marker = document.getElementById('solar-clock-sun');
  const altitude = document.getElementById('solar-clock-altitude');
  const azimuth = document.getElementById('solar-clock-azimuth');
  if (!marker || !altitude || !azimuth) return;
  syncSolarClockSymbol();
  if (solarClockRenderedData !== solarClockData || solarClockRenderedView !== solarClockView) renderSolarBands();
  if (!solarClockData) {
    marker.setAttribute('visibility', 'hidden');
    altitude.textContent = azimuth.textContent = '—';
    return;
  }
  const {noonEpoch, riseEpoch, setEpoch} = solarClockGeometry(solarClockData);
  const point = solarClockPoint(now, noonEpoch, riseEpoch, setEpoch, solarClockView);
  if (point) {
    marker.setAttribute('transform', `translate(${point.x} ${point.y})`);
    marker.setAttribute('visibility', 'visible');
    marker.dataset.view = solarClockView;
  } else {
    marker.setAttribute('visibility', 'hidden');
  }
  altitude.textContent = `${solarClockData.current.elevation.toFixed(1)}°`;
  azimuth.textContent = `${solarClockData.current.az.toFixed(1)}°`;
  updateSolarHighlights(now);
}

function solarOffset(eventEpoch, now = Date.now()) {
  const elapsed = now - eventEpoch;
  const minutes = Math.floor(Math.abs(elapsed) / 60000);
  return `${elapsed < 0 ? '-' : '+'}${String(Math.floor(minutes / 60)).padStart(2, '0')}${SOLAR_TIME_UNITS.hour}${String(minutes % 60).padStart(2, '0')}${SOLAR_TIME_UNITS.minute}`;
}

function solarPhaseProgress(now, start, end) {
  return Math.max(0, Math.min(1, (now - start) / (end - start)));
}

function updateSolarTimeline(now) {
  const stages = document.getElementById('solar-stages');
  const marker = stages && stages.querySelector('.solar-timeline-marker');
  if (!marker || !solarClockData) return;
  const bounds = stages.getBoundingClientRect();
  const dayStart = Date.parse(solarClockData.day_start);
  const dayEnd = Date.parse(solarClockData.day_end);
  const events = solarCountdowns.map(({entry, epoch}) => ({epoch,
    y: entry.getBoundingClientRect().top - bounds.top + parseFloat(getComputedStyle(entry, '::before').top)}));
  const points = [{epoch: dayStart, y: 0}, ...events, {epoch: dayEnd, y: bounds.height}];
  const next = points.findIndex(point => point.epoch > now);
  const index = next < 0 ? points.length - 2 : Math.max(0, next - 1);
  const from = points[index], to = points[index + 1];
  const y = from.y + (to.y - from.y) * solarPhaseProgress(now, from.epoch, to.epoch);
  const offset = stages.querySelector('.solar-offset').getBoundingClientRect();
  stages.style.setProperty('--solar-rail-x', `${offset.right - bounds.left}px`);
  stages.style.setProperty('--solar-elapsed', `${y}px`);
  marker.hidden = !(dayStart <= now && now < dayEnd);
}

function tickSolarCountdowns() {
  const now = Date.now();
  tickSolarClock(now);
  for (const { element, epoch, nextEpoch, row } of solarCountdowns) {
    const text = solarOffset(epoch, now);
    if (element.textContent !== text) element.textContent = text;
    const current = epoch <= now && now < nextEpoch;
    row.dataset.state = current ? 'current' : epoch <= now ? 'elapsed' : 'upcoming';
    row.dataset.before = epoch <= now ? 'elapsed' : 'upcoming';
    row.dataset.after = current || nextEpoch <= now ? 'elapsed' : 'upcoming';
  }
  updateSolarTimeline(now);
}

// Daily Skyfield almanac, displayed above the diagram in station local time.
let solarRequest = 0;
async function refreshSolar(status) {
  const request = ++solarRequest;
  solarClockView = status.sun_view === 'up' ? 'up' : 'down';
  const current = status.current || {};
  const draw = solar => drawSunPath('chart-sun', solar, status.sun_projection,
    status.sun_view, current.wind_mps, current.wind_dir_deg);
  const duration = document.getElementById('solar-duration');
  const date = document.getElementById('solar-date');
  const stages = document.getElementById('solar-stages');
  const message = document.getElementById('solar-message');
  if (!duration) return;
  duration.textContent = '—';
  date.textContent = '';
  stages.replaceChildren();
  solarCountdowns = [];
  solarClockData = null;
  tickSolarClock();
  message.textContent = I18N.loading;
  draw(null);
  try {
    const res = await fetch('/api/solar');
    if (!res.ok) throw new Error('Solar calculation unavailable');
    const data = await res.json();
    if (request !== solarRequest) return;
    if (!data.available || data.station !== (status.location || {}).station) throw new Error('Station location unavailable');
    const seconds = data.day_length_seconds;
    duration.textContent = solarDuration(seconds);
    date.textContent = `${data.date} · ${data.timezone}`;
    const clock = new Intl.DateTimeFormat(UI_LOCALE, {
      timeZone: data.timezone, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
    const noonEvent = data.stages.find(event => event.stage === 'solar_noon');
    const noonEpoch = noonEvent ? Date.parse(noonEvent.time) : NaN;
    const initial = document.createElement('div');
    initial.className = 'solar-stage-initial';
    const initialLabel = document.createElement('span');
    initialLabel.className = 'solar-phase-name';
    initialLabel.textContent = I18N[data.initial_phase === 'day' ? 'solar_day_morning' : `solar_${data.initial_phase}`];
    const initialOffset = document.createElement('span');
    initialOffset.className = 'solar-offset';
    const initialRail = document.createElement('span');
    initialRail.className = 'solar-rail';
    initialRail.ariaHidden = 'true';
    initialOffset.append(document.createElement('span'), initialRail);
    initial.append(initialOffset, initialLabel);
    setSolarPhase(initial, data.initial_phase, 'rising');
    stages.append(initial);
    data.stages.forEach((stage, index) => {
      const entry = document.createElement('div');
      entry.className = 'solar-stage-row';
      const side = stage.stage === 'solar_noon' ? 'both' : Date.parse(stage.time) < noonEpoch ? 'rising' : 'setting';
      setSolarPhase(entry, stage.phase, side);
      const offset = document.createElement('span');
      offset.className = 'solar-offset';
      offset.dataset.first = String(index === 0);
      offset.dataset.last = String(index === data.stages.length - 1);
      const value = document.createElement('span');
      const rail = document.createElement('span');
      rail.className = 'solar-rail';
      rail.ariaHidden = 'true';
      offset.append(value, rail);
      solarCountdowns.push({ element: value, row: offset, entry,
        epoch: Date.parse(stage.time),
        nextEpoch: index + 1 < data.stages.length ? Date.parse(data.stages[index + 1].time) : Infinity });
      const time = document.createElement('time');
      time.dateTime = stage.time;
      time.textContent = clock.format(new Date(stage.time));
      const label = document.createElement('span');
      label.className = 'solar-event-label';
      const phaseName = I18N[`solar_${stage.phase}`];
      label.textContent = stage.stage === 'solar_noon' ? I18N.solar_solar_noon
        : I18N.solar_enter_phase.replace('{phase}', phaseName.toLocaleLowerCase(UI_LOCALE));
      if (stage.stage === 'sunrise' || stage.stage === 'sunset') {
        const anchor = I18N[stage.stage === 'sunrise' ? 'anchor_sunrise' : 'anchor_sunset'];
        label.textContent += ` · ${anchor.toLocaleLowerCase(UI_LOCALE)}`;
      }
      const phase = document.createElement('span');
      phase.className = 'solar-phase-name';
      phase.textContent = stage.phase === 'day'
        ? I18N[Date.parse(stage.time) < noonEpoch ? 'solar_day_morning' : 'solar_day_afternoon'] : phaseName;
      entry.setAttribute('aria-label', `${label.textContent} ${time.textContent}`);
      entry.append(offset, time, label, phase);
      stages.append(entry);
    });
    const timelineRail = document.createElement('span');
    timelineRail.className = 'solar-timeline-rail';
    timelineRail.ariaHidden = 'true';
    const timelineMarker = document.createElement('span');
    timelineMarker.className = 'solar-timeline-marker';
    timelineMarker.textContent = '☉';
    timelineMarker.ariaHidden = 'true';
    stages.append(timelineRail, timelineMarker);
    solarClockData = data;
    tickSolarCountdowns();
    message.textContent = '';
    draw(data);
  } catch (err) {
    if (request !== solarRequest) return;
    duration.textContent = '—';
    date.textContent = '';
    stages.replaceChildren();
    solarCountdowns = [];
    solarClockData = null;
    tickSolarClock();
    message.textContent = I18N.solar_unavailable;
    draw(null);
  }
}

// ── Sun path widget ──────────────────────────────────────────────────────────
//
// Daily path, sunrise/set events and current position all come from the
// local Skyfield calculation, shared with the event list above the diagram.
// Elevations near sunrise/set are clamped to the horizon for plotting:
// these events include the solar radius and standard atmospheric refraction.
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
const SUN_WIDGET_LINE_WIDTH = 1;

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
// The shaft remains in the plot; its numeric readout is in the lower-right
// corner outside the circle. Direction still follows the normal setting.
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

  // Feather/pennant ticks are fixed-size (off ringR, not the shaft), and
  // deliberately smaller than the old shaftLen-scaled ones so a dense run of
  // them (strong, gusty wind) doesn't read as a solid blob. What grows
  // instead is the shaft itself: it starts short (baseShaftLen, enough for a
  // single tick) and lengthens by one `step` per extra tick beyond the
  // first, so ticks stay evenly spaced at their fixed size no matter how
  // many stack up, rather than overlapping as they crowd a fixed-length
  // shaft. Capped at maxShaftLen -- same 0.8*ringR the shaft always used --
  // so it still sits strictly inside the smallest ring regardless of size or
  // projection.
  ctx.fillStyle = fg; // pennants (below) are filled, not just stroked
  const maxShaftLen = ringR * 0.8;
  const baseShaftLen = ringR * 0.45;
  const step = ringR * 0.09;
  const featherLen = ringR * 0.19;
  const halfLen = ringR * 0.10;
  const pennantLen = ringR * 0.15;

  let units = Math.round((windMps / WIND_BARB_MPS_PER_FEATHER) * 2) / 2; // nearest half-feather
  const pennants = Math.floor(units / 5);
  const afterPennants = units - pennants * 5;
  const feathers = Math.floor(afterPennants);
  const half = (afterPennants - feathers) >= 0.5 ? 1 : 0;
  const tickCount = pennants + feathers + half;
  const shaftLen = Math.min(maxShaftLen, baseShaftLen + Math.max(0, tickCount - 1) * step);

  const bx = cx + ux * shaftLen, by = cy + uy * shaftLen; // barb tip ("from" direction)

  // Shaft: a plain line from the zenith out to the barb tip, no arrowhead.
  ctx.strokeStyle = fg;
  ctx.lineWidth = SUN_WIDGET_LINE_WIDTH;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(bx, by);
  ctx.stroke();

  // Feathers, walking from the barb tip inward toward the zenith.
  const at = (dist) => ({ x: bx - ux * dist, y: by - uy * dist }); // dist measured from the barb tip
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

}

function renderSunPathReadouts(solar, windMps, windDirDeg) {
  const write = (id, text) => { document.getElementById(id).textContent = text; };
  const clock = solar && new Intl.DateTimeFormat('en-GB', {
    timeZone: solar.timezone, hour: '2-digit', minute: '2-digit', hour12: false,
  });
  for (const [stage, prefix] of [['sunrise', 'sun-rise'], ['sunset', 'sun-set']]) {
    const event = solar && solar.stages.find(event => event.stage === stage);
    write(`${prefix}-time`, event ? clock.format(new Date(event.time)) : '—');
    write(`${prefix}-az`, event ? `${Math.round(event.az)}°` : '—');
  }
  write('sun-position-alt', `${I18N.solar_altitude} ${solar ? solar.current.elevation.toFixed(1) + '°' : '—'}`);
  write('sun-position-az', `${I18N.solar_azimuth} ${solar ? solar.current.az.toFixed(1) + '°' : '—'}`);
  write('sun-wind-speed', windMps != null && Number.isFinite(Number(windMps)) ? `${Number(windMps).toFixed(1)} m/s` : '—');
  write('sun-wind-dir', formatWindDir(windDirDeg));
}

let lastSunPathRender = null;
function drawSunPath(canvasId, solar, projection, view, windMps, windDirDeg) {
  lastSunPathRender = [canvasId, solar, projection, view, windMps, windDirDeg];
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  renderSunPathReadouts(solar, windMps, windDirDeg);
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const pw = canvas.parentElement ? (canvas.parentElement.clientWidth || 200) : 200;
  const twilightWidth = document.querySelector('.solar-twilight-backbone').getBoundingClientRect().width;
  // The twilight circle occupies 188 of its 296 viewBox units. Match that
  // circumference rather than matching the SVG's label-inclusive box.
  const diameter = twilightWidth * 188 / 296;
  const PAD = Math.min(18, Math.max(0, (pw - diameter) / 2));
  const size = diameter + 2 * PAD;
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

  if (!solar) return;

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
    labels.push({ text: elev + '°', x: lx, y: ly, font: chartFont(9) });
  }
  ctx.setLineDash([]);

  // Compass labels -- via azUnit so 'up' swaps E/W screen positions along
  // with everything else, instead of just the curve flipping underneath
  // fixed N/S/E/W text.
  for (const [text, az] of [['N', 0], ['S', 180], ['E', 90], ['W', 270]]) {
    const [ux, uy] = azUnit(az, view);
    labels.push({ text, x: cx + ux * (R + 7), y: cy + uy * (R + 7), font: chartFont(10) });
  }

  // Use Skyfield's samples, breaking the curve during nighttime.
  ctx.strokeStyle = fg;
  ctx.lineWidth = SUN_WIDGET_LINE_WIDTH;
  ctx.beginPath();
  let drawing = false;
  for (const point of solar.path) {
    if (!point.visible) { drawing = false; continue; }
    const [x, y] = toXY(Math.max(0, point.elevation), point.az);
    if (drawing) ctx.lineTo(x, y); else { ctx.moveTo(x, y); drawing = true; }
  }
  ctx.stroke();

  // Event ticks stay on the horizon; times and azimuths are in the corners.
  for (const event of solar.stages.filter(event => event.stage === 'sunrise' || event.stage === 'sunset')) {
    const [ux, uy] = azUnit(event.az, view);
    ctx.strokeStyle = fg;
    ctx.lineWidth = SUN_WIDGET_LINE_WIDTH;
    ctx.beginPath();
    ctx.moveTo(cx + ux * R, cy + uy * R);
    ctx.lineTo(cx + ux * (R + 5), cy + uy * (R + 5));
    ctx.stroke();
  }

  const nowPos = solar.current;
  if (nowPos.visible) {
    const [x, y] = toXY(Math.max(0, nowPos.elevation), nowPos.az);
    drawRayGlyph(ctx, x, y, view, fg, SUN_GLYPH_RADIUS, bg);
  }

  // Compass and elevation-ring references stay attached to the plot.
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
  // Use the same enlarged size in both projections, with the shaft and
  // feathers scaled together while retaining the shared 1px stroke.
  const windGlyphRadius = R * elevToRadiusFraction(60, 'linear') * 1.5;
  drawWindBarb(ctx, cx, cy, windGlyphRadius, view, fg, bg, windMps, windDirDeg);
}

const solarDiagramResize = new ResizeObserver(() => {
  syncSolarClockSymbol();
  updateSolarTimeline(Date.now());
  if (lastSunPathRender) drawSunPath(...lastSunPathRender);
});
solarDiagramResize.observe(document.querySelector('.solar-twilight-backbone'));

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

// Current observations come from METAR. Forecast ranges refresh with the
// selected station independently of committed irrigation decisions.
function renderMetrics(current, forecastStats) {
  const cur  = current || {};
  const tMin = forecastStats.tmin24_c;
  const tMax = forecastStats.tmax24_c;
  const rhMin = forecastStats.rhmin24_pct;
  const rhMax = forecastStats.rhmax24_pct;

  document.getElementById('m-temp').textContent       = cur.temp_c   != null ? `${rv(cur.temp_c, 0)}°` : '—';
  document.getElementById('m-temp-range').textContent = `${rv(tMin)}° / ${rv(tMax)}°`;
  document.getElementById('m-rh').textContent         = cur.rh_pct   != null ? `${rv(cur.rh_pct, 0)}%` : '—';
  document.getElementById('m-rh-sub').textContent     = `${rv(rhMin, 0)}% / ${rv(rhMax, 0)}%`;
  document.getElementById('m-wind').textContent       = cur.wind_mps != null ? `${rv(cur.wind_mps)} m/s` : '—';
  document.getElementById('m-wind-dir').textContent   = formatWindDir(cur.wind_dir_deg);
  document.getElementById('m-pressure').textContent   = cur.pressure_hpa != null ? rv(cur.pressure_hpa, 0) : '—';
}

// hLabel/dateMarks/AXIS_STEP/RAIN_MIN_MAX_MM/combinedAxis/renderRainCharts/
// renderMeanChart/applyTrinityMode now live in charts.js (loaded before this
// file -- see weather.html), shared with irrigation.js.

// ── Main refresh ─────────────────────────────────────────────────────────────

let solarStatus = null;

async function refresh() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    stationTz = (data.location || {}).tz || null;
    tickLocalClock();
    renderLocation(data.location);

    const forecastStats = data.forecast_stats || {};
    const current = data.current    || null;
    const frows   = (data.comparison || {}).rows || [];

    await ensureChartFontReady();
    const solarSettingsChanged = !solarStatus || !solarClockData
      || (solarStatus.location || {}).station !== (data.location || {}).station
      || (solarStatus.location || {}).lat !== (data.location || {}).lat
      || (solarStatus.location || {}).lon !== (data.location || {}).lon
      || (solarStatus.location || {}).tz !== (data.location || {}).tz
      || solarStatus.sun_view !== data.sun_view
      || solarStatus.sun_projection !== data.sun_projection;
    solarStatus = data;
    if (solarSettingsChanged) await refreshSolar(data);

    renderMetrics(current, forecastStats);
    await applyTrinityMode(frows, data.trinity_mode !== 'off');

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
// Recalculate solar position and redraw both graphs once per minute,
// independently of weather-provider fetches. Timetable motion stays per-second.
setInterval(() => {
  if (solarStatus) void refreshSolar(solarStatus);
}, 60_000);
