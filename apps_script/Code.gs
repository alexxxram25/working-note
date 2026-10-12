/**
 * @OnlyCurrentDoc
 *
 * Working Note: the sheet's thin client (the Coach menu).
 *
 * The script reads this sheet, sends lines, criteria and settings to the Coach
 * service, and writes the results back. It holds no Jev key, makes no
 * judgment of its own, and never writes feedback: every word in the Feedback
 * column comes back from the service, which takes it verbatim from this
 * sheet's Criteria tab.
 *
 * Access: this spreadsheet only (@OnlyCurrentDoc), calls to the Coach service,
 * and an OpenID identity token so the service knows who is asking. Nothing
 * else in Drive.
 *
 * Tabs it owns: Flux (Status, Failed criteria, Feedback), Log, Criteria
 * History, Criteria Health and Second Attempts (views, hidden, warning on
 * edit; Run criteria health brings its own two back into view). It writes Calibration status and Last reviewed in the Criteria tab
 * only when a reviewer runs a reviewer action.
 */

const CLIENT_VERSION = 'apps-script/1.0.0';

const TABS = {
  FLUX: 'Flux',
  CRITERIA: 'Criteria',
  SETTINGS: 'Settings',
  LOG: 'Log',
  HISTORY: 'Criteria History',
  HEALTH: 'Criteria Health',
  SECOND: 'Second Attempts',
};
const TAB_ORDER = [TABS.FLUX, TABS.CRITERIA, TABS.SETTINGS, TABS.LOG, TABS.HISTORY, TABS.HEALTH, TABS.SECOND];

// These must match backend/coach/sheet_schema.py; a test holds them together.
const FLUX_COLUMNS = ['Line ID', 'Entity', 'Period', 'Comparison basis', 'Account', 'Prior', 'Current',
  'Variance ($)', 'Variance (%)', 'Commentary', 'Status', 'Failed criteria', 'Feedback', 'Override', 'Override note'];
const CRITERIA_COLUMNS = ['ID', 'Criterion', 'Passes when', 'Feedback when failed', 'Why it matters',
  'Example of good', 'Owner', 'Last reviewed', 'Threshold', 'Calibration status', 'Active (Y/N)'];
const SETTINGS_COLUMNS = ['Setting', 'Value', 'Notes'];
const LOG_COLUMNS = ['Timestamp', 'Close period', 'Line ID', 'Run ID', 'Attempt number', 'Commentary hash',
  'Criterion ID', 'Result', 'Jev probability', 'Criterion version', 'Question-builder version', 'Model version',
  'Calibration status'];
const CRITERIA_HISTORY_COLUMNS = ['Criterion ID', 'Version', 'Changed at', 'Changed by', 'Fields changed', 'Before',
  'After', 'Calibration impact'];
const CRITERIA_HEALTH_COLUMNS = ['Criterion ID', 'Criterion', 'Signal', 'What it suggests', 'Evidence',
  'Close window', 'Resolution', 'Resolved by', 'Resolved at', 'Prompt ID'];
const SECOND_ATTEMPTS_LEAD_COLUMNS = ['Criterion ID', 'Criterion'];
const HEALTH_RESOLUTIONS = ['edit', 'retire', 'keep'];
const SYSTEM_IDS = ['ARITH', 'DUP', 'NEAR'];
const CALIBRATION_FIELDS = ['Passes when', 'Threshold'];

// Only lines that need the preparer's attention are filled (light red); a
// passed line reads as plain text.
const STATUS_STYLES = {
  passed: { background: '#ffffff', color: '#202124' },
  needs_revision: { background: '#fce8e6', color: '#a50e0e' },
  needs_commentary: { background: '#fce8e6', color: '#a50e0e' },
  skipped_immaterial: { background: '#f1f3f4', color: '#5f6368' },
  reviewer_prompt: { background: '#e8f0fe', color: '#174ea6' },
  error: { background: '#f1f3f4', color: '#a50e0e' },
  stale: { background: '#f3e8fd', color: '#6a1b9a' },
};
// Statuses that show the attempt number once a line is past its first draft.
const ATTEMPT_STATUSES = ['passed', 'needs_revision', 'needs_commentary', 'reviewer_prompt'];
const STALE_LABEL = 'Changed during run: rerun Coach';
const HEADER_STYLE = { background: '#003049', color: '#ffffff' };

const PROP = {
  SERVICE_URL: 'COACH_SERVICE_URL',   // script property: shared by everyone using the sheet
  SHEET_KEY: 'COACH_SHEET_KEY',       // script property: preparer-scoped fallback key
  REVIEWER_KEY: 'COACH_REVIEWER_KEY', // user property: a reviewer's own fallback key
  ROLE: 'COACH_ROLE',                 // user property: cached from /v1/me, menu only
  EDIT_HINTS: 'COACH_CRITERIA_EDIT_HINTS', // document property: who edited which criterion
};

// ---------------------------------------------------------------------------
// Menu
// ---------------------------------------------------------------------------

function onOpen() {
  buildMenu_();
}

function buildMenu_() {
  const ui = SpreadsheetApp.getUi();
  const menu = ui.createMenu('Coach')
    .addItem('Run Coach', 'runCoach')
    .addItem('Submit overrides', 'submitOverrides');
  // The service enforces roles; this only keeps reviewer actions out of a
  // preparer's way. An unknown role shows the preparer menu.
  if (cachedRole_() === 'reviewer') {
    menu.addSeparator()
      .addItem('Run criteria health', 'runCriteriaHealth')
      .addItem('Sync criteria status', 'syncCriteriaStatus');
  }
  menu.addSeparator().addSubMenu(ui.createMenu('Admin')
    .addItem('Set up sheet', 'setUpSheet')
    .addItem('Configure service', 'configureService')
    .addItem('Show connection info', 'showConnectionInfo')
    .addItem('Refresh my role', 'refreshRole'));
  menu.addToUi();
}

function cachedRole_() {
  try {
    return PropertiesService.getUserProperties().getProperty(PROP.ROLE) || '';
  } catch (e) {
    return '';
  }
}

// ---------------------------------------------------------------------------
// Run Coach
// ---------------------------------------------------------------------------

function runCoach() {
  runCoach_({});
}

// One run. Returns the service's summary, or null if the run didn't happen
// (the reason has been shown). quiet skips the closing toast.
function runCoach_(options) {
  const lock = LockService.getDocumentLock();
  if (!lock.tryLock(10000)) {
    alert_('Another Coach action is running in this sheet. Try again in a minute.');
    return null;
  }
  let runId = null;
  let written = [];
  let stale = [];
  const ss = SpreadsheetApp.getActive();
  try {
    const flux = requireSheet_(TABS.FLUX);
    let table = readTable_(flux, FLUX_COLUMNS);
    const assigned = assignLineIds_(flux, table);
    if (assigned.length) table = readTable_(flux, FLUX_COLUMNS);
    const read = readLines_(table);
    const lines = read.lines;
    const problems = read.problems;
    if (!lines.length && !problems.length) {
      alert_('There are no lines in the Flux tab to check.');
      return null;
    }
    if (!lines.length) {
      // Every line has a cell the service could not have read: say which, send nothing.
      writeResults_(flux, table, [], {}, problems);
      alert_('Run Coach: no line could be checked.\n- ' + problems.map(p => p.error).join('\n- '));
      return null;
    }
    const sent = {};
    lines.forEach(l => { sent[l.line_id] = l.commentary; });
    const response = callService_('post', '/v1/runs', {
      sheet_id: sheetId_(),
      client_version: CLIENT_VERSION,
      settings: readSettings_(),
      criteria: readCriteria_(),
      criteria_edit_hints: readEditHints_(),
      lines: lines,
    }, { retry: false });
    if (response.code !== 200) {
      showError_(response, 'Run Coach');
      return null;
    }
    const body = response.body;
    runId = body.run_id;

    // Results attach by Line ID, never by row: the sheet may have been sorted
    // while the run was in flight. A line whose commentary changed meanwhile
    // gets no feedback, because the feedback would be about text that is gone.
    const current = readTable_(flux, FLUX_COLUMNS);
    const outcome = writeResults_(flux, current, body.results, sent, problems.concat(body.rejected || []));
    written = outcome.written;
    stale = outcome.stale;

    if (body.criteria_changes && body.criteria_changes.length) {
      appendHistory_(body.criteria_changes);
      clearEditHints_(body.criteria_changes.map(c => c.criterion_id));
    }
    if (cachedRole_() === 'reviewer') writeCalibrationStatus_(body.criteria_status || []);
    refreshOverrideValidation_();
    if (!options.quiet) toast_(summaryText_(body.summary, stale.length, problems.length));
    return body.summary || {};
  } finally {
    if (runId) {
      // Frees the service's write lease. If this call is lost, the lease
      // expires on its own after a few minutes.
      try {
        callService_('post', '/v1/runs/' + runId + '/complete',
          { sheet_id: sheetId_(), written: written, stale: stale }, { retry: true });
      } catch (e) {
        console.warn('could not complete run ' + runId + ': ' + e);
      }
    }
    lock.releaseLock();
  }
}

// The lines to send, and the rows that cannot be sent. A row is a problem when
// a number cell holds something that is not a number (a #DIV/0!, a typo like
// "30,00O"); sending it as blank would let the service derive a variance the
// sheet never showed. A problem names the cell, so the fix is one click away.
function readLines_(table) {
  const lines = [];
  const problems = [];
  table.rows.filter(r => String(r.values['Line ID'] || '').trim()).forEach(row => {
    const v = row.values;
    const line = {
      line_id: String(v['Line ID']).trim(),
      entity: String(v['Entity'] || ''),
      period: periodText_(v['Period']),
      basis: String(v['Comparison basis'] || ''),
      account: String(v['Account'] || ''),
      commentary: v['Commentary'] === null || v['Commentary'] === undefined ? '' : String(v['Commentary']),
      row: row.rowIndex,
    };
    const errors = [];
    [['prior', 'Prior'], ['current', 'Current'], ['variance_usd', 'Variance ($)'], ['variance_pct', 'Variance (%)']]
      .forEach(([key, header]) => {
        try {
          line[key] = numberOrNull_(v[header]);
        } catch (e) {
          line[key] = null;
          errors.push(header + ' (' + cellA1_(row.rowIndex, table.columns[header]) + ') ' + e.message);
        }
      });
    if (errors.length) problems.push({ line_id: line.line_id, row: row.rowIndex, error: errors.join('; ') });
    else lines.push(line);
  });
  return { lines: lines, problems: problems };
}

function writeResults_(sheet, table, results, sent, rejected) {
  const col = table.columns;
  const byId = {};
  const rowsById = {};
  table.rows.forEach(r => {
    const id = String(r.values['Line ID'] || '').trim();
    if (!id) return;
    byId[id] = r;
    (rowsById[id] = rowsById[id] || []).push(r);
  });
  const updates = [];
  const written = [];
  const stale = [];
  results.forEach(result => {
    const row = byId[result.line_id];
    if (!row) return; // the line was deleted while the run was in flight
    const now = row.values['Commentary'] === null || row.values['Commentary'] === undefined
      ? '' : String(row.values['Commentary']);
    if (now !== sent[result.line_id]) {
      stale.push(result.line_id);
      updates.push({ row: row.rowIndex, status: STALE_LABEL, failed: '', feedback: '', note: '', style: STATUS_STYLES.stale });
      return;
    }
    written.push(result.line_id);
    updates.push({
      row: row.rowIndex,
      status: statusText_(result),
      failed: result.failed_cell || '',
      feedback: result.feedback_cell || '',
      note: result.note || '',
      style: STATUS_STYLES[result.status] || STATUS_STYLES.error,
    });
  });
  // A rejected Line ID marks every row that carries it: when the rejection is
  // "this ID appears twice", one of the two rows left blank would read as graded.
  rejected.forEach(item => {
    (rowsById[item.line_id] || []).forEach(row => {
      updates.push({ row: row.rowIndex, status: 'Not graded: ' + item.error, failed: '', feedback: '', note: '',
        style: STATUS_STYLES.error });
    });
  });
  if (updates.length) {
    // One read and one write per column, not one call per cell: on a 40-line
    // tab that is the difference between a second and most of a minute.
    const first = 2;
    const count = sheet.getLastRow() - 1;
    const statusRange = sheet.getRange(first, col['Status'], count, 1);
    const failedRange = sheet.getRange(first, col['Failed criteria'], count, 1);
    const feedbackRange = sheet.getRange(first, col['Feedback'], count, 1);
    const status = statusRange.getValues();
    const backgrounds = statusRange.getBackgrounds();
    const colors = statusRange.getFontColors();
    const notes = statusRange.getNotes();
    const failed = failedRange.getValues();
    const feedback = feedbackRange.getValues();
    updates.forEach(u => {
      const i = u.row - first;
      status[i][0] = u.status;
      backgrounds[i][0] = u.style.background;
      colors[i][0] = u.style.color;
      notes[i][0] = u.note;
      failed[i][0] = u.failed;
      feedback[i][0] = u.feedback;
    });
    statusRange.setValues(status);
    statusRange.setBackgrounds(backgrounds);
    statusRange.setFontColors(colors);
    statusRange.setNotes(notes);
    failedRange.setValues(failed);
    feedbackRange.setValues(feedback);
  }
  return { written: written, stale: stale };
}

// "Passed (attempt 2)": from the second attempt on, the Status cell says which
// attempt this is, so a revision that passes is visible without opening the note.
function statusText_(result) {
  const attempt = Number(result.attempt_number) || 0;
  return result.status_label + (attempt >= 2 && ATTEMPT_STATUSES.indexOf(result.status) >= 0
    ? ' (attempt ' + attempt + ')' : '');
}

function summaryText_(summary, staleCount, problemCount) {
  if (!summary) return 'Coach run finished.';
  const parts = [
    summary.passed + ' passed',
    summary.needs_revision + ' need revision',
    summary.skipped_immaterial + ' skipped as immaterial',
  ];
  if (summary.needs_commentary) parts.push(summary.needs_commentary + ' need commentary');
  if (summary.reviewer_prompt) parts.push(summary.reviewer_prompt + ' with a reviewer prompt');
  if (summary.errors) parts.push(summary.errors + ' not graded');
  if (problemCount) parts.push(problemCount + ' not sent (see Status for the cell to fix)');
  if (staleCount) parts.push(staleCount + ' changed during the run (rerun Coach)');
  return parts.join(', ') + '.';
}

// ---------------------------------------------------------------------------
// Line IDs (same rule as backend/coach/line_ids.py; shared test vectors)
// ---------------------------------------------------------------------------

function cleanPart_(value, name) {
  const text = String(value === null || value === undefined ? '' : value).replace(/\s+/g, ' ').trim();
  if (!text) throw new Error(name + ' is empty');
  if (text.indexOf('|') >= 0) throw new Error(name + " may not contain '|'");
  return text;
}

function accountKey_(account) {
  const text = cleanPart_(account, 'account');
  const match = text.match(/^\s*(\d[\d.\-]*)/);
  if (match) return match[1].replace(/[.\-]+$/, '');
  const slug = text.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').toUpperCase();
  if (!slug) throw new Error('account has no usable characters');
  return slug;
}

function buildLineId_(entity, period, basis, account, slice) {
  const parts = [cleanPart_(entity, 'entity'), cleanPart_(period, 'period'), cleanPart_(basis, 'comparison basis'),
    accountKey_(account)];
  if (slice !== null && slice !== undefined && String(slice).trim()) parts.push(cleanPart_(slice, 'slice'));
  return parts.join('|');
}

// Assigns an ID to rows that have none, once. An existing Line ID is never
// rewritten, even if the account text changes later: the account is display only.
function assignLineIds_(sheet, table) {
  const assigned = [];
  table.rows.forEach(row => {
    const v = row.values;
    if (String(v['Line ID'] || '').trim()) return;
    if (!String(v['Account'] || '').trim() && !String(v['Commentary'] || '').trim()) return;
    try {
      const id = buildLineId_(v['Entity'], periodText_(v['Period']), v['Comparison basis'], v['Account'], null);
      sheet.getRange(row.rowIndex, table.columns['Line ID']).setValue(id);
      assigned.push(id);
    } catch (e) {
      const cell = sheet.getRange(row.rowIndex, table.columns['Status']);
      cell.setValue('Not graded: cannot build a Line ID (' + e.message + ')');
      cell.setBackground(STATUS_STYLES.error.background);
    }
  });
  return assigned;
}

// ---------------------------------------------------------------------------
// Overrides
// ---------------------------------------------------------------------------

function submitOverrides() {
  const flux = requireSheet_(TABS.FLUX);
  const table = readTable_(flux, FLUX_COLUMNS);
  const overrides = table.rows
    .filter(r => String(r.values['Override'] || '').trim() && String(r.values['Line ID'] || '').trim())
    .map(r => ({
      line_id: String(r.values['Line ID']).trim(),
      value: String(r.values['Override']).trim(),
      note: String(r.values['Override note'] || '').trim() || null,
    }));
  if (!overrides.length) {
    alert_('No overrides to submit. Pick a value in the Override column first.');
    return;
  }
  const response = callService_('post', '/v1/overrides', {
    sheet_id: sheetId_(), overrides: overrides,
  }, { retry: true });
  if (response.code !== 200) {
    showError_(response, 'Submit overrides');
    return;
  }
  const body = response.body;
  let message = body.recorded + ' recorded, ' + body.already_recorded + ' already on file.';
  if (body.attributed_to === null) message += ' Saved without attribution: Google did not share your email.';
  if (body.invalid && body.invalid.length) {
    alert_(message + '\n\nNot recorded:\n' + body.invalid.map(i => i.line_id + ': ' + i.error).join('\n'));
  } else {
    toast_(message);
  }
}

function overrideOptions_(criteria) {
  const active = criteria.filter(c => String(c['ID'] || '').trim() && isActive_(c['Active (Y/N)']));
  const ids = active.map(c => String(c['ID']).trim());
  const missable = ids.filter(id => SYSTEM_IDS.indexOf(id) < 0);
  return ids.map(id => 'Flag was wrong: ' + id)
    .concat(missable.map(id => 'Missed an issue: ' + id))
    .concat(['Missed an issue: not covered']);
}

function refreshOverrideValidation_() {
  const flux = SpreadsheetApp.getActive().getSheetByName(TABS.FLUX);
  if (!flux) return;
  const table = readTable_(flux, FLUX_COLUMNS);
  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(overrideOptions_(readCriteria_()), true)
    .setAllowInvalid(false)
    .build();
  const rows = Math.max(flux.getMaxRows() - 1, 1);
  flux.getRange(2, table.columns['Override'], rows, 1).setDataValidation(rule);
}

// ---------------------------------------------------------------------------
// Reviewer actions
// ---------------------------------------------------------------------------

function runCriteriaHealth() {
  const ss = SpreadsheetApp.getActive();
  const healthSheet = ensureTab_(TABS.HEALTH, CRITERIA_HEALTH_COLUMNS);
  let health;
  try {
    health = readTable_(healthSheet, CRITERIA_HEALTH_COLUMNS);
  } catch (e) {
    health = { columns: {}, rows: [] }; // an older layout: this run rewrites it
  }
  const resolutions = health.rows
    .filter(r => HEALTH_RESOLUTIONS.indexOf(String(r.values['Resolution'] || '').trim()) >= 0
      && !String(r.values['Resolved at'] || '').trim() && String(r.values['Prompt ID'] || '').trim())
    .map(r => ({
      id: Number(r.values['Prompt ID']),
      resolution: String(r.values['Resolution']).trim(),
      criterion_id: String(r.values['Criterion ID']).trim(),
    }));

  const response = callService_('post', '/v1/health', {
    sheet_id: sheetId_(),
    criteria: readCriteria_(),
    settings: readSettings_(),
    resolutions: resolutions.map(r => ({ id: r.id, resolution: r.resolution })),
    include_log: true,
    log_limit: 2000,
  }, { retry: false });
  if (response.code !== 200) {
    showError_(response, 'Run criteria health');
    return;
  }
  // The service accepted the resolutions; reflect them in the Criteria tab.
  if (resolutions.length) applyResolutions_(resolutions);
  const body = response.body;
  const prompts = body.prompts || [];
  writeHealth_(prompts);
  const second = writeSecondAttempts_(body.second_attempts);
  writeHistory_(body.history || []);
  writeLog_(body.log || []);

  // The views stay hidden from everyday use; a reviewer who just asked for them
  // gets them in front of them, with the headline in a dialog.
  const healthTab = ss.getSheetByName(TABS.HEALTH);
  if (prompts.length && healthTab) healthTab.showSheet();
  if (second) second.showSheet().activate();
  alert_(healthSummary_(body.second_attempts, prompts.length, body.close_window, resolutions.length));
}

function healthSummary_(table, promptCount, closeWindow, resolved) {
  const lines = [];
  const closes = (table && table.closes) || [];
  if (closes.length) {
    lines.push('Lines that needed a second attempt, by close:');
    closes.forEach(c => lines.push('    ' + c + ':  ' + table.totals[c]));
    if (closes.length > 1 && table.falling) lines.push('Fewer every close.');
    lines.push('The Second Attempts tab breaks this down by criterion.');
  } else {
    lines.push('No closes in the Log yet: run Coach on a close first.');
  }
  lines.push('');
  lines.push(promptCount
    ? promptCount + (promptCount === 1 ? ' criterion' : ' criteria') + ' to look at in ' + (closeWindow || 'this close') +
      ': see the Criteria Health tab, and set Resolution to edit, retire or keep.'
    : 'Criteria Health has nothing to look at in ' + (closeWindow || 'this close') + '.');
  if (resolved) lines.push(resolved + ' resolution(s) recorded.');
  if (table && table.caveat) lines.push('', table.caveat);
  return lines.join('\n');
}

// keep: re-confirms the wording (Last reviewed = today); it does not claim the
// model was revalidated. retire: switches the criterion off. edit: the
// reviewer edits the row; the edit itself becomes a new criterion version.
function applyResolutions_(resolutions) {
  const sheet = requireSheet_(TABS.CRITERIA);
  const table = readTable_(sheet, CRITERIA_COLUMNS);
  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  resolutions.forEach(item => {
    const row = table.rows.find(r => String(r.values['ID'] || '').trim() === item.criterion_id);
    if (!row) return;
    sheet.getRange(row.rowIndex, table.columns['Last reviewed']).setValue(today);
    if (item.resolution === 'retire') sheet.getRange(row.rowIndex, table.columns['Active (Y/N)']).setValue('N');
  });
}

function syncCriteriaStatus() {
  const response = callService_('post', '/v1/criteria/status', {
    sheet_id: sheetId_(), criteria: readCriteria_(),
  }, { retry: true });
  if (response.code !== 200) {
    showError_(response, 'Sync criteria status');
    return;
  }
  writeCalibrationStatus_(response.body.criteria || []);
  toast_('Calibration status updated from the service.');
}

function writeCalibrationStatus_(statuses) {
  const sheet = SpreadsheetApp.getActive().getSheetByName(TABS.CRITERIA);
  if (!sheet || !statuses.length) return;
  const table = readTable_(sheet, CRITERIA_COLUMNS);
  const byId = {};
  statuses.forEach(s => { byId[s.id] = s.calibration_status; });
  table.rows.forEach(row => {
    const id = String(row.values['ID'] || '').trim();
    if (byId[id] && byId[id] !== row.values['Calibration status']) {
      sheet.getRange(row.rowIndex, table.columns['Calibration status']).setValue(byId[id]);
    }
  });
}

// ---------------------------------------------------------------------------
// View tabs (written by the script, protected with a warning)
// ---------------------------------------------------------------------------

function writeHealth_(prompts) {
  const rows = prompts.map(p => [p.criterion_id, p.criterion || '', p.signal, p.suggestion,
    (p.evidence && p.evidence.text) || '', p.close_window || '', '', '', '', p.id === undefined ? '' : p.id]);
  const sheet = replaceRows_(TABS.HEALTH, CRITERIA_HEALTH_COLUMNS, rows);
  const resolution = SpreadsheetApp.newDataValidation().requireValueInList(HEALTH_RESOLUTIONS, true)
    .setAllowInvalid(false).build();
  sheet.getRange(2, CRITERIA_HEALTH_COLUMNS.indexOf('Resolution') + 1, Math.max(rows.length, 1), 1)
    .setDataValidation(resolution);
  sheet.hideColumns(CRITERIA_HEALTH_COLUMNS.indexOf('Prompt ID') + 1);
  sheet.getRange(2, 1, Math.max(rows.length, 1), CRITERIA_HEALTH_COLUMNS.length).setVerticalAlignment('top');
  ['What it suggests', 'Evidence'].forEach(name =>
    sheet.getRange(2, CRITERIA_HEALTH_COLUMNS.indexOf(name) + 1, Math.max(rows.length, 1), 1).setWrap(true));
  setWidths_(sheet, CRITERIA_HEALTH_COLUMNS, { 'Criterion ID': 100, 'Criterion': 220, 'Signal': 110,
    'What it suggests': 360, 'Evidence': 300, 'Close window': 100, 'Resolution': 110, 'Resolved by': 180,
    'Resolved at': 150 });
}

// The total comes first: it's the headline. Each criterion's row below shows
// its count and its share of the lines it judged on a first attempt.
function writeSecondAttempts_(table) {
  if (!table) return null;
  const closes = table.closes || [];
  const header = SECOND_ATTEMPTS_LEAD_COLUMNS.concat(closes);
  const rows = [['Total', 'Lines with any second attempt'].concat(closes.map(c => table.totals[c]))];
  (table.rows || []).forEach(r => rows.push([r.criterion_id, r.criterion || '']
    .concat(closes.map(c => formatAttempts_(r.counts[c], r.rates[c])))));
  rows.push(['', table.caveat || ''].concat(closes.map(() => '')));
  const sheet = replaceRows_(TABS.SECOND, header, rows);
  sheet.getRange(2, 1, 1, header.length).setFontWeight('bold');
  setWidths_(sheet, header, { 'Criterion ID': 100, 'Criterion': 260 });
  closes.forEach((c, i) => sheet.setColumnWidth(SECOND_ATTEMPTS_LEAD_COLUMNS.length + i + 1, 110));
  if (closes.length) {
    sheet.getRange(2, SECOND_ATTEMPTS_LEAD_COLUMNS.length + 1, rows.length, closes.length).setHorizontalAlignment('right');
  }
  return sheet;
}

// Wide enough to read on screen: the views are written by the script, so their
// widths are set here rather than left at Sheets' default.
function setWidths_(sheet, header, widths) {
  Object.keys(widths).forEach(name => sheet.setColumnWidth(header.indexOf(name) + 1, widths[name]));
}

function formatAttempts_(count, rate) {
  if (rate === null || rate === undefined) return count;
  return count + ' (' + Math.round(rate * 100) + '%)';
}

function writeHistory_(history) {
  replaceRows_(TABS.HISTORY, CRITERIA_HISTORY_COLUMNS, history.map(h => [h.criterion_id, h.version,
    h.changed_at || '', h.changed_by || h.detected_by || '', h.fields_changed, h.before, h.after,
    h.calibration_impact]));
}

function appendHistory_(changes) {
  const sheet = ensureTab_(TABS.HISTORY, CRITERIA_HISTORY_COLUMNS);
  const stamp = new Date().toISOString();
  const rows = changes.map(c => [c.criterion_id, c.version, stamp, c.changed_by || c.detected_by || '',
    c.fields_changed, c.before, c.after, c.calibration_impact]);
  if (rows.length) sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
}

function writeLog_(entries) {
  replaceRows_(TABS.LOG, LOG_COLUMNS, entries.map(e => [e.ts, e.close_period, e.line_id, e.run_id,
    e.attempt_number, e.commentary_hash, e.criterion_id, e.result,
    e.jev_probability === null || e.jev_probability === undefined ? '' : e.jev_probability,
    e.criterion_version === null || e.criterion_version === undefined ? '' : e.criterion_version,
    e.question_builder_version || '', e.model_version || '', e.calibration_status || '']));
}

function replaceRows_(tabName, header, rows) {
  const sheet = ensureTab_(tabName, header);
  const last = sheet.getLastRow();
  if (last > 0) sheet.getRange(1, 1, last, Math.max(sheet.getLastColumn(), header.length)).clearContent();
  sheet.getRange(1, 1, 1, header.length).setValues([header]);
  styleHeader_(sheet, header.length);
  if (rows.length) sheet.getRange(2, 1, rows.length, header.length).setValues(rows);
  return sheet;
}

// ---------------------------------------------------------------------------
// Admin
// ---------------------------------------------------------------------------

function setUpSheet() {
  const ss = SpreadsheetApp.getActive();
  TAB_ORDER.forEach(name => ensureTab_(name, headerFor_(name)));

  const criteria = ss.getSheetByName(TABS.CRITERIA);
  const settings = ss.getSheetByName(TABS.SETTINGS);
  const needsDefaults = criteria.getLastRow() < 2 || settings.getLastRow() < 2;
  if (needsDefaults) {
    const response = callService_('get', '/v1/defaults', undefined, { retry: true });
    if (response.code !== 200) {
      showError_(response, 'Set up sheet');
      return;
    }
    if (criteria.getLastRow() < 2) {
      const rows = response.body.criteria.map(c => CRITERIA_COLUMNS.map(h => c[h] === undefined ? '' : c[h]));
      if (rows.length) criteria.getRange(2, 1, rows.length, CRITERIA_COLUMNS.length).setValues(rows);
    }
    if (settings.getLastRow() < 2) {
      const rows = response.body.settings.map(s => [s.Setting, s.Value, s.Notes]);
      if (rows.length) settings.getRange(2, 1, rows.length, 3).setValues(rows);
    }
  }

  formatFlux_(ss.getSheetByName(TABS.FLUX));
  const active = SpreadsheetApp.newDataValidation().requireValueInList(['Y', 'N'], true)
    .setAllowInvalid(false).build();
  criteria.getRange(2, CRITERIA_COLUMNS.indexOf('Active (Y/N)') + 1, Math.max(criteria.getMaxRows() - 1, 1), 1)
    .setDataValidation(active);
  refreshOverrideValidation_();

  const protectedTo = protectReviewerTabs_();
  [TABS.LOG, TABS.HISTORY, TABS.HEALTH, TABS.SECOND].forEach(name => {
    const sheet = ss.getSheetByName(name);
    protectWithWarning_(sheet, name + ' is written by the Coach service.');
    sheet.hideSheet();
  });
  const flux = ss.getSheetByName(TABS.FLUX);
  const table = readTable_(flux, FLUX_COLUMNS);
  // Set up sheet runs again after onboarding files are imported: replace these, don't stack them.
  const existing = flux.getProtections(SpreadsheetApp.ProtectionType.RANGE);
  ['Status', 'Failed criteria', 'Feedback'].forEach(name => {
    const description = name + ' is written by Coach';
    existing.filter(p => p.getDescription() === description).forEach(p => p.remove());
    const range = flux.getRange(1, table.columns[name], flux.getMaxRows(), 1);
    range.protect().setDescription(description).setWarningOnly(true);
  });
  alert_('The sheet is set up.' + (protectedTo
    ? '\n\nCriteria and Settings are locked to reviewers (the lock on their tabs). They are editable by: ' +
      protectedTo.join(', ') + '. Preparers can read them but not change them.'
    : '\n\nCriteria and Settings protection was skipped: ' +
      'run this as a reviewer once the service knows your role.'));
}

function headerFor_(name) {
  return {
    'Flux': FLUX_COLUMNS, 'Criteria': CRITERIA_COLUMNS, 'Settings': SETTINGS_COLUMNS, 'Log': LOG_COLUMNS,
    'Criteria History': CRITERIA_HISTORY_COLUMNS, 'Criteria Health': CRITERIA_HEALTH_COLUMNS,
    'Second Attempts': SECOND_ATTEMPTS_LEAD_COLUMNS,
  }[name];
}

function protectReviewerTabs_() {
  const response = callService_('get', '/v1/team', undefined, { retry: true });
  if (response.code !== 200) return null;
  const reviewers = response.body.reviewers || [];
  const me = Session.getEffectiveUser().getEmail();
  [TABS.CRITERIA, TABS.SETTINGS].forEach(name => {
    const sheet = SpreadsheetApp.getActive().getSheetByName(name);
    sheet.getProtections(SpreadsheetApp.ProtectionType.SHEET).forEach(p => p.remove());
    const protection = sheet.protect().setDescription(name + ': reviewers only. Preparers can read it but not edit it.');
    protection.setWarningOnly(false);
    if (protection.canDomainEdit()) protection.setDomainEdit(false);
    const keep = reviewers.concat(me ? [me] : []);
    protection.removeEditors(protection.getEditors().filter(e => keep.indexOf(e.getEmail()) < 0));
    if (reviewers.length) protection.addEditors(reviewers);
  });
  return reviewers;
}

function protectWithWarning_(sheet, description) {
  sheet.getProtections(SpreadsheetApp.ProtectionType.SHEET).forEach(p => p.remove());
  sheet.protect().setDescription(description).setWarningOnly(true);
}

function formatFlux_(sheet) {
  const table = readTable_(sheet, FLUX_COLUMNS);
  const rows = Math.max(sheet.getMaxRows() - 1, 1);
  ['Prior', 'Current', 'Variance ($)'].forEach(name =>
    sheet.getRange(2, table.columns[name], rows, 1).setNumberFormat('#,##0;(#,##0)'));
  sheet.getRange(2, table.columns['Variance (%)'], rows, 1).setNumberFormat('0.0%');
  ['Commentary', 'Feedback', 'Override note'].forEach(name =>
    sheet.getRange(2, table.columns[name], rows, 1).setWrap(true).setVerticalAlignment('top'));
  const widths = { 'Line ID': 220, 'Account': 230, 'Commentary': 420, 'Status': 190, 'Failed criteria': 150,
    'Feedback': 380, 'Override': 200, 'Override note': 220 };
  Object.keys(widths).forEach(name => sheet.setColumnWidth(table.columns[name], widths[name]));
}

function configureService() {
  const ui = SpreadsheetApp.getUi();
  const props = PropertiesService.getScriptProperties();
  const current = props.getProperty(PROP.SERVICE_URL) || '';
  const url = ui.prompt('Coach service URL', 'The https address of your Coach service' +
    (current ? ' (now ' + current + ')' : '') + '.', ui.ButtonSet.OK_CANCEL);
  if (url.getSelectedButton() !== ui.Button.OK) return;
  const value = url.getResponseText().trim().replace(/\/+$/, '');
  if (value) {
    if (!/^https:\/\//.test(value)) {
      alert_('The service URL must start with https://');
      return;
    }
    props.setProperty(PROP.SERVICE_URL, value);
  }
  const key = ui.prompt('Sheet key (optional)', 'Only if your service uses sheet keys instead of Google ' +
    'sign-in. Leave blank to keep the current setting.', ui.ButtonSet.OK_CANCEL);
  if (key.getSelectedButton() === ui.Button.OK && key.getResponseText().trim()) {
    props.setProperty(PROP.SHEET_KEY, key.getResponseText().trim());
  }
  toast_('Coach service configured.');
}

function showConnectionInfo() {
  const lines = ['Service: ' + (PropertiesService.getScriptProperties().getProperty(PROP.SERVICE_URL) || 'not set')];
  const claims = tokenClaims_();
  if (claims) {
    lines.push('Identity token audience (set GOOGLE_AUDIENCES to this): ' + claims.aud);
    lines.push('Signed-in email in the token: ' + (claims.email || 'none (you will act as a preparer)'));
    if (claims.hd) lines.push('Workspace domain: ' + claims.hd);
  } else {
    lines.push('No identity token: the openid scope is missing or was not granted.');
  }
  try {
    const health = callService_('get', '/healthz', undefined, { retry: true, anonymous: true });
    if (health.code === 200) {
      const b = health.body;
      lines.push('Service store: ' + b.store + '; Jev ' + b.jev.mode + ' ' + b.jev.request_model +
        ' (pinned ' + b.jev.pinned_version + '); auth: ' + b.auth.join(', '));
    } else {
      lines.push('Service health check failed (' + health.code + ').');
    }
    const me = callService_('get', '/v1/me', undefined, { retry: true });
    lines.push(me.code === 200 ? 'Your role: ' + me.body.role + (me.body.verified ? ' (verified)' : ' (unverified)')
      : 'The service did not accept your credentials: ' + detailOf_(me.body));
  } catch (e) {
    lines.push('Could not reach the service: ' + e.message);
  }
  alert_(lines.join('\n'));
}

function refreshRole() {
  const me = callService_('get', '/v1/me', undefined, { retry: true });
  if (me.code !== 200) {
    showError_(me, 'Refresh my role');
    return;
  }
  PropertiesService.getUserProperties().setProperty(PROP.ROLE, me.body.role);
  buildMenu_();
  alert_(roleText_(me.body.role));
}

function roleText_(role) {
  if (role === 'reviewer') {
    return 'You are a reviewer on this sheet\'s team list.\n\n' +
      'That is why you can edit the Criteria and Settings tabs: the lock on those tabs means only reviewers can ' +
      'change them. Preparers can read them but not edit them. You can also run criteria health and resolve its ' +
      'prompts.\n\nEveryone, reviewers included, writes Commentary and uses Override in the Flux tab.';
  }
  return 'You are a ' + role + ' on this sheet\'s team list.\n\n' +
    'You write Commentary in the Flux tab, run Coach, and flag a result you disagree with in Override. ' +
    'Criteria and Settings are locked to reviewers (the lock on those tabs): you can read them but not change them.';
}

// The service knows this sheet by its spreadsheet ID.
function sheetId_() {
  return SpreadsheetApp.getActive().getId();
}

// ---------------------------------------------------------------------------
// Criteria edits: who edited, and an immediate calibration hint
// ---------------------------------------------------------------------------

// Simple trigger: runs as the editing user, so it can read their email when
// Google shares it. It records a hint the service stores as "changed by"; the
// service still decides what changed and what that did to calibration.
function onEdit(e) {
  if (!e || !e.range) return;
  const sheet = e.range.getSheet();
  if (sheet.getName() !== TABS.CRITERIA || e.range.getRow() < 2) return;
  const table = readTable_(sheet, CRITERIA_COLUMNS);
  const columns = [];
  for (let c = e.range.getColumn(); c < e.range.getColumn() + e.range.getNumColumns(); c++) {
    const name = Object.keys(table.columns).find(k => table.columns[k] === c);
    if (name) columns.push(name);
  }
  const touchesContent = columns.some(n => ['Last reviewed', 'Calibration status'].indexOf(n) < 0);
  if (!touchesContent) return;
  let email = '';
  try {
    email = (e.user && e.user.getEmail && e.user.getEmail()) || Session.getActiveUser().getEmail() || '';
  } catch (err) {
    email = '';
  }
  const hints = readEditHints_();
  const recalibrate = columns.some(n => CALIBRATION_FIELDS.indexOf(n) >= 0);
  const edited = [];
  for (let r = e.range.getRow(); r < e.range.getRow() + e.range.getNumRows(); r++) {
    const row = table.rows.find(x => x.rowIndex === r);
    const id = row ? String(row.values['ID'] || '').trim() : '';
    if (!id) continue;
    edited.push(id);
    hints[id] = { by: email, at: new Date().toISOString() };
    if (recalibrate && SYSTEM_IDS.indexOf(id) < 0) {
      sheet.getRange(r, table.columns['Calibration status']).setValue('Uncalibrated (re-check needed)');
    }
  }
  PropertiesService.getDocumentProperties().setProperty(PROP.EDIT_HINTS, JSON.stringify(hints));
  if (edited.length) {
    toast_(edited.join(', ') + ' saved: Coach uses the new wording on the next run.' +
      (cachedRole_() === 'reviewer' ? ' You can edit this tab because you are a reviewer; the lock keeps ' +
        'preparers to reading it.' : ''));
  }
}

function readEditHints_() {
  try {
    return JSON.parse(PropertiesService.getDocumentProperties().getProperty(PROP.EDIT_HINTS) || '{}');
  } catch (e) {
    return {};
  }
}

function clearEditHints_(ids) {
  const hints = readEditHints_();
  ids.forEach(id => { delete hints[id]; });
  PropertiesService.getDocumentProperties().setProperty(PROP.EDIT_HINTS, JSON.stringify(hints));
}

// ---------------------------------------------------------------------------
// Service calls
// ---------------------------------------------------------------------------

function serviceUrl_() {
  const url = PropertiesService.getScriptProperties().getProperty(PROP.SERVICE_URL);
  if (!url) throw new Error('The Coach service URL is not set. Use Coach > Admin > Configure service.');
  return url.replace(/\/+$/, '');
}

function credentials_() {
  let token = null;
  try {
    token = ScriptApp.getIdentityToken();
  } catch (e) {
    token = null;
  }
  const key = PropertiesService.getUserProperties().getProperty(PROP.REVIEWER_KEY)
    || PropertiesService.getScriptProperties().getProperty(PROP.SHEET_KEY);
  return { token: token, key: key };
}

function headersFor_(credentials, useKey) {
  const headers = { 'X-Coach-Client': CLIENT_VERSION };
  if (!useKey && credentials.token) {
    headers['Authorization'] = 'Bearer ' + credentials.token;
    return headers;
  }
  if (credentials.key) {
    headers['X-Coach-Sheet-Key'] = credentials.key;
    let email = '';
    try {
      email = Session.getActiveUser().getEmail();
    } catch (e) {
      email = '';
    }
    if (email) headers['X-Coach-User'] = email; // recorded as unverified
    return headers;
  }
  throw new Error('No credentials: no Google identity token and no sheet key. ' +
    'See Coach > Admin > Show connection info.');
}

// Retries only when asked: a run is never retried blindly, because a retry
// after a lost response would find its own lease and report the sheet busy.
function callService_(method, path, payload, options) {
  options = options || {};
  const url = serviceUrl_() + path;
  const credentials = options.anonymous ? null : credentials_();
  const attempt = (useKey) => {
    const params = {
      method: method,
      contentType: 'application/json',
      muteHttpExceptions: true,
      followRedirects: true,
      headers: credentials ? headersFor_(credentials, useKey) : { 'X-Coach-Client': CLIENT_VERSION },
    };
    if (payload !== undefined) params.payload = JSON.stringify(payload);
    let response = null;
    const tries = options.retry ? 3 : 1;
    for (let i = 0; i < tries; i++) {
      try {
        response = UrlFetchApp.fetch(url, params);
      } catch (e) {
        // A dropped connection ("Address unavailable") throws instead of
        // answering. Retry it like a 503, or a lost completion holds the
        // sheet's lease until it expires.
        if (i === tries - 1) throw e;
        Utilities.sleep(1000 * Math.pow(2, i));
        continue;
      }
      const code = response.getResponseCode();
      if ([429, 502, 503, 504].indexOf(code) < 0) break;
      if (i < tries - 1) Utilities.sleep(1000 * Math.pow(2, i));
    }
    const text = response.getContentText();
    let body;
    try {
      body = text ? JSON.parse(text) : {};
    } catch (e) {
      body = { detail: text.slice(0, 300) };
    }
    return { code: response.getResponseCode(), body: body };
  };
  let result = attempt(false);
  // A service that only accepts sheet keys answers a token with 401; fall back once.
  if (credentials && result.code === 401 && credentials.token && credentials.key) result = attempt(true);
  return result;
}

function detailOf_(body) {
  if (!body) return '';
  const detail = body.detail;
  if (detail && typeof detail === 'object') {
    return (detail.message || '') + (detail.problems && detail.problems.length ? ':\n- ' + detail.problems.join('\n- ') : '');
  }
  return detail || '';
}

function showError_(response, action) {
  const detail = detailOf_(response.body);
  const requestId = response.body && response.body.request_id ? ' (request ' + response.body.request_id + ')' : '';
  const messages = {
    401: 'The Coach service did not accept your credentials: ' + detail,
    403: detail || 'You are not allowed to do that.',
    404: detail || 'The Coach service did not recognise that request.',
    409: detail,
    413: 'Too much data for one run. Split the Flux tab and run again.',
    422: 'Something in the sheet needs fixing: ' + detail,
    429: 'Too many requests. Wait a minute and try again.',
  };
  const message = messages[response.code] ||
    'The Coach service hit an error' + requestId + '. Try again; if it keeps happening, share the request ID.';
  alert_(action + ': ' + message);
}

function tokenClaims_() {
  let token = null;
  try {
    token = ScriptApp.getIdentityToken();
  } catch (e) {
    return null;
  }
  if (!token) return null;
  const part = token.split('.')[1];
  const json = Utilities.newBlob(Utilities.base64DecodeWebSafe(part + '==='.slice((part.length + 3) % 4)))
    .getDataAsString();
  return JSON.parse(json);
}

// ---------------------------------------------------------------------------
// Sheet helpers
// ---------------------------------------------------------------------------

function requireSheet_(name) {
  const sheet = SpreadsheetApp.getActive().getSheetByName(name);
  if (!sheet) throw new Error('There is no "' + name + '" tab. Run Coach > Admin > Set up sheet.');
  return sheet;
}

function ensureTab_(name, header) {
  const ss = SpreadsheetApp.getActive();
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.getRange(1, 1, 1, header.length).setValues([header]);
  } else if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, header.length).setValues([header]);
  }
  styleHeader_(sheet, header.length);
  return sheet;
}

function styleHeader_(sheet, width) {
  const range = sheet.getRange(1, 1, 1, width);
  range.setFontWeight('bold');
  range.setBackground(HEADER_STYLE.background);
  range.setFontColor(HEADER_STYLE.color);
  sheet.setFrozenRows(1);
}

// Reads a tab by header name, so columns may be reordered without breaking
// anything. A missing column is an error with a fix, not a silent skip.
function readTable_(sheet, required) {
  const lastRow = sheet.getLastRow();
  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const header = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(h => String(h).trim());
  const columns = {};
  header.forEach((h, i) => { if (h && columns[h] === undefined) columns[h] = i + 1; });
  const missing = required.filter(h => columns[h] === undefined);
  if (missing.length) {
    throw new Error('The "' + sheet.getName() + '" tab is missing: ' + missing.join(', ') +
      '. Run Coach > Admin > Set up sheet.');
  }
  const rows = [];
  if (lastRow >= 2) {
    const values = sheet.getRange(2, 1, lastRow - 1, lastCol).getValues();
    values.forEach((rowValues, i) => {
      const record = {};
      header.forEach((h, j) => { if (h) record[h] = rowValues[j]; });
      if (rowValues.some(v => v !== '' && v !== null)) rows.push({ rowIndex: i + 2, values: record });
    });
  }
  return { columns: columns, rows: rows };
}

function readCriteria_() {
  const sheet = requireSheet_(TABS.CRITERIA);
  return readTable_(sheet, CRITERIA_COLUMNS).rows
    .filter(r => String(r.values['ID'] || '').trim())
    .map(r => {
      const out = {};
      CRITERIA_COLUMNS.forEach(h => { out[h] = cellText_(r.values[h], h); });
      return out;
    });
}

function readSettings_() {
  const sheet = requireSheet_(TABS.SETTINGS);
  return readTable_(sheet, SETTINGS_COLUMNS).rows
    .filter(r => String(r.values['Setting'] || '').trim())
    .map(r => ({ Setting: String(r.values['Setting']).trim(), Value: r.values['Value'] }));
}

function cellText_(value, header) {
  if (value instanceof Date) return Utilities.formatDate(value, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  if (header === 'Threshold' && typeof value === 'number') return value;
  return value === null || value === undefined ? '' : String(value);
}

function periodText_(value) {
  if (value instanceof Date) return Utilities.formatDate(value, Session.getScriptTimeZone(), 'yyyy-MM');
  return String(value === null || value === undefined ? '' : value).trim();
}

// A blank cell (or n/a) is "not given" and comes back null. Anything else must
// read as a finite number, the same way backend/coach/models.py reads it:
// "$25,000", "(1,200)", "4.5%". A spreadsheet error (#DIV/0!, #REF!) or a typo
// ("30,00O") throws, with the offending text, instead of passing as blank.
function numberOrNull_(value) {
  if (value === '' || value === null || value === undefined) return null;
  if (typeof value === 'number') {
    if (!isFinite(value)) throw new Error('is not a finite number: ' + value);
    return value;
  }
  const raw = String(value).trim();
  if (!raw || /^(n\/a|na|-|none)$/i.test(raw)) return null;
  if (raw.charAt(0) === '#') throw new Error('is a spreadsheet error: ' + raw);
  const text = raw.replace(/[$,\s]/g, '');
  const negative = /^\(.*\)$/.test(text);
  const body = negative ? text.slice(1, -1) : text;
  const percent = /%$/.test(body);
  const digits = body.replace(/%$/, '');
  const n = Number(digits);
  if (digits === '' || !isFinite(n)) throw new Error('is not a number: ' + raw);
  const magnitude = percent ? n / 100 : n;
  return negative ? -magnitude : magnitude;
}

// "G7" for row 7 of column 7: the address a person types into the Name box.
function cellA1_(row, col) {
  let letters = '';
  for (let n = col; n > 0; n = Math.floor((n - 1) / 26)) {
    letters = String.fromCharCode(65 + ((n - 1) % 26)) + letters;
  }
  return letters + row;
}

function isActive_(value) {
  return ['y', 'yes', 'true', '1', 'active'].indexOf(String(value === undefined ? 'Y' : value).trim().toLowerCase()) >= 0;
}

function alert_(message) {
  SpreadsheetApp.getUi().alert(message);
}

function toast_(message) {
  SpreadsheetApp.getActive().toast(message, 'Coach', 8);
}
