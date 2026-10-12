// End to end: the real Code.gs, driving the real Coach service over HTTP.
//
// Started by backend/tests/test_apps_script_e2e.py, which runs the service
// (replay Jev, memory store, Google auth against a local JWKS) and hands this
// script its URL and two signed identity tokens. UrlFetchApp is backed by curl,
// so every request is real HTTP with real token verification.
//
// The scenario is one reviewer edit changing what one preparer sees, in five
// steps, plus the edges around it. The close is pasted into the Flux tab without
// Line IDs, the way a team's own close goes in, and the first run assigns them.
//   1. the reviewer rewrites C1's feedback in their own words;
//   2. the preparer runs Coach and the hosting line fails C1;
//   3. the reviewer's own words appear beside it, verbatim;
//   4. the preparer rewrites the commentary;
//   5. the rerun passes, as attempt 2.
// Then: a mid-run edit, a preparer refused a reviewer action, overrides, and
// Criteria Health with the second-attempts view.

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import test from 'node:test';
import { loadCoach } from './gas_mocks.mjs';

const URL_BASE = process.env.COACH_TEST_URL;
const TOKENS = { reviewer: process.env.COACH_TEST_REVIEWER_TOKEN, preparer: process.env.COACH_TEST_PREPARER_TOKEN };
const LINE = 'FHD-US|2026-09|MoM|5000';
const NEW_FEEDBACK = 'Who or what, specifically? Name the customer, vendor, contract or hire so I can check it.';
const REVISION = process.env.COACH_TEST_REVISION;
const CLOSE_FILE = process.env.COACH_TEST_CLOSE_FILE;

function curlFetch(hook) {
  return (url, params) => {
    const args = ['-sS', '-X', (params.method || 'get').toUpperCase(), '-w', '\n%{http_code}'];
    for (const [k, v] of Object.entries(params.headers || {})) args.push('-H', `${k}: ${v}`);
    if (params.payload !== undefined) args.push('-H', 'Content-Type: application/json', '--data-binary', '@-');
    args.push(url);
    const out = execFileSync('curl', args, { input: params.payload || '', maxBuffer: 64 * 1024 * 1024 }).toString();
    const cut = out.lastIndexOf('\n');
    const reply = { code: Number(out.slice(cut + 1)), text: out.slice(0, cut) };
    if (hook) hook(url, params, reply);
    return reply;
  };
}

test('one reviewer edit reaches one preparer, end to end, through the real script and service', () => {
  assert.ok(URL_BASE && TOKENS.reviewer && TOKENS.preparer && REVISION && CLOSE_FILE, 'run via test_apps_script_e2e.py');
  const env = loadCoach({ token: TOKENS.reviewer, email: 'reviewer@example.com' });
  const { coach, ss, ui, props, state } = env;
  let hook = null;
  state.fetch = curlFetch((url, params, reply) => hook && hook(url, params, reply));
  props.script.setProperty('COACH_SERVICE_URL', URL_BASE);

  // Setup, as the reviewer: tabs, defaults from the service, protections.
  coach.refreshRole();
  assert.equal(props.user.getProperty('COACH_ROLE'), 'reviewer');
  coach.setUpSheet();
  assert.match(ui.alerts.at(-1), /editable by: reviewer@example.com/);
  const criteria = ss.getSheetByName('Criteria');
  const c1 = criteria.rowWhere('ID', 'C1');
  assert.equal(criteria.value(c1, 'Calibration status'), 'Uncalibrated', 'the starter criteria arrive uncalibrated');
  // This team has calibrated (the service holds its calibrations): syncing shows it, as the README says to.
  coach.syncCriteriaStatus();
  assert.equal(criteria.value(c1, 'Calibration status'), 'Calibrated');
  // The close goes in the way a team's does: pasted into the Flux tab, Line ID left blank.
  const close = JSON.parse(fs.readFileSync(CLOSE_FILE, 'utf8')).map(r => ({ ...r, 'Line ID': '' }));
  const flux = ss.tab('Flux', env.constants.FLUX_COLUMNS, close);

  // Step 1: the reviewer rewrites C1's feedback. The pass condition is untouched.
  criteria.setValueAt(c1, 'Feedback when failed', NEW_FEEDBACK);
  const col = env.constants.CRITERIA_COLUMNS.indexOf('Feedback when failed') + 1;
  coach.onEdit({ range: criteria.getRange(c1, col), user: { getEmail: () => 'reviewer@example.com' } });
  assert.equal(criteria.value(c1, 'Calibration status'), 'Calibrated', 'feedback edits do not touch calibration');

  // Step 2: the preparer runs Coach; the line fails C1.
  props.user.setProperty('COACH_ROLE', 'preparer');
  state.token = TOKENS.preparer;
  state.email = 'preparer@example.com';
  coach.runCoach();
  const row = flux.rowWhere('Line ID', LINE);
  assert.ok(row > 1, 'the first run assigned Line IDs');
  assert.equal(flux.value(row, 'Status'), 'Needs revision');
  assert.equal(flux.value(row, 'Failed criteria'), 'C1 (likely)');

  // Step 3: the reviewer's own feedback appears beside it, verbatim.
  assert.equal(flux.value(row, 'Feedback'), 'C1: ' + NEW_FEEDBACK);
  const history = ss.getSheetByName('Criteria History').rows();
  const c1Change = history.find(h => h['Criterion ID'] === 'C1' && h['Version'] === 1);
  assert.ok(c1Change, 'first run records version 1 of each criterion');
  console.log('RAN: e2e steps 1-3, feedback verbatim: ' + flux.value(row, 'Feedback'));

  // Step 4: the preparer rewrites the commentary.  Step 5: rerun, and it passes.
  flux.setValueAt(row, 'Commentary', REVISION);
  coach.runCoach();
  assert.equal(flux.value(row, 'Status'), 'Passed (attempt 2)');
  assert.equal(flux.value(row, 'Feedback'), '');
  assert.match(flux.styleAt(row, 'Status').note, /Attempt 2/);
  console.log('RAN: e2e steps 4-5, status: ' + flux.value(row, 'Status'));

  // A line edited while the run is in flight gets no stale feedback.
  const other = flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|6210');
  hook = (url, params) => {
    if (url.endsWith('/v1/runs') && params.method === 'post') {
      flux.setValueAt(other, 'Commentary', 'Edited while the run was in flight.');
    }
  };
  let completeBody = null;
  const previous = state.fetch;
  state.fetch = (url, params) => {
    if (url.endsWith('/complete')) completeBody = JSON.parse(params.payload);
    return previous(url, params);
  };
  coach.runCoach();
  hook = null;
  state.fetch = previous;
  assert.equal(flux.value(other, 'Status'), env.constants.STALE_LABEL);
  assert.deepEqual(completeBody.stale, ['FHD-US|2026-09|MoM|6210']);
  console.log('RAN: e2e mid-run edit marked stale and reported to the service');

  // The service, not the menu, enforces roles.
  coach.runCriteriaHealth();
  assert.match(ui.alerts.at(-1), /Run criteria health: this action is for reviewers/);

  // Overrides, as the reviewer.
  state.token = TOKENS.reviewer;
  state.email = 'reviewer@example.com';
  props.user.setProperty('COACH_ROLE', 'reviewer');
  flux.setValueAt(flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|7100'), 'Override', 'Missed an issue: not covered');
  flux.setValueAt(flux.rowWhere('Line ID', 'FHD-US|2026-09|MoM|7100'), 'Override note', 'Is this hedged?');
  coach.submitOverrides();
  assert.match(ss.toasts.at(-1), /1 recorded/);
  coach.submitOverrides();
  assert.match(ss.toasts.at(-1), /0 recorded, 1 already on file/);

  // Criteria Health and the second-attempts view, as the reviewer.
  coach.runCriteriaHealth();
  const secondAttempts = ss.getSheetByName('Second Attempts');
  assert.equal(ss.getActiveSheet(), secondAttempts, 'criteria health lands on the view');
  assert.equal(secondAttempts.hidden, false);
  assert.match(ui.alerts.at(-1), /Lines that needed a second attempt, by close:\n {4}2026-09: {2}1/);
  assert.deepEqual(secondAttempts.header().slice(0, 2), ['Criterion ID', 'Criterion']);
  const c1Row = secondAttempts.rows().find(r => r['Criterion ID'] === 'C1');
  assert.equal(String(c1Row['2026-09']).split(' ')[0], '1', 'the hosting line is one C1 second attempt');
  const log = ss.getSheetByName('Log').rows();
  assert.ok(log.length > 50, 'the Log view is filled');
  assert.ok(log.every(r => r['Question-builder version'] === '' || /^qb-/.test(r['Question-builder version'])));
  const healthRows = ss.getSheetByName('Criteria Health').rows();
  console.log('RAN: e2e criteria health with ' + healthRows.length + ' prompt(s); second attempts and log written');
  console.log('RAN: e2e complete');
});
