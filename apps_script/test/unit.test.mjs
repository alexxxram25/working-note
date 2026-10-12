// Unit tests for Code.gs against in-memory Apps Script mocks and a scripted service.
// Run: node --test apps_script/test/unit.test.mjs

import assert from 'node:assert/strict';
import test from 'node:test';
import { loadCoach, readFixture } from './gas_mocks.mjs';

const SERVICE = 'https://coach.example.com';
const vectors = readFixture('line_id_vectors.json');

function json(code, body) {
  return { code, text: JSON.stringify(body) };
}

// A scripted service: routes are "METHOD /path" -> (payload, params) => reply.
function service(routes) {
  const calls = [];
  const fetch = (url, params) => {
    const path = url.replace(SERVICE, '');
    const key = `${(params.method || 'get').toUpperCase()} ${path}`;
    const payload = params.payload ? JSON.parse(params.payload) : undefined;
    calls.push({ key, payload, headers: params.headers });
    const handler = routes[key] || Object.entries(routes).find(([k]) => k.endsWith('*') && key.startsWith(k.slice(0, -1)))?.[1];
    if (!handler) return json(404, { detail: 'no route ' + key });
    return handler(payload, params);
  };
  return { fetch, calls };
}

function criteriaRows() {
  return [
    { 'ID': 'C1', 'Criterion': 'Names a specific driver', 'Passes when': 'Points to a vendor', 'Feedback when failed': 'Name it.', 'Threshold': 0.91, 'Calibration status': 'Calibrated', 'Active (Y/N)': 'Y', 'Last reviewed': new Date(2026, 8, 8) },
    { 'ID': 'C2', 'Criterion': 'Explains why', 'Passes when': 'Says what changed', 'Feedback when failed': 'Why?', 'Threshold': 0.84, 'Calibration status': 'Calibrated', 'Active (Y/N)': 'N' },
    { 'ID': 'ARITH', 'Criterion': 'Amounts add up', 'Feedback when failed': 'Add it up.', 'Threshold': 'n/a', 'Calibration status': 'Deterministic', 'Active (Y/N)': 'Y' },
    { 'ID': 'RESTATE', 'Criterion': 'Says more than the row', 'Passes when': 'Adds something', 'Feedback when failed': 'Say more.', 'Threshold': 0.85, 'Calibration status': 'Uncalibrated', 'Active (Y/N)': 'Y' },
  ];
}

function setup(options = {}) {
  const env = loadCoach({ token: 'header.' + Buffer.from(JSON.stringify({ aud: 'client-123.apps.googleusercontent.com', email: 'rev@example.com', hd: 'example.com' })).toString('base64url') + '.sig', ...options });
  env.props.script.setProperty('COACH_SERVICE_URL', SERVICE);
  const C = env.constants;
  env.ss.tab('Flux', C.FLUX_COLUMNS, [
    { 'Line ID': 'FHD-US|2026-09|MoM|5000', 'Entity': 'FHD-US', 'Period': '2026-09', 'Comparison basis': 'MoM', 'Account': '5000 Cloud hosting', 'Prior': 409000, 'Current': 457500, 'Variance ($)': 48500, 'Variance (%)': 0.118582, 'Commentary': 'Hosting rose $48.5K because of a one-off data load.' },
    { 'Line ID': '', 'Entity': 'FHD-US', 'Period': '2026-09', 'Comparison basis': 'MoM', 'Account': '6400 Depreciation', 'Prior': 129000, 'Current': 131000, 'Variance ($)': 2000, 'Variance (%)': 0.0155, 'Commentary': 'In line.' },
    { 'Line ID': 'FHD-US|2026-09|MoM|1200', 'Entity': 'FHD-US', 'Period': '2026-09', 'Comparison basis': 'MoM', 'Account': '1200 Accounts receivable', 'Prior': '7,598,000', 'Current': '(10)', 'Variance ($)': '', 'Variance (%)': '', 'Commentary': 'AR fell.' },
  ]);
  env.ss.tab('Criteria', C.CRITERIA_COLUMNS, criteriaRows());
  env.ss.tab('Settings', C.SETTINGS_COLUMNS, [{ 'Setting': 'Materiality ($)', 'Value': 25000 }, { 'Setting': 'Pinned model version', 'Value': 'jev-1.13.0' }]);
  return env;
}

function runResponse(extra = {}) {
  return {
    run_id: '11111111-1111-1111-1111-111111111111',
    results: [
      { line_id: 'FHD-US|2026-09|MoM|5000', status: 'needs_revision', status_label: 'Needs revision', attempt_number: 1, failed_cell: 'C1 (likely)', feedback_cell: 'C1: Name it.', note: 'C1: p=0.88 < threshold 0.91' },
      { line_id: 'FHD-US|2026-09|MoM|6400', status: 'skipped_immaterial', status_label: 'Skipped: immaterial', attempt_number: 2, failed_cell: '', feedback_cell: '', note: '' },
    ],
    rejected: [{ line_id: 'FHD-US|2026-09|MoM|1200', error: 'cannot read the variance' }],
    criteria_status: [{ id: 'RESTATE', calibration_status: 'Calibrated', version: 1 }],
    criteria_changes: [{ criterion_id: 'C1', version: 2, fields_changed: 'Feedback when failed', before: 'a', after: 'b', calibration_impact: 'none: Feedback when failed only', changed_by: 'rev@example.com' }],
    summary: { passed: 0, needs_revision: 1, skipped_immaterial: 1, needs_commentary: 0, reviewer_prompt: 0, errors: 0 },
    ...extra,
  };
}

test('Line IDs match the shared vectors', () => {
  const { coach } = loadCoach();
  for (const v of vectors.build) {
    assert.equal(coach.buildLineId_(v.entity, v.period, v.basis, v.account, v.slice), v.expected);
  }
  for (const v of vectors.invalid_build) {
    assert.throws(() => coach.buildLineId_(v.entity, v.period, v.basis, v.account, v.slice));
  }
});

test('override options cover active criteria; system checks cannot be "missed"', () => {
  const { coach } = loadCoach();
  const options = coach.overrideOptions_(criteriaRows());
  assert.deepEqual(options, [
    'Flag was wrong: C1', 'Flag was wrong: ARITH', 'Flag was wrong: RESTATE',
    'Missed an issue: C1', 'Missed an issue: RESTATE', 'Missed an issue: not covered',
  ]);
});

test('a missing column is an error that says how to fix it', () => {
  const env = setup();
  env.ss.getSheetByName('Flux').getRange(1, 10).setValue('Notes');
  assert.throws(() => env.coach.runCoach(), /missing: Commentary.*Set up sheet/);
});

test('Run Coach sends the sheet, writes results by Line ID and completes the run', () => {
  const env = setup();
  env.props.user.setProperty('COACH_ROLE', 'reviewer');
  const svc = service({
    'POST /v1/runs': () => json(200, runResponse()),
    'POST /v1/runs/11111111-1111-1111-1111-111111111111/complete': () => json(200, { released: true }),
  });
  env.state.fetch = svc.fetch;
  env.coach.runCoach();

  const run = svc.calls.find(c => c.key === 'POST /v1/runs');
  assert.equal(run.payload.sheet_id, 'sheet-test-1');
  assert.equal(run.headers.Authorization.split(' ')[0], 'Bearer');
  assert.equal(run.payload.lines.length, 3);
  const assigned = run.payload.lines.find(l => l.account === '6400 Depreciation');
  assert.equal(assigned.line_id, 'FHD-US|2026-09|MoM|6400');
  const ar = run.payload.lines.find(l => l.line_id.endsWith('1200'));
  assert.equal(ar.prior, 7598000);
  assert.equal(ar.current, -10);
  assert.equal(ar.variance_usd, null);
  const c1 = run.payload.criteria.find(c => c.ID === 'C1');
  assert.equal(c1['Last reviewed'], '2026-09-08');
  assert.equal(c1['Threshold'], 0.91);

  const flux = env.ss.getSheetByName('Flux');
  const row = flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|5000');
  assert.equal(flux.value(row, 'Status'), 'Needs revision');
  assert.equal(flux.value(row, 'Failed criteria'), 'C1 (likely)');
  assert.equal(flux.value(row, 'Feedback'), 'C1: Name it.');
  assert.equal(flux.styleAt(row, 'Status').bg, env.constants.STATUS_STYLES.needs_revision.background);
  assert.match(flux.styleAt(row, 'Status').note, /p=0.88/);
  assert.equal(flux.value(flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|6400'), 'Status'), 'Skipped: immaterial',
    'an immaterial line never shows an attempt');
  const rejected = flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|1200');
  assert.equal(flux.value(rejected, 'Status'), 'Not graded: cannot read the variance');

  const done = svc.calls.find(c => c.key.endsWith('/complete'));
  assert.deepEqual(done.payload.stale, []);
  assert.deepEqual(done.payload.written.sort(), ['FHD-US|2026-09|MoM|5000', 'FHD-US|2026-09|MoM|6400']);
  assert.equal(env.ss.getSheetByName('Criteria History').rows()[0]['Calibration impact'], 'none: Feedback when failed only');
  const criteria = env.ss.getSheetByName('Criteria');
  assert.equal(criteria.value(criteria.rowWhere('ID', 'RESTATE'), 'Calibration status'), 'Calibrated');
  assert.match(env.ss.toasts.at(-1), /1 need revision/);
});

test('a preparer run never writes the Criteria tab', () => {
  const env = setup();
  const svc = service({ 'POST /v1/runs': () => json(200, runResponse()), 'POST /v1/runs/*': () => json(200, {}) });
  env.state.fetch = svc.fetch;
  env.coach.runCoach();
  const criteria = env.ss.getSheetByName('Criteria');
  assert.equal(criteria.value(criteria.rowWhere('ID', 'RESTATE'), 'Calibration status'), 'Uncalibrated');
});

test('a line edited mid-run gets no feedback and is reported stale', () => {
  const env = setup();
  const flux = env.ss.getSheetByName('Flux');
  const row = flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|5000');
  const svc = service({
    'POST /v1/runs': () => {
      flux.setValueAt(row, 'Commentary', 'Hosting rose $48.5K because Orion Freight ran a data load.');
      return json(200, runResponse());
    },
    'POST /v1/runs/*': () => json(200, {}),
  });
  env.state.fetch = svc.fetch;
  env.coach.runCoach();
  assert.equal(flux.value(row, 'Status'), env.constants.STALE_LABEL);
  assert.equal(flux.value(row, 'Feedback'), '');
  const done = svc.calls.find(c => c.key.endsWith('/complete'));
  assert.deepEqual(done.payload.stale, ['FHD-US|2026-09|MoM|5000']);
  assert.match(env.ss.toasts.at(-1), /1 changed during the run/);
});

test('results follow the Line ID even if the rows were re-sorted mid-run', () => {
  const env = setup();
  const flux = env.ss.getSheetByName('Flux');
  const svc = service({
    'POST /v1/runs': () => {
      const rows = flux.getRange(2, 1, 3, 15).getValues();
      flux.getRange(2, 1, 3, 15).setValues([rows[2], rows[0], rows[1]]);
      return json(200, runResponse());
    },
    'POST /v1/runs/*': () => json(200, {}),
  });
  env.state.fetch = svc.fetch;
  env.coach.runCoach();
  const row = flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|5000');
  assert.equal(row, 3);
  assert.equal(flux.value(row, 'Feedback'), 'C1: Name it.');
})

test('a busy sheet, a busy service and a bad sheet each get a clear message', () => {
  const env = setup();
  env.state.lockBusy = true;
  env.coach.runCoach();
  assert.match(env.ui.alerts.at(-1), /Another Coach action is running/);
  env.state.lockBusy = false;
  env.state.fetch = service({ 'POST /v1/runs': () => json(409, { detail: 'Another Coach run is in progress on this sheet (started by prep@example.com).' }) }).fetch;
  env.coach.runCoach();
  assert.match(env.ui.alerts.at(-1), /started by prep@example.com/);
  env.state.fetch = service({ 'POST /v1/runs': () => json(422, { detail: { message: 'Fix the Criteria tab', problems: ['Criteria row 2: threshold must be between 0 and 1'] } }) }).fetch;
  env.coach.runCoach();
  assert.match(env.ui.alerts.at(-1), /Fix the Criteria tab:\n- Criteria row 2/);
  env.state.fetch = service({ 'POST /v1/runs': () => json(500, { detail: 'boom', request_id: 'abc123' }) }).fetch;
  env.coach.runCoach();
  assert.match(env.ui.alerts.at(-1), /request abc123/);
});

test('a run is never retried blindly, but completion is, even when the connection drops', () => {
  const env = setup();
  let runs = 0;
  let completes = 0;
  env.state.fetch = service({
    'POST /v1/runs': () => { runs += 1; return json(503, { detail: 'starting' }); },
  }).fetch;
  env.coach.runCoach();
  assert.equal(runs, 1);
  env.state.fetch = service({
    'POST /v1/runs': () => json(200, runResponse()),
    'POST /v1/runs/*': () => { completes += 1; return completes < 3 ? json(503, {}) : json(200, {}); },
  }).fetch;
  env.coach.runCoach();
  assert.equal(completes, 3);

  // A dropped connection throws rather than answering. Completion retries it;
  // the run itself is still sent once.
  completes = 0;
  env.state.fetch = service({
    'POST /v1/runs': () => json(200, runResponse()),
    'POST /v1/runs/*': () => {
      completes += 1;
      if (completes < 2) throw new Error('Address unavailable: ' + SERVICE + '/v1/runs/x/complete');
      return json(200, {});
    },
  }).fetch;
  env.coach.runCoach();
  assert.equal(completes, 2);
  runs = 0;
  env.state.fetch = service({
    'POST /v1/runs': () => { runs += 1; throw new Error('Address unavailable: ' + SERVICE + '/v1/runs'); },
  }).fetch;
  assert.throws(() => env.coach.runCoach(), /Address unavailable/);
  assert.equal(runs, 1);
});

test('sheet key fallback when no token, and on a 401 for the token', () => {
  const env = setup({ token: null, email: 'prep@example.com' });
  env.props.script.setProperty('COACH_SHEET_KEY', 'fcc_k_abc_secret');
  const svc = service({ 'GET /v1/me': () => json(200, { role: 'preparer', verified: false }) });
  env.state.fetch = svc.fetch;
  env.coach.refreshRole();
  assert.equal(svc.calls[0].headers['X-Coach-Sheet-Key'], 'fcc_k_abc_secret');
  assert.equal(svc.calls[0].headers['X-Coach-User'], 'prep@example.com');
  assert.equal(env.props.user.getProperty('COACH_ROLE'), 'preparer');
  assert.match(env.ui.alerts.at(-1), /You are a preparer[\s\S]*locked to reviewers/);

  const both = setup();
  both.props.script.setProperty('COACH_SHEET_KEY', 'fcc_k_abc_secret');
  const svc2 = service({ 'GET /v1/me': (_, params) => params.headers.Authorization ? json(401, { detail: 'identity tokens are not enabled' }) : json(200, { role: 'reviewer', verified: false }) });
  both.state.fetch = svc2.fetch;
  both.coach.refreshRole();
  assert.equal(svc2.calls.length, 2);
  assert.equal(both.props.user.getProperty('COACH_ROLE'), 'reviewer');
  assert.match(both.ui.alerts.at(-1), /You are a reviewer[\s\S]*why you can edit the Criteria and Settings tabs/);

  const none = setup({ token: null });
  assert.throws(() => none.coach.refreshRole(), /No credentials/);
});

test('the menu shows reviewer actions only to a cached reviewer', () => {
  const env = setup();
  env.coach.onOpen();
  const labels = m => m.items.filter(i => i.label).map(i => i.label);
  assert.deepEqual(labels(env.ui.menus[0]), ['Run Coach', 'Submit overrides']);
  env.props.user.setProperty('COACH_ROLE', 'reviewer');
  env.coach.onOpen();
  assert.deepEqual(labels(env.ui.menus[1]), ['Run Coach', 'Submit overrides', 'Run criteria health', 'Sync criteria status']);
  const admin = env.ui.menus[1].items.find(i => i.submenu).submenu;
  assert.deepEqual(labels(admin), ['Set up sheet', 'Configure service', 'Show connection info', 'Refresh my role']);
});

test('editing a pass condition marks the row uncalibrated and records who edited', () => {
  const env = setup({ email: 'rev@example.com' });
  const criteria = env.ss.getSheetByName('Criteria');
  const row = criteria.rowWhere('ID', 'C1');
  const col = env.constants.CRITERIA_COLUMNS.indexOf('Passes when') + 1;
  env.coach.onEdit({ range: criteria.getRange(row, col), user: { getEmail: () => 'rev@example.com' } });
  assert.equal(criteria.value(row, 'Calibration status'), 'Uncalibrated (re-check needed)');
  const hints = JSON.parse(env.props.document.getProperty('COACH_CRITERIA_EDIT_HINTS'));
  assert.equal(hints.C1.by, 'rev@example.com');

  const feedbackCol = env.constants.CRITERIA_COLUMNS.indexOf('Feedback when failed') + 1;
  const arith = criteria.rowWhere('ID', 'ARITH');
  env.coach.onEdit({ range: criteria.getRange(arith, feedbackCol) });
  assert.equal(criteria.value(arith, 'Calibration status'), 'Deterministic');
  assert.ok(JSON.parse(env.props.document.getProperty('COACH_CRITERIA_EDIT_HINTS')).ARITH);

  const reviewed = env.constants.CRITERIA_COLUMNS.indexOf('Last reviewed') + 1;
  env.props.document.deleteProperty('COACH_CRITERIA_EDIT_HINTS');
  env.coach.onEdit({ range: criteria.getRange(row, reviewed) });
  assert.equal(env.props.document.getProperty('COACH_CRITERIA_EDIT_HINTS'), null);
  env.coach.onEdit({ range: env.ss.getSheetByName('Flux').getRange(2, 10) });
});

test('overrides are sent and the result explained', () => {
  const env = setup();
  const flux = env.ss.getSheetByName('Flux');
  flux.setValueAt(2, 'Override', 'Flag was wrong: C1');
  flux.setValueAt(2, 'Override note', 'Specific enough.');
  const svc = service({ 'POST /v1/overrides': () => json(200, { recorded: 1, already_recorded: 0, invalid: [], attributed_to: null }) });
  env.state.fetch = svc.fetch;
  env.coach.submitOverrides();
  assert.deepEqual(svc.calls[0].payload.overrides, [{ line_id: 'FHD-US|2026-09|MoM|5000', value: 'Flag was wrong: C1', note: 'Specific enough.' }]);
  assert.match(env.ss.toasts.at(-1), /without attribution/);
});

test('criteria health sends resolutions, applies them, and rebuilds the views', () => {
  const env = setup();
  const C = env.constants;
  env.ss.tab('Criteria Health', C.CRITERIA_HEALTH_COLUMNS, [
    { 'Criterion ID': 'C1', 'Signal': 'stale', 'Resolution': 'keep', 'Prompt ID': 41 },
    { 'Criterion ID': 'C2', 'Signal': 'never_fails', 'Resolution': 'retire', 'Prompt ID': 42 },
    { 'Criterion ID': 'RESTATE', 'Signal': 'stale', 'Resolution': '', 'Prompt ID': 43 },
    { 'Criterion ID': 'C1', 'Signal': 'flag_wrong', 'Resolution': 'keep', 'Resolved at': '2026-10-01', 'Prompt ID': 40 },
  ]);
  const svc = service({
    'POST /v1/health': () => json(200, {
      close_window: '2026-09',
      prompts: [{ id: 50, criterion_id: 'C5', criterion: 'Sizes each driver', signal: 'stale', suggestion: 'Nobody is maintaining it', evidence: { text: 'last reviewed 2026-06-12' }, close_window: '2026-09' }],
      second_attempts: { closes: ['2026-07', '2026-08'], rows: [{ criterion_id: 'C1', criterion: 'Names a specific driver', counts: { '2026-07': 11, '2026-08': 4 }, rates: { '2026-07': 0.5, '2026-08': null } }], totals: { '2026-07': 15, '2026-08': 9 }, falling: true, caveat: 'Counts revisions, not learning.' },
      history: [{ criterion_id: 'C1', version: 2, changed_at: '2026-10-07T00:00:00', changed_by: 'rev@example.com', fields_changed: 'Feedback when failed', before: 'a', after: 'b', calibration_impact: 'none' }],
      log: [{ ts: '2026-10-07T00:00:00', close_period: '2026-09', line_id: 'L', run_id: 'r', attempt_number: 1, commentary_hash: 'h', criterion_id: 'C1', result: 'fail', jev_probability: 0.2, criterion_version: 2, question_builder_version: 'qb-1.1.0', model_version: 'm', calibration_status: 'Calibrated' }],
      resolutions_recorded: 2,
    }),
  });
  env.state.fetch = svc.fetch;
  env.coach.runCriteriaHealth();
  assert.deepEqual(svc.calls[0].payload.resolutions, [{ id: 41, resolution: 'keep' }, { id: 42, resolution: 'retire' }]);
  assert.equal(svc.calls[0].payload.include_log, true);
  const criteria = env.ss.getSheetByName('Criteria');
  const today = new Date();
  const stamp = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  assert.equal(criteria.value(criteria.rowWhere('ID', 'C1'), 'Last reviewed'), stamp);
  assert.equal(criteria.value(criteria.rowWhere('ID', 'C2'), 'Active (Y/N)'), 'N');
  const health = env.ss.getSheetByName('Criteria Health').rows();
  assert.equal(health.length, 1);
  assert.equal(health[0]['Criterion ID'], 'C5');
  assert.equal(health[0]['Prompt ID'], 50);
  const second = env.ss.getSheetByName('Second Attempts');
  assert.deepEqual(second.header(), ['Criterion ID', 'Criterion', '2026-07', '2026-08']);
  assert.equal(second.rows()[0]['Criterion ID'], 'Total');
  assert.equal(second.rows()[0]['2026-08'], 9);
  assert.equal(second.rows()[1]['2026-07'], '11 (50%)');
  assert.equal(second.rows()[1]['2026-08'], 4);
  assert.equal(second.rows()[2]['Criterion'], 'Counts revisions, not learning.');
  assert.equal(second.hidden, false);
  assert.equal(env.ss.getActiveSheet(), second, 'the reviewer lands on the view');
  assert.equal(env.ss.getSheetByName('Criteria Health').hidden, false, 'shown: it has a prompt to resolve');
  const summary = env.ui.alerts.at(-1);
  assert.match(summary, /2026-07: {2}15\n {4}2026-08: {2}9\nFewer every close\./);
  assert.match(summary, /1 criterion to look at in 2026-09/);
  assert.match(env.coach.healthSummary_(null, 2, '2026-09', 0), /2 criteria to look at in 2026-09/);
  assert.equal(second.widths[2], 260, 'criterion names readable');
  assert.equal(second.widths[3], 110);
  const healthTab = env.ss.getSheetByName('Criteria Health');
  assert.equal(healthTab.widths[C.CRITERIA_HEALTH_COLUMNS.indexOf('What it suggests') + 1], 360);
  assert.match(summary, /2 resolution\(s\) recorded/);
  assert.match(summary, /Counts revisions, not learning\.$/);
  assert.equal(env.ss.getSheetByName('Log').rows()[0]['Jev probability'], 0.2);
  assert.equal(env.ss.getSheetByName('Criteria History').rows()[0]['Changed by'], 'rev@example.com');
});

test('set up sheet builds every tab, loads defaults, and protects what it should', () => {
  const env = loadCoach({ token: 'a.' + Buffer.from('{"aud":"x","email":"rev@example.com"}').toString('base64url') + '.c', email: 'owner@example.com' });
  env.props.script.setProperty('COACH_SERVICE_URL', SERVICE);
  const C = env.constants;
  env.state.fetch = service({
    'GET /v1/defaults': () => json(200, {
      criteria: [{ 'ID': 'C1', 'Criterion': 'Names a specific driver', 'Passes when': 'p', 'Feedback when failed': 'f', 'Threshold': '0.91', 'Calibration status': 'Calibrated', 'Active (Y/N)': 'Y' }],
      settings: [{ Setting: 'Materiality ($)', Value: 25000, Notes: 'n' }],
    }),
    'GET /v1/team': () => json(200, { reviewers: ['rev@example.com'], preparers: ['prep@example.com'] }),
  }).fetch;
  env.coach.setUpSheet();
  assert.deepEqual(env.ss.getSheets().map(s => s.name), C.TAB_ORDER);
  assert.deepEqual(env.ss.getSheetByName('Flux').header(), C.FLUX_COLUMNS);
  assert.equal(env.ss.getSheetByName('Criteria').rows()[0].ID, 'C1');
  assert.equal(env.ss.getSheetByName('Settings').rows()[0].Value, 25000);
  for (const name of ['Log', 'Criteria History', 'Criteria Health', 'Second Attempts']) {
    assert.equal(env.ss.getSheetByName(name).hidden, true, name);
    const p = env.ss.protections.find(x => x.sheet.name === name && x.type === 'SHEET');
    assert.equal(p.warningOnly, true, name);
  }
  for (const name of ['Criteria', 'Settings']) {
    const p = env.ss.protections.find(x => x.sheet.name === name && x.type === 'SHEET');
    assert.equal(p.warningOnly, false);
    assert.deepEqual(p.editors.sort(), ['owner@example.com', 'rev@example.com'].sort());
  }
  const rangeProtections = env.ss.protections.filter(x => x.type === 'RANGE').map(p => p.description);
  assert.deepEqual(rangeProtections, ['Status is written by Coach', 'Failed criteria is written by Coach', 'Feedback is written by Coach']);
  const flux = env.ss.getSheetByName('Flux');
  const overrideRule = flux.validation[C.FLUX_COLUMNS.indexOf('Override') + 1];
  assert.ok(overrideRule.values.includes('Missed an issue: not covered'));
  assert.equal(overrideRule.allowInvalid, false);
  assert.match(env.ui.alerts.at(-1), /locked to reviewers \(the lock on their tabs\)\. They are editable by: rev@example.com/);

  // Run again, as after importing onboarding files: the imported rows stay, and
  // protections are replaced rather than stacked.
  env.ss.getSheetByName('Criteria').getRange(2, 1).setValue('C9');
  env.coach.setUpSheet();
  assert.equal(env.ss.getSheetByName('Criteria').rows()[0].ID, 'C9');
  assert.equal(env.ss.protections.filter(x => x.type === 'RANGE').length, 3);
  assert.equal(env.ss.protections.filter(x => x.type === 'SHEET').length, 6);
});

test('connection info shows the token audience for GOOGLE_AUDIENCES', () => {
  const env = setup();
  env.state.fetch = service({
    'GET /healthz': () => json(200, { store: 'postgres', jev: { mode: 'live', request_model: 'typesafe/jev-1.13-20260917', pinned_version: 'jev-1.13.0' }, auth: ['google'] }),
    'GET /v1/me': () => json(200, { role: 'reviewer', verified: true }),
  }).fetch;
  env.coach.showConnectionInfo();
  const text = env.ui.alerts.at(-1);
  assert.match(text, /audience \(set GOOGLE_AUDIENCES to this\): client-123.apps.googleusercontent.com/);
  assert.match(text, /Your role: reviewer \(verified\)/);
  assert.match(text, /pinned jev-1.13.0/);
});

test('configure service insists on https', () => {
  const env = setup();
  env.ui.prompts.push('http://insecure.example.com');
  env.coach.configureService();
  assert.match(env.ui.alerts.at(-1), /must start with https/);
  env.ui.prompts.push('https://coach.example.org/', 'fcc_k_x_y');
  env.coach.configureService();
  assert.equal(env.props.script.getProperty('COACH_SERVICE_URL'), 'https://coach.example.org');
  assert.equal(env.props.script.getProperty('COACH_SHEET_KEY'), 'fcc_k_x_y');
});

test('number cells: blank is not given, a broken cell is an error that names itself', () => {
  const n = setup().coach.numberOrNull_;
  assert.equal(n(''), null);
  assert.equal(n(null), null);
  assert.equal(n('n/a'), null);
  assert.equal(n(409000), 409000);
  assert.equal(n('$7,598,000'), 7598000);
  assert.equal(n('(1,200)'), -1200);
  assert.equal(n('4.5%'), 0.045);
  assert.equal(n('(4.5%)'), -0.045);
  for (const bad of ['#DIV/0!', '#REF!', '#N/A', '30,00O', 'Infinity', '1e999', 'abc', NaN, Infinity]) {
    assert.throws(() => n(bad), /is (not a (finite )?number|a spreadsheet error): /, String(bad));
  }
  assert.equal(setup().coach.cellA1_(7, 7), 'G7');
  assert.equal(setup().coach.cellA1_(2, 27), 'AA2');
});

test('a row with a broken number cell is not sent; its Status names the cell', () => {
  const env = setup();
  const flux = env.ss.getSheetByName('Flux');
  const ar = flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|1200');
  flux.setValueAt(ar, 'Prior', '30,00O');
  flux.setValueAt(ar, 'Variance (%)', '#DIV/0!');
  const reply = runResponse();
  reply.rejected = [];
  const svc = service({ 'POST /v1/runs': () => json(200, reply), 'POST /v1/runs/*': () => json(200, {}) });
  env.state.fetch = svc.fetch;
  env.coach.runCoach();
  const run = svc.calls.find(c => c.key === 'POST /v1/runs');
  assert.deepEqual(run.payload.lines.map(l => l.line_id).sort(), ['FHD-US|2026-09|MoM|5000', 'FHD-US|2026-09|MoM|6400'],
    'the broken row stays home');
  assert.equal(flux.value(ar, 'Status'),
    'Not graded: Prior (F4) is not a number: 30,00O; Variance (%) (I4) is a spreadsheet error: #DIV/0!');
  assert.equal(flux.styleAt(ar, 'Status').bg, env.constants.STATUS_STYLES.error.background);
  assert.equal(flux.value(flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|5000'), 'Status'), 'Needs revision',
    'the other rows are graded as usual');
  assert.match(env.ss.toasts.at(-1), /1 not sent \(see Status for the cell to fix\)/);
  const done = svc.calls.find(c => c.key.endsWith('/complete'));
  assert.deepEqual(done.payload.written.sort(), ['FHD-US|2026-09|MoM|5000', 'FHD-US|2026-09|MoM|6400']);
});

test('when every row has a broken cell nothing is sent and the alert lists each cell', () => {
  const env = setup();
  const flux = env.ss.getSheetByName('Flux');
  [2, 3, 4].forEach(row => flux.getRange(row, env.constants.FLUX_COLUMNS.indexOf('Current') + 1).setValue('#REF!'));
  const svc = service({ 'POST /v1/runs': () => json(200, runResponse()) });
  env.state.fetch = svc.fetch;
  env.coach.runCoach();
  assert.equal(svc.calls.length, 0, 'no run was started');
  assert.match(env.ui.alerts.at(-1), /no line could be checked\.\n- Current \(G2\) is a spreadsheet error: #REF!/);
  [2, 3, 4].forEach(row => assert.match(String(flux.getRange(row, env.constants.FLUX_COLUMNS.indexOf('Status') + 1).getValue()),
    /^Not graded: Current \(G\d\) is a spreadsheet error: #REF!$/));
});

test('a Line ID on two rows keeps both rows home, and both rows say so', () => {
  const env = setup();
  const flux = env.ss.getSheetByName('Flux');
  const C = env.constants;
  flux.getRange(5, 1, 1, C.FLUX_COLUMNS.length).setValues([C.FLUX_COLUMNS.map(h => ({
    'Line ID': 'FHD-US|2026-09|MoM|5000', 'Entity': 'FHD-US', 'Period': '2026-09', 'Comparison basis': 'MoM',
    'Account': '5000 Cloud hosting', 'Prior': 1000, 'Current': 50000, 'Variance ($)': 49000, 'Variance (%)': 49,
    'Commentary': 'Hosting rose because the two rows share one ID.',
  })[h] ?? '')]);
  const reply = runResponse();
  reply.results = reply.results.filter(r => r.line_id !== 'FHD-US|2026-09|MoM|5000');
  const svc = service({ 'POST /v1/runs': () => json(200, reply), 'POST /v1/runs/*': () => json(200, {}) });
  env.state.fetch = svc.fetch;
  env.coach.runCoach();
  const run = svc.calls.find(c => c.key === 'POST /v1/runs');
  assert.deepEqual(run.payload.lines.map(l => l.line_id).sort(), ['FHD-US|2026-09|MoM|1200', 'FHD-US|2026-09|MoM|6400']);
  const message = 'Not graded: Line ID appears on rows 2, 5: add a slice (entity|period|basis|account|slice) so each row has its own';
  assert.equal(flux.value(2, 'Status'), message);
  assert.equal(flux.value(5, 'Status'), message);
  assert.equal(flux.styleAt(5, 'Status').bg, C.STATUS_STYLES.error.background);
  assert.match(env.ss.toasts.at(-1), /2 not sent/);
});

test('a Line ID the service rejects is marked on every row that carries it', () => {
  const env = setup();
  const flux = env.ss.getSheetByName('Flux');
  const table = env.coach.readTable_(flux, env.constants.FLUX_COLUMNS);
  flux.setValueAt(3, 'Line ID', 'FHD-US|2026-09|MoM|5000'); // the sheet changed under us: two rows, one ID
  const current = env.coach.readTable_(flux, env.constants.FLUX_COLUMNS);
  env.coach.writeResults_(flux, current, [], {}, [{ line_id: 'FHD-US|2026-09|MoM|5000', error: 'Line ID appears 2 times in this run (rows 2, 3); none was graded' }]);
  assert.equal(flux.value(2, 'Status'), 'Not graded: Line ID appears 2 times in this run (rows 2, 3); none was graded');
  assert.equal(flux.value(3, 'Status'), 'Not graded: Line ID appears 2 times in this run (rows 2, 3); none was graded');
  assert.equal(flux.value(4, 'Status'), '', 'an unrelated row is untouched');
  assert.ok(table.rows.length === 3);
});

test('RAN marker', () => {
  console.log('RAN: apps script unit tests');
});

test('the Status cell shows the attempt from the second one on', () => {
  const text = r => setup().coach.statusText_(r);
  assert.equal(text({ status: 'passed', status_label: 'Passed', attempt_number: 1 }), 'Passed');
  assert.equal(text({ status: 'passed', status_label: 'Passed', attempt_number: 2 }), 'Passed (attempt 2)');
  assert.equal(text({ status: 'needs_revision', status_label: 'Needs revision', attempt_number: 3 }),
    'Needs revision (attempt 3)');
  assert.equal(text({ status: 'error', status_label: 'Not graded', attempt_number: 0 }), 'Not graded');
  const C = setup().constants;
  assert.equal(C.STATUS_STYLES.needs_revision.background, '#fce8e6', 'light red');
  assert.equal(C.STATUS_STYLES.passed.background, '#ffffff', 'no fill on a passed line');
});

test('saving a criterion says why this person could edit it', () => {
  const env = setup();
  env.props.user.setProperty('COACH_ROLE', 'reviewer');
  const criteria = env.ss.getSheetByName('Criteria');
  const row = criteria.rowWhere('ID', 'C1');
  const col = env.constants.CRITERIA_COLUMNS.indexOf('Feedback when failed') + 1;
  env.coach.onEdit({ range: criteria.getRange(row, col), user: { getEmail: () => 'rev@example.com' } });
  assert.match(env.ss.toasts.at(-1), /^C1 saved: Coach uses the new wording on the next run\. You can edit this tab because you are a reviewer/);
});
