'use strict';
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

function getColors() {
  const cs = getComputedStyle(document.documentElement);
  return {
    fg:    cs.getPropertyValue('--b').trim() || '#fff',
    bg:    cs.getPropertyValue('--w').trim() || '#000',
    hist:  cs.getPropertyValue('--hist').trim() || '#888',
  };
}
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

function colLayout(parentElem, nCols) {
  const pw   = parentElem ? (parentElem.clientWidth || 400) : 400;
  const ML   = 36;
  const CELL = Math.max(4, Math.min(8, Math.floor((pw - ML) / nCols)));
  const GAP  = Math.max(1, Math.floor(CELL * 0.15));
  const colW = CELL + GAP;
  return { ML, CELL, GAP, colW, cssW: ML + nCols * colW };
}
let chartHoverReadout = null;

function getChartHoverReadout() {
  if (!chartHoverReadout) {
    chartHoverReadout = document.createElement('div');
    chartHoverReadout.className = 'chart-hover-readout';
    chartHoverReadout.hidden = true;
    chartHoverReadout.setAttribute('aria-hidden', 'true');
    document.body.appendChild(chartHoverReadout);
  }
  return chartHoverReadout;
}

function hoverNumber(value) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  const rounded = Math.round(Number(value) * 10) / 10;
  if (rounded === 0 && Number(value) !== 0) return String(Number(Number(value).toPrecision(2)));
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}
function hoverRange(mean, range, unit) {
  const value = `${hoverNumber(mean)}${unit}`;
  return range ? `${value} · ${hoverNumber(range.min)}–${hoverNumber(range.max)}${unit}` : value;
}
function hoverPeriod(localTime) {
  const match = /^(\d{2})-(\d{2})\s+(\d{1,2}):/.exec(localTime || '');
  if (!match) return localTime || '—';
  const startHour = Number(match[3]);
  const endHour = (startHour + 2) % 24;
  const pad = n => String(n).padStart(2, '0');
  return `${match[1]}/${match[2]} ${pad(startHour)}-${pad(endHour)}h`;
}

function hideChartHoverReadout() {
  if (chartHoverReadout) chartHoverReadout.hidden = true;
}

function positionChartHoverReadout(readout, event) {
  const offset = 12;
  const edge = 8;
  readout.hidden = false;
  const left = Math.min(event.clientX + offset, window.innerWidth - readout.offsetWidth - edge);
  const top = Math.min(event.clientY + offset, window.innerHeight - readout.offsetHeight - edge);
  readout.style.left = `${Math.max(edge, left)}px`;
  readout.style.top = `${Math.max(edge, top)}px`;
}

function bindGridHover(canvas, data) {
  canvas._gridHoverData = data;
  if (canvas.dataset.gridHoverBound === 'true') return;
  canvas.dataset.gridHoverBound = 'true';

  canvas.addEventListener('mousemove', event => {
    const current = canvas._gridHoverData;
    const rect = canvas.getBoundingClientRect();
    if (!current || !rect.width) return hideChartHoverReadout();

    const x = (event.clientX - rect.left) * (current.canvasW / rect.width);
    const gridRight = current.ML + current.nCols * current.colW;
    if (x < current.ML || x >= gridRight) return hideChartHoverReadout();

    const col = Math.floor((x - current.ML) / current.colW);
    const readout = getChartHoverReadout();
    readout.replaceChildren(
      Object.assign(document.createElement('div'), { textContent: hoverPeriod(current.localTimes[col]) }),
      Object.assign(document.createElement('div'), { textContent: hoverRange(current.temperatures[col], current.tempRanges?.[col], '°') }),
      Object.assign(document.createElement('div'), { textContent: hoverRange(current.rain[col], current.rainRanges?.[col], 'mm') }),
    );
    positionChartHoverReadout(readout, event);
  });
  canvas.addEventListener('mouseleave', hideChartHoverReadout);
}

// Join adjacent provider extrema into a continuous filled envelope. Missing
// ranges and historic observations split the band rather than bridging gaps.
function drawRangeBand(ctx, ranges, toX, toY, halfWidth, historic, color) {
  const drawRun = run => {
    if (!run.length) return;
    const first = run[0], last = run[run.length - 1];
    ctx.beginPath();
    ctx.moveTo(toX(first) - halfWidth, toY(ranges[first].max));
    for (const c of run) ctx.lineTo(toX(c), toY(ranges[c].max));
    ctx.lineTo(toX(last) + halfWidth, toY(ranges[last].max));
    ctx.lineTo(toX(last) + halfWidth, toY(ranges[last].min));
    for (const c of [...run].reverse()) ctx.lineTo(toX(c), toY(ranges[c].min));
    ctx.lineTo(toX(first) - halfWidth, toY(ranges[first].min));
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  };
  ctx.save();
  ctx.fillStyle = color;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  let run = [];
  ranges.forEach((range, c) => {
    if (!range || !Number.isFinite(range.min) || !Number.isFinite(range.max) || historic?.[c]) {
      drawRun(run);
      run = [];
    } else run.push(c);
  });
  drawRun(run);
  ctx.restore();
}

function drawGrid(canvasId, values, opts) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const dpr   = Math.min(2, window.devicePixelRatio || 1);
  const nCols = values.length;
  const nRows = Math.max(1, Math.round((opts.maxY - opts.minY) / opts.stepY));
  const totalRows = nRows + 1;

  const MB = opts.hideXLabels ? 0 : 18;
  const { ML, CELL, GAP, colW, cssW } = opts.layout || colLayout(canvas.parentElement, nCols);
  const rowH = CELL + 1;
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
  const { fg, bg, hist } = getColors();
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, canvasW, cssH);
  ctx.font = chartFont(8);
  ctx.fillStyle = fg;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  const MIN_Y_LABEL_GAP_PX = 10;
  const yEvery = Math.max(1, Math.ceil(MIN_Y_LABEL_GAP_PX / rowH));
  for (let r = 0; r <= nRows; r += yEvery) {
    const val = opts.minY + r * opts.stepY;
    const y   = cssH - MB - r * rowH;
    ctx.fillText(val.toFixed(opts.decimals ?? 0) + (opts.unit || ''), ML - 4, y);
    ctx.strokeStyle = fg;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(ML - 2, Math.round(y) + 0.5);
    ctx.lineTo(ML,     Math.round(y) + 0.5);
    ctx.stroke();
  }
  for (let c = 0; c < nCols; c++) {
    const v = values[c];
    if (v == null || isNaN(v)) continue;
    const coarse = !!(opts.interpolated && opts.interpolated[c]);
    const isHist = !!(opts.historic && opts.historic[c]);
    const barColor = isHist ? hist : fg;
    const filled = Math.max(0, Math.min(nRows, (v - opts.minY) / opts.stepY));
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
      const fraction = Math.max(0, Math.min(1, filled - r));
      if (fraction < 1) {
        ctx.fillStyle = bg;
        ctx.fillRect(cx, cy, w, CELL);
        ctx.strokeStyle = barColor;
        ctx.lineWidth = 0.5;
        ctx.strokeRect(cx + 0.5, cy + 0.5, w - 1, CELL - 1);
      }
      if (fraction > 0) {
        // Preserve fractional rain; even a trace gets one physical pixel.
        const fillHeight = Math.max(1 / dpr, fraction * CELL);
        ctx.fillStyle = barColor;
        ctx.fillRect(cx, cy + CELL - fillHeight, w, fillHeight);
      }
    }
    c = end;
  }
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
  if (opts.overlay) {
    const ov = opts.overlay;
    const cH   = cssH - MT - MB; // same vertical span as the bar area
    const toYOv = v => MT + cH - ((v - ov.minY) / (ov.maxY - ov.minY)) * cH;
    const axisX = Math.round(cssW) + 0.5;
    ctx.strokeStyle = fg;
    ctx.lineWidth   = 1;
    ctx.beginPath(); ctx.moveTo(axisX, MT); ctx.lineTo(axisX, MT + cH); ctx.stroke();

    ctx.font = chartFont(8);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (let r = 0; r <= nRows; r += yEvery) {
      const val = ov.minY + (r / nRows) * (ov.maxY - ov.minY);
      const y   = Math.round(toYOv(val)) + 0.5;
      ctx.strokeStyle = fg;
      ctx.lineWidth   = 1;
      ctx.beginPath(); ctx.moveTo(axisX, y); ctx.lineTo(axisX + 3, y); ctx.stroke();
      ctx.fillStyle = fg;
      ctx.fillText(val.toFixed(0) + (ov.unit || ''), axisX + 5, y);
    }
    const toXOv = c => ML + c * colW + CELL / 2;
    const barTopY = c => {
      const v = values[c];
      if (v == null || isNaN(v)) return cssH - MB;
      const filled = Math.max(0, Math.min(nRows, (v - opts.minY) / opts.stepY));
      if (filled <= 0) return cssH - MB;
      const fullCells = Math.floor(filled);
      const fraction = filled - fullCells;
      return fraction > 0
        ? cssH - MB - fullCells * rowH - Math.max(1 / dpr, fraction * CELL)
        : cssH - MB - fullCells * rowH + 1;
    };
    const colAt = x => Math.max(0, Math.min(nCols - 1, Math.round((x - ML - CELL / 2) / colW)));
    const isHistCol = c => !!(opts.historic && opts.historic[c]);

    if (ov.ranges) {
      drawRangeBand(ctx, ov.ranges, toXOv, toYOv, colW / 2, opts.historic, fg);
    }
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
          // Combined mode shows the forecast envelope without a mean line.
          // Keep the gray observation history and each provider's own line.
          if (ov.ranges && !isHistCol(midCol)) continue;
          ctx.strokeStyle = isHistCol(midCol) ? hist : (midY >= barTopY(midCol) ? bg : fg);
          ctx.beginPath();
          ctx.moveTo(x0, y0); ctx.lineTo(x1, y1);
          ctx.stroke();
        }
      }
      prevX = x; prevY = y;
    }

    if (ov.showMarkers !== false || opts.historic?.some(Boolean)) {
      const a = 3;
      for (let c = 0; c < nCols; c++) {
        if (ov.showMarkers === false && !isHistCol(c)) continue;
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

  bindGridHover(canvas, {
    ML, colW, nCols, canvasW,
    localTimes: opts.localTimes || [],
    temperatures: opts.overlay ? opts.overlay.values : [],
    rain: values,
    tempRanges: opts.overlay?.ranges,
    rainRanges: opts.ranges,
  });
}

function hLabel(row) {
  return parseInt(row.local_time.slice(-5), 10) + 'h';
}
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
const AXIS_STEP = 2;
const RAIN_MIN_MAX_MM = 20; // floor for the rain axis top, so a dry spell doesn't flatten the grid
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
    const padding = span - (tempRange.max - tempRange.min);
    temp = { minY: tempRange.min - padding / 2, maxY: tempRange.max + padding / 2, unit: '°' };
  }

  return {
    rain: { minY: 0, maxY: span, stepY: AXIS_STEP, decimals: 0, unit: 'mm' },
    temp,
  };
}
function fixedChartLayout(nCols) {
  const ML = 36, CELL = 8, GAP = Math.max(1, Math.floor(CELL * 0.15));
  return { ML, CELL, GAP, colW: CELL + GAP, cssW: ML + nCols * (CELL + GAP) };
}
function renderRainCharts(rows) {
  const labels       = rows.map(hLabel);
  const localTimes   = rows.map(r => r.local_time);
  const epochs       = rows.map(r => r.epoch);
  const historic     = rows.map(r => r.historic ?? false);
  const owmRain      = rows.map(r => r.historic ? null : (r.owm_rain_3h_mm ?? 0));
  const yrRain       = rows.map(r => r.historic ? null : (r.yr_rain_3h_mm  ?? 0));
  const omRain       = rows.map(r => r.historic ? null : (r.om_rain_3h_mm  ?? 0));
  const yrInterp     = rows.map(r => r.yr_rain_interpolated ?? false);
  const yrBlock      = rows.map(r => r.yr_rain_block ?? null);
  const owmTemps     = rows.map(r => r.owm_temp_c);
  const yrTemps      = rows.map(r => r.yr_temp_c);
  const omTemps      = rows.map(r => r.om_temp_c);

  const dm = dateMarks(rows);
  const axisOwm = combinedAxis([owmRain], [owmTemps]);
  const axisYr  = combinedAxis([yrRain],  [yrTemps]);
  const axisOm  = combinedAxis([omRain],  [omTemps]);
  const chartLayout = fixedChartLayout(rows.length);

  drawGrid('chart-rain-owm', owmRain, {
    labels, localTimes, ...axisOwm.rain, hideXLabels: false, dateMarks: dm, epochs, historic, layout: chartLayout,
    overlay: axisOwm.temp && { values: owmTemps, ...axisOwm.temp },
  });
  drawGrid('chart-rain-yr', yrRain, {
    labels, localTimes, ...axisYr.rain, hideXLabels: false, interpolated: yrInterp, block: yrBlock, dateMarks: dm, epochs, historic, layout: chartLayout,
    overlay: axisYr.temp && { values: yrTemps, ...axisYr.temp },
  });
  drawGrid('chart-rain-om', omRain, {
    labels, localTimes, ...axisOm.rain, hideXLabels: false, dateMarks: dm, epochs, historic, layout: chartLayout,
    overlay: axisOm.temp && { values: omTemps, ...axisOm.temp },
  });
}
function meanOf(values) {
  const nums = values.filter(Number.isFinite);
  return nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : null;
}

function providerRange(values) {
  const nums = values.filter(Number.isFinite);
  return nums.length >= 2 ? { min: Math.min(...nums), max: Math.max(...nums) } : null;
}

function renderMeanChart(rows) {
  const labels   = rows.map(hLabel);
  const localTimes = rows.map(r => r.local_time);
  const epochs   = rows.map(r => r.epoch);
  const historic = rows.map(r => r.historic ?? false);
  const rainMean = rows.map(r => r.historic ? null : meanOf([r.owm_rain_3h_mm, r.yr_rain_3h_mm, r.om_rain_3h_mm]));
  const tempMean = rows.map(r => meanOf([r.owm_temp_c, r.yr_temp_c, r.om_temp_c]));
  const rainRanges = rows.map(r => r.historic ? null : providerRange([r.owm_rain_3h_mm, r.yr_rain_3h_mm, r.om_rain_3h_mm]));
  const tempRanges = rows.map(r => r.historic ? null : providerRange([r.owm_temp_c, r.yr_temp_c, r.om_temp_c]));

  const dm = dateMarks(rows);
  // Scale to the full provider high–low range.
  const axis = combinedAxis(
    [rainMean, rainRanges.filter(Boolean).flatMap(r => [r.min, r.max])],
    [tempMean, tempRanges.filter(Boolean).flatMap(r => [r.min, r.max])],
  );
  const layout = fixedChartLayout(rows.length);

  drawGrid('chart-rain-mean', rainMean, {
    labels, localTimes, ...axis.rain, hideXLabels: false, dateMarks: dm, epochs, historic, layout, ranges: rainRanges,
    overlay: axis.temp && { values: tempMean, ...axis.temp, ranges: tempRanges, showMarkers: false },
  });
}
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
