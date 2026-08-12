'use strict';

// Shared by weather.js (three per-source rain/temp charts) and irrigation.js
// (one mean-of-sources chart) -- split out here so irrigation.html doesn't
// have to load weather.js's own page-driving code (sun path widget, local
// clock, its own refresh()/setInterval) just to get drawGrid. Load this
// script before either of those on any page that uses it.

// Canvas does not inherit CSS fonts, so every chart label uses this exact
// self-hosted Google font instead of the platform-dependent `monospace`
// generic (Consolas on Windows, commonly DejaVu Sans Mono on Linux). CJK
// locales add their regional Noto Sans family as a glyph fallback while
// keeping Latin/numeric chart metrics in Space Mono.
const CHART_FONT_FAMILY = document.documentElement.lang === 'zh-Hant'
  ? '"Space Mono", "Noto Sans TC"'
  : document.documentElement.lang === 'ja'
    ? '"Space Mono", "Noto Sans JP"'
    : '"Space Mono"';
let chartFontReadyPromise = null;

function chartFont(sizePx) {
  return `${sizePx}px ${CHART_FONT_FAMILY}`;
}

function ensureChartFontReady() {
  if (!document.fonts || !document.fonts.load) return Promise.resolve();
  if (!chartFontReadyPromise) {
    const sample = document.documentElement.lang === 'zh-Hant'
      ? '0123456789氣溫雨量歷史日出日落'
      : document.documentElement.lang === 'ja'
        ? '0123456789気温雨量履歴日の出入り'
        : '0123456789Mm°%';
    chartFontReadyPromise = document.fonts.load(`10px ${CHART_FONT_FAMILY}`, sample);
  }
  return chartFontReadyPromise;
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
// opts.eventMarks (the irrigation-scheduling overlay) is dormant wherever a
// caller never populates it -- left in place rather than hand-trimmed out,
// to avoid risking a mistake in this carefully-tuned canvas math for a
// branch that's already harmless when unused.

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
  // opts.layout lets a caller share one measurement across multiple charts
  // (see renderRainCharts) instead of each canvas measuring its own
  // parentElement -- falls back to measuring here when no shared layout was
  // given, same as before.
  const { ML, CELL, GAP, colW, cssW } = opts.layout || colLayout(canvas.parentElement, nCols);
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
  ctx.font = chartFont(8);
  ctx.fillStyle = fg;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  // Label every few grid rows -- spacing driven by how many rows fit the
  // minimum pixel gap text needs, not stretched to hit some fixed total
  // label count. That way a taller chart (more rows) gets proportionally
  // more labels instead of the same handful spread thinner across it.
  const MIN_Y_LABEL_GAP_PX = 10;
  const yEvery = Math.max(1, Math.ceil(MIN_Y_LABEL_GAP_PX / rowH));
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
  // as the date labels below. (Dormant unless a caller populates
  // opts.eventMarks -- see the note above drawGrid.)
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
    ctx.font = chartFont(8);
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
    ctx.font         = chartFont(8);
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

    ctx.font = chartFont(8);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    // Same grid-row cadence as the left mm axis (yEvery), not a fixed count
    // of labels -- ov.minY/maxY span exactly nRows grid rows by construction
    // (combinedAxis ties the temp range to the same row count as rain), so
    // stepping by row index keeps both axes' labels aligned to the same
    // horizontal gridlines.
    for (let r = 0; r <= nRows; r += yEvery) {
      const val = ov.minY + (r / nRows) * (ov.maxY - ov.minY);
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

// Computes one rain+temp axis (row scale, span, centered temp range) from
// whatever series lists it's given -- 1 row is always AXIS_STEP mm AND
// AXIS_STEP °C, that ratio is a fixed constant, not derived from the data.
// Called per-chart (see renderRainCharts below), each with just its
// own single series: the row scale being fixed is exactly what makes each
// chart free to size its own height independently -- OWM, Yr.no and
// Open-Meteo (or renderMeanChart's single mean-of-sources chart) can each max
// out at their own tallest prediction (and fall back to RAIN_MIN_MAX_MM's
// floor on a dry/flat one) without losing the "1mm/1°C is always this many
// pixels, on every chart" comparability that mattered here, since that
// comes from the shared step, not a shared total height. Still takes lists
// (not single series) rather than being hard-wired to one-chart-at-a-time,
// in case a caller ever does want a genuinely shared axis across multiple
// series again.
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

// ── Shared chart width, fixed at colLayout's own max cell size ─────────────
// Not measured against the container -- see renderRainCharts's docstring
// below for the feedback-loop it avoids. Shared by every caller here since
// they all want the exact same deterministic-from-first-paint behavior.
function fixedChartLayout(nCols) {
  const ML = 36, CELL = 8, GAP = Math.max(1, Math.floor(CELL * 0.15));
  return { ML, CELL, GAP, colW: CELL + GAP, cssW: ML + nCols * (CELL + GAP) };
}

// ── Three-per-source chart (OWM/Yr.no/Open-Meteo, one canvas each) ─────────
// Shared by weather.html and irrigation.html -- both carry the same three
// canvas ids (chart-rain-owm/-yr/-om), shown when trinity mode is on (see
// applyTrinityMode below and dashboard/services.py's TRINITY_MODES).
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
  // One axis per chart, not one shared across all three -- see
  // combinedAxis's docstring for why that's fine despite the "same mm/°C
  // per row everywhere" comparability still holding: each chart maxes out
  // (or floors out, on a dry/flat one) at its own tallest prediction
  // instead of all three being stretched to match whichever source has the
  // biggest number.
  const axisOwm = combinedAxis([owmRain], [owmTemps]);
  const axisYr  = combinedAxis([yrRain],  [yrTemps]);
  const axisOm  = combinedAxis([omRain],  [omTemps]);

  // Width, unlike height, IS still shared across all three -- but fixed at
  // colLayout's own max cell size (8px), not measured against the
  // container the way colLayout normally would. Measuring created a
  // feedback loop with .layout's max-content-sized right column
  // (style.css): a freshly loaded page starts with placeholder-sized
  // canvases (no chart has been drawn yet), so the very first measurement
  // read a tiny width and produced a small chart -- exactly what changing
  // the language surfaced, since saving a new language reloads the whole
  // page. Worse, each subsequent 60s refresh re-measured against the
  // PREVIOUS cycle's now-slightly-wider canvas, climbing toward the true
  // max size over several visibly growing steps instead of landing on it
  // immediately -- that's the "grows 3 times" toggling citrus mode seemed
  // to trigger, though citrus itself never touches these charts; it just
  // happened to be clicked while a couple of those 60s cycles landed.
  // Fixed removes the feedback loop entirely: nCols (rows.length) is the
  // only input, so the width is deterministic from the very first paint.
  // A container too narrow for it falls back to .chart-scroll's own
  // horizontal scroll (that's what it's there for) rather than ever being
  // measured and shrunk to fit.
  const chartLayout = fixedChartLayout(rows.length);

  drawGrid('chart-rain-owm', owmRain, {
    labels, ...axisOwm.rain, hideXLabels: false, dateMarks: dm, epochs, historic, layout: chartLayout,
    overlay: axisOwm.temp && { values: owmTemps, ...axisOwm.temp },
  });
  drawGrid('chart-rain-yr', yrRain, {
    labels, ...axisYr.rain, hideXLabels: false, interpolated: yrInterp, block: yrBlock, dateMarks: dm, epochs, historic, layout: chartLayout,
    overlay: axisYr.temp && { values: yrTemps, ...axisYr.temp },
  });
  drawGrid('chart-rain-om', omRain, {
    labels, ...axisOm.rain, hideXLabels: false, dateMarks: dm, epochs, historic, layout: chartLayout,
    overlay: axisOm.temp && { values: omTemps, ...axisOm.temp },
  });
}

// ── Mean-of-sources chart (one canvas, all three sources averaged) ─────────
// Shared by weather.html and irrigation.html -- both carry a chart-rain-mean
// canvas, shown when trinity mode is off (see applyTrinityMode below).
function meanOf(values) {
  const nums = values.filter(v => v != null);
  return nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : null;
}

function renderMeanChart(rows) {
  const labels   = rows.map(hLabel);
  const epochs   = rows.map(r => r.epoch);
  const historic = rows.map(r => r.historic ?? false);
  // Historic rows carry no rain total from any source (see renderRainCharts
  // above) -- averaging three nulls would already come out null via
  // meanOf, this just keeps that "blank/padded, not a false 0mm" intent
  // explicit rather than incidental.
  const rainMean = rows.map(r => r.historic ? null : meanOf([r.owm_rain_3h_mm, r.yr_rain_3h_mm, r.om_rain_3h_mm]));
  const tempMean = rows.map(r => meanOf([r.owm_temp_c, r.yr_temp_c, r.om_temp_c]));

  const dm = dateMarks(rows);
  const axis = combinedAxis([rainMean], [tempMean]);
  const layout = fixedChartLayout(rows.length);

  drawGrid('chart-rain-mean', rainMean, {
    labels, ...axis.rain, hideXLabels: false, dateMarks: dm, epochs, historic, layout,
    overlay: axis.temp && { values: tempMean, ...axis.temp },
  });
}

// ── Trinity mode: show whichever chart layout is active and render it ──────
// Both weather.html and irrigation.html carry both layouts' DOM at all
// times (see their own .trinity-sources/.trinity-mean wrappers) -- this
// just flips which one's visible and draws into it, so a mid-session
// toggle (no page reload, see app.js's cfg-trinity-toggle) takes effect
// immediately on the next status refresh. Silently no-ops on a page with
// neither wrapper rather than erroring, so it's safe to call unconditionally
// from every page's refresh() even before either wrapper exists in the DOM.
async function applyTrinityMode(rows, trinityOn) {
  const sources = document.querySelector('.trinity-sources');
  const mean    = document.querySelector('.trinity-mean');
  if (sources) sources.classList.toggle('hidden', !trinityOn);
  if (mean)    mean.classList.toggle('hidden', trinityOn);
  if (!rows || !rows.length) return;
  await ensureChartFontReady();
  if (trinityOn) renderRainCharts(rows);
  else renderMeanChart(rows);
}
