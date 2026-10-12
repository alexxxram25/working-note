// In-memory stand-ins for the Apps Script services Code.gs uses, and a loader
// that runs the real Code.gs (unmodified) inside a vm context against them.
//
// The mocks model what the script relies on: cell values, backgrounds, font
// colors and notes; tabs and their order; protections; data validation;
// properties; the document lock; the UI's alerts, prompts and toasts. Styling
// calls that only change appearance are accepted and recorded, nothing more.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const CODE_PATH = path.resolve(HERE, '..', 'Code.gs');

class Cell {
  constructor() {
    this.v = '';
    this.bg = '#ffffff';
    this.fc = '#000000';
    this.note = '';
  }
}

export class MockRange {
  constructor(sheet, row, col, numRows = 1, numCols = 1) {
    if (row < 1 || col < 1 || numRows < 1 || numCols < 1) {
      throw new Error(`invalid range ${row},${col},${numRows},${numCols} on ${sheet.name}`);
    }
    Object.assign(this, { sheet, row, col, numRows, numCols });
  }
  _grid(fn) {
    const out = [];
    for (let r = 0; r < this.numRows; r++) {
      const line = [];
      for (let c = 0; c < this.numCols; c++) line.push(fn(this.sheet.cell(this.row + r, this.col + c)));
      out.push(line);
    }
    return out;
  }
  _set(values, fn) {
    if (!Array.isArray(values) || values.length !== this.numRows || values.some(l => l.length !== this.numCols)) {
      throw new Error(`dimension mismatch: range is ${this.numRows}x${this.numCols}`);
    }
    values.forEach((line, r) => line.forEach((v, c) => fn(this.sheet.cell(this.row + r, this.col + c), v)));
    return this;
  }
  getValues() { return this._grid(c => c.v); }
  getDisplayValues() { return this._grid(c => String(c.v)); }
  setValues(values) { return this._set(values, (c, v) => { c.v = v === null || v === undefined ? '' : v; }); }
  getValue() { return this.sheet.cell(this.row, this.col).v; }
  setValue(v) { this.sheet.cell(this.row, this.col).v = v === null || v === undefined ? '' : v; return this; }
  getBackgrounds() { return this._grid(c => c.bg); }
  setBackgrounds(values) { return this._set(values, (c, v) => { c.bg = v; }); }
  setBackground(v) { this._grid(c => { c.bg = v === null ? '#ffffff' : v; }); return this; }
  getFontColors() { return this._grid(c => c.fc); }
  setFontColors(values) { return this._set(values, (c, v) => { c.fc = v; }); }
  setFontColor(v) { this._grid(c => { c.fc = v; }); return this; }
  getNotes() { return this._grid(c => c.note); }
  setNotes(values) { return this._set(values, (c, v) => { c.note = v || ''; }); }
  setNote(v) { this._grid(c => { c.note = v || ''; }); return this; }
  clearNote() { return this.setNote(''); }
  clearContent() { this._grid(c => { c.v = ''; }); return this; }
  setNumberFormat(f) { this.sheet.formats.push([this.a1(), f]); return this; }
  setWrap() { return this; }
  setVerticalAlignment() { return this; }
  setHorizontalAlignment() { return this; }
  setFontWeight() { return this; }
  setDataValidation(rule) {
    for (let r = 0; r < this.numRows; r++) this.sheet.validation[this.col] = rule;
    return this;
  }
  protect() { return this.sheet.ss._protection('RANGE', this.sheet, this); }
  getRow() { return this.row; }
  getColumn() { return this.col; }
  getNumRows() { return this.numRows; }
  getNumColumns() { return this.numCols; }
  getSheet() { return this.sheet; }
  a1() { return `${this.sheet.name}!R${this.row}C${this.col}:${this.numRows}x${this.numCols}`; }
}

export class MockSheet {
  constructor(ss, name) {
    this.ss = ss;
    this.name = name;
    this.cells = new Map();
    this.hidden = false;
    this.hiddenColumns = new Set();
    this.frozen = 0;
    this.widths = {};
    this.formats = [];
    this.validation = {};
  }
  cell(r, c) {
    const key = `${r},${c}`;
    if (!this.cells.has(key)) this.cells.set(key, new Cell());
    return this.cells.get(key);
  }
  getName() { return this.name; }
  getRange(row, col, numRows = 1, numCols = 1) { return new MockRange(this, row, col, numRows, numCols); }
  getLastRow() {
    let last = 0;
    for (const [key, cell] of this.cells) if (cell.v !== '' && cell.v !== null) last = Math.max(last, Number(key.split(',')[0]));
    return last;
  }
  getLastColumn() {
    let last = 0;
    for (const [key, cell] of this.cells) if (cell.v !== '' && cell.v !== null) last = Math.max(last, Number(key.split(',')[1]));
    return last;
  }
  getMaxRows() { return Math.max(200, this.getLastRow()); }
  getMaxColumns() { return Math.max(26, this.getLastColumn()); }
  setFrozenRows(n) { this.frozen = n; return this; }
  setColumnWidth(col, w) { this.widths[col] = w; return this; }
  hideSheet() { this.hidden = true; return this; }
  showSheet() { this.hidden = false; return this; }
  isSheetHidden() { return this.hidden; }
  activate() { this.ss.active = this; return this; }
  hideColumns(col) { this.hiddenColumns.add(col); return this; }
  protect() { return this.ss._protection('SHEET', this, null); }
  getProtections(type) { return this.ss.protections.filter(p => p.type === type && p.sheet === this); }
  // Test helpers ------------------------------------------------------------
  header() { return this.getRange(1, 1, 1, Math.max(this.getLastColumn(), 1)).getValues()[0]; }
  rows() {
    const header = this.header();
    const last = this.getLastRow();
    if (last < 2) return [];
    return this.getRange(2, 1, last - 1, header.length).getValues()
      .map(line => Object.fromEntries(header.map((h, i) => [h, line[i]])));
  }
  rowWhere(column, value) {
    const header = this.header();
    const col = header.indexOf(column) + 1;
    for (let r = 2; r <= this.getLastRow(); r++) if (this.cell(r, col).v === value) return r;
    return -1;
  }
  value(row, column) { return this.cell(row, this.header().indexOf(column) + 1).v; }
  setValueAt(row, column, v) { this.cell(row, this.header().indexOf(column) + 1).v = v; }
  styleAt(row, column) { const c = this.cell(row, this.header().indexOf(column) + 1); return { bg: c.bg, fc: c.fc, note: c.note }; }
}

class MockProtection {
  constructor(ss, type, sheet, range) {
    Object.assign(this, { ss, type, sheet, range, description: '', warningOnly: false, editors: ['owner@example.com'] });
  }
  setDescription(d) { this.description = d; return this; }
  getDescription() { return this.description; }
  setWarningOnly(b) { this.warningOnly = b; return this; }
  isWarningOnly() { return this.warningOnly; }
  canDomainEdit() { return false; }
  setDomainEdit() { return this; }
  getEditors() { return this.editors.map(e => ({ getEmail: () => e })); }
  addEditors(list) { list.forEach(e => { if (!this.editors.includes(e)) this.editors.push(e); }); return this; }
  removeEditors(list) {
    const drop = list.map(e => (typeof e === 'string' ? e : e.getEmail()));
    this.editors = this.editors.filter(e => !drop.includes(e));
    return this;
  }
  remove() { this.ss.protections = this.ss.protections.filter(p => p !== this); }
}

export class MockSpreadsheet {
  constructor(id = 'sheet-test-1') {
    this.id = id;
    this.sheets = [];
    this.protections = [];
    this.toasts = [];
  }
  getId() { return this.id; }
  getSheetByName(name) { return this.sheets.find(s => s.name === name) || null; }
  getSheets() { return [...this.sheets]; }
  getActiveSheet() { return this.active || this.sheets[0] || null; }
  insertSheet(name) {
    if (this.getSheetByName(name)) throw new Error(`sheet ${name} exists`);
    const sheet = new MockSheet(this, name);
    this.sheets.push(sheet);
    return sheet;
  }
  toast(message, title) { this.toasts.push(message); }
  _protection(type, sheet, range) {
    const p = new MockProtection(this, type, sheet, range);
    this.protections.push(p);
    return p;
  }
  // Test helper: write a table (header + rows of objects) into a tab.
  tab(name, header, rows = []) {
    const sheet = this.getSheetByName(name) || this.insertSheet(name);
    sheet.getRange(1, 1, 1, header.length).setValues([header]);
    if (rows.length) {
      sheet.getRange(2, 1, rows.length, header.length)
        .setValues(rows.map(r => header.map(h => (r[h] === undefined ? '' : r[h]))));
    }
    return sheet;
  }
}

class MockProperties {
  constructor() { this.map = new Map(); }
  getProperty(k) { return this.map.has(k) ? this.map.get(k) : null; }
  setProperty(k, v) { this.map.set(k, String(v)); return this; }
  deleteProperty(k) { this.map.delete(k); return this; }
  getProperties() { return Object.fromEntries(this.map); }
}

class MenuBuilder {
  constructor(ui, name) { Object.assign(this, { ui, name, items: [] }); }
  addItem(label, fn) { this.items.push({ label, fn }); return this; }
  addSeparator() { this.items.push({ separator: true }); return this; }
  addSubMenu(menu) { this.items.push({ submenu: menu }); return this; }
  addToUi() { this.ui.menus.push(this); }
}

function validationBuilder() {
  const rule = { values: null, allowInvalid: true };
  const builder = {
    requireValueInList(values, dropdown) { rule.values = [...values]; rule.dropdown = dropdown; return builder; },
    setAllowInvalid(b) { rule.allowInvalid = b; return builder; },
    build() { return { ...rule }; },
  };
  return builder;
}

function formatDate(date, tz, fmt) {
  const pad = n => String(n).padStart(2, '0');
  const y = date.getFullYear();
  const m = pad(date.getMonth() + 1);
  const d = pad(date.getDate());
  return fmt.replace('yyyy', y).replace('MM', m).replace('dd', d);
}

// Build a fresh context: services, state the tests steer, and the loaded script.
export function loadCoach(options = {}) {
  const state = {
    token: options.token === undefined ? null : options.token,
    email: options.email || '',
    lockBusy: false,
    fetch: options.fetch || (() => { throw new Error('UrlFetchApp.fetch called with no handler'); }),
    requests: [],
  };
  const ss = new MockSpreadsheet(options.sheetId);
  const ui = {
    alerts: [],
    prompts: [],
    menus: [],
    confirms: [],
    Button: { OK: 'OK', CANCEL: 'CANCEL', YES: 'YES', NO: 'NO' },
    ButtonSet: { OK_CANCEL: 'OK_CANCEL', OK: 'OK', YES_NO: 'YES_NO' },
    // alert(prompt) or alert(title, prompt, buttons); a YES_NO question takes
    // its answer from confirms (default YES).
    alert(...args) {
      const titled = args.length === 3;
      this.alerts.push(titled ? args[0] + '\n\n' + args[1] : String(args[0]));
      const buttons = titled ? args[2] : args[1];
      if (buttons === 'YES_NO') return this.confirms.length ? this.confirms.shift() : 'YES';
      return 'OK';
    },
    prompt() {
      const next = this.prompts.shift();
      if (next === undefined) throw new Error('unexpected prompt');
      return { getSelectedButton: () => (next === null ? 'CANCEL' : 'OK'), getResponseText: () => next || '' };
    },
    createMenu(name) { return new MenuBuilder(this, name); },
  };
  const props = { script: new MockProperties(), document: new MockProperties(), user: new MockProperties() };
  const services = {
    SpreadsheetApp: {
      getActive: () => ss,
      getActiveSpreadsheet: () => ss,
      getUi: () => ui,
      newDataValidation: validationBuilder,
      ProtectionType: { SHEET: 'SHEET', RANGE: 'RANGE' },
      flush() {},
    },
    UrlFetchApp: {
      fetch(url, params) {
        state.requests.push({ url, params });
        const reply = state.fetch(url, params);
        return { getResponseCode: () => reply.code, getContentText: () => reply.text || '' };
      },
    },
    ScriptApp: { getIdentityToken: () => state.token },
    PropertiesService: {
      getScriptProperties: () => props.script,
      getDocumentProperties: () => props.document,
      getUserProperties: () => props.user,
    },
    LockService: {
      getDocumentLock: () => ({
        tryLock: () => !state.lockBusy,
        waitLock: () => { if (state.lockBusy) throw new Error('lock busy'); },
        releaseLock: () => {},
      }),
    },
    Session: {
      getActiveUser: () => ({ getEmail: () => state.email }),
      getEffectiveUser: () => ({ getEmail: () => state.email }),
      getScriptTimeZone: () => 'America/New_York',
    },
    Utilities: {
      formatDate,
      sleep: () => {},
      base64DecodeWebSafe: s => [...Buffer.from(s.replace(/-/g, '+').replace(/_/g, '/'), 'base64')],
      newBlob: bytes => ({ getDataAsString: () => Buffer.from(bytes).toString('utf8') }),
    },
    console: options.verbose ? console : { log() {}, warn() {}, error() {}, info() {} },
    Date,
  };
  const context = vm.createContext({ ...services });
  const code = fs.readFileSync(CODE_PATH, 'utf8');
  vm.runInContext(code + `
;globalThis.__constants = { CLIENT_VERSION, TABS, TAB_ORDER, FLUX_COLUMNS, CRITERIA_COLUMNS, SETTINGS_COLUMNS,
  LOG_COLUMNS, CRITERIA_HISTORY_COLUMNS, CRITERIA_HEALTH_COLUMNS, SECOND_ATTEMPTS_LEAD_COLUMNS, HEALTH_RESOLUTIONS,
  STATUS_STYLES, STALE_LABEL, PROP };`, context, { filename: 'Code.gs' });
  // Copied into this realm: arrays built inside the vm context have a different
  // prototype, and strict deep-equality would reject identical contents.
  const constants = JSON.parse(JSON.stringify(context.__constants));
  return { coach: context, ss, ui, props, state, constants };
}

export function readFixture(name) {
  return JSON.parse(fs.readFileSync(path.resolve(HERE, 'fixtures', name), 'utf8'));
}
