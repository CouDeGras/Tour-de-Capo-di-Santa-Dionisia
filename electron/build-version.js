'use strict';

// Build versions use the local calendar date in a valid three-part numeric
// form: YY.M.D. Example: 12 August 2026 -> 26.8.12. Release tags may present
// the same date as v26.08.12, but Electron's semantic version cannot contain
// leading zeroes in numeric fields. Keeping this in one cross-platform script
// guarantees that Windows and Linux use the same calculation.
function buildVersion(now = new Date()) {
  const year = String(now.getFullYear()).slice(-2);
  const month = now.getMonth() + 1;
  const day = now.getDate();
  return `${year}.${month}.${day}`;
}

if (require.main === module) {
  process.stdout.write(buildVersion());
}

module.exports = { buildVersion };
