'use strict';

// Build versions use local wall-clock time in a valid three-part numeric
// form: YY.MDD.HHmm. Example: 12 August 2026 at 12:31 -> 26.812.1231.
// Keeping this in one cross-platform script guarantees that Windows and
// Linux use the same calculation without rewriting package.json.
function buildVersion(now = new Date()) {
  const year = String(now.getFullYear()).slice(-2);
  const monthDay = `${now.getMonth() + 1}${String(now.getDate()).padStart(2, '0')}`;
  const hourMinute = `${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}`;
  return `${year}.${monthDay}.${hourMinute}`;
}

if (require.main === module) {
  process.stdout.write(buildVersion());
}

module.exports = { buildVersion };
