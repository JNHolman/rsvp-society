#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const adminDir = path.resolve(here, '../../frontend/admin');
const frontendDir = path.resolve(here, '../../frontend');
const inviteSource = fs.readFileSync(path.join(adminDir, 'invite.js'), 'utf8');
const membersSource = fs.readFileSync(path.join(adminDir, 'members.js'), 'utf8');
const appSource = fs.readFileSync(path.join(frontendDir, 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(frontendDir, 'index.html'), 'utf8');
const importSource = fs.readFileSync(path.join(adminDir, 'import.js'), 'utf8');
const adminIndexSource = fs.readFileSync(path.join(adminDir, 'index.html'), 'utf8');
const adminAppSource = fs.readFileSync(path.join(adminDir, 'admin-app.js'), 'utf8');
const eventSource = fs.readFileSync(path.join(adminDir, 'event.js'), 'utf8');
const inviteViewSource = fs.readFileSync(path.join(adminDir, 'invite_view.js'), 'utf8');

const AREA_TO_STATE = {
  '502': 'Kentucky',
  '615': 'Tennessee',
  '859': 'Kentucky',
};

function extractFunction(source, name) {
  const re = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\([^)]*\\)\\s*\\{`);
  const match = re.exec(source);
  assert.ok(match, `${name} not found`);
  const start = match.index;
  const braceStart = start + match[0].lastIndexOf('{');
  let depth = 0;
  for (let i = braceStart; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

function loadFunction(source, name, extraContext = {}) {
  const context = { AREA_TO_STATE, ...extraContext };
  vm.createContext(context);
  vm.runInContext(`${extractFunction(source, name)}; this.__fn = ${name};`, context);
  return context.__fn;
}

const marketFromMember = loadFunction(inviteSource, 'marketFromMember');
const getMarket = loadFunction(membersSource, 'getMarket');

test('Invite UI uses explicit member market even when phone area code belongs elsewhere', () => {
  assert.equal(
    marketFromMember({ phone: '+16155551212', market: 'Louisville' }),
    'Louisville',
  );
});

test('Louisville and Lexington remain distinct explicit markets', () => {
  assert.equal(marketFromMember({ phone: '+15025550001', market: 'Louisville' }), 'Louisville');
  assert.equal(marketFromMember({ phone: '+15025550002', market: 'Lexington' }), 'Lexington');
});

test('runPreview sends the operator filters and preserves the backend preview lock', async () => {
  const elements = {
    'inv-event-id': { value: 'rooftop-sept2026' },
    'inv-capacity': { value: '100' },
    'inv-female-pct': { value: '60' },
    'inv-wave-size': { value: '25' },
    'invite-include-existing': { checked: false },
    'invite-search': { value: 'Jordan' },
    'invite-market-filter': { value: 'Louisville' },
    'invite-tier-filter': { value: '1' },
    'invite-gender-filter': { value: 'F' },
    'invite-status-filter': { value: '' },
    'inv-wave-number': { value: '' },
  };
  let request;
  const state = { preview: {} };
  const runPreview = loadFunction(inviteSource, 'runPreview', {
    $: (id) => elements[id] || null,
    apiJson: async (route, options) => {
      request = { route, options };
      return {
        ok: true,
        previewSessionId: 'lock-123',
        summary: { waveNumber: 2, waveSize: 25, previewSessionId: 'lock-123' },
        members: [],
      };
    },
    ROUTES: { ADMIN_INVITE_PREVIEW: '/admin/invite/preview' },
    state,
    showToast: () => {},
    clearPreviewUi: () => {},
    renderPreview: () => {},
    getSendEligibleRows: () => [],
    syncPreviewCheckboxes: () => {},
    updatePreviewCount: () => {},
    setSendButtonReady: () => {},
  });

  await runPreview();

  assert.equal(request.route, '/admin/invite/preview');
  assert.deepEqual(JSON.parse(JSON.stringify(request.options.body.audienceFilters)), {
    query: 'Jordan', market: 'Louisville', tier: '1', gender: 'F', inviteStatus: '',
  });
  assert.equal(state.preview.lastPreview.previewSessionId, 'lock-123');
  assert.equal(state.preview.lastPreview.lockedWave, true);
  assert.equal(state.preview.lastPreview.waveNumber, 2);
});

test('executeInviteSend sends the exact preview session and selected phone subset', async () => {
  let request;
  const state = { preview: { removedPhones: new Set(['+15025550003']) } };
  const button = {
    disabled: false,
    textContent: '',
    classList: { remove: () => {} },
  };
  const executeInviteSend = loadFunction(inviteSource, 'executeInviteSend', {
    apiJson: async (route, options) => {
      request = { route, options };
      return { ok: true, jobId: 'job-1' };
    },
    ROUTES: { ADMIN_INVITE_SEND: '/admin/invite/send' },
    state,
    showToast: () => {},
    openJobStatusModal: () => {},
    currentEventLabel: () => 'RSVP Society',
    updatePreviewCount: () => {},
  });
  const preview = {
    eventId: 'rooftop-sept2026',
    capacity: 100,
    waveNumber: 2,
    lockedWave: true,
    previewSessionId: 'lock-123',
    audienceFilters: { market: 'Louisville' },
  };

  await executeInviteSend({ preview, phones: ['+15025550002'], sendButton: button });

  assert.equal(request.route, '/admin/invite/send');
  assert.equal(request.options.body.previewSessionId, 'lock-123');
  assert.deepEqual(Array.from(request.options.body.phones), ['+15025550002']);
  assert.deepEqual(Array.from(request.options.body.removedPhones), ['+15025550003']);
  assert.equal(request.options.body.confirmSend, true);
});

test('invite job status shows the scheduled next wave', async () => {
  const summaries = [];
  let modal;
  const makeNode = (tag, props = {}) => ({
    tag,
    ...props,
    children: [],
    addEventListener() {},
    appendChild(child) { this.children.push(child); },
  });
  const openStatus = loadFunction(inviteViewSource, 'openJobStatusModal', {
    apiJson: async () => ({
      status: 'COMPLETE', smsSent: 20, invitesWritten: 20, failed: 0,
      autoWaveStatus: 'SCHEDULED', autoWaveNumber: 2, autoWaveAt: 48,
    }),
    ROUTES: { ADMIN_INVITE_STATUS: '/admin/invite/status' },
    createNode: makeNode,
    appendChildren: (parent, ...children) => parent.children.push(...children),
    buildSummaryCard: (rows) => { summaries.push(rows); return { rows }; },
    clearNode: (node) => { node.children = []; },
    openModal: (value) => { modal = value; },
    closeModal: () => {},
    currentEventLabel: () => 'RSVP Society',
    window: { setTimeout() { throw new Error('completed job should not poll again'); } },
  });

  openStatus('job-1', { mode: 'Invite Wave', count: 20 });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.ok(modal);
  assert.ok(summaries.at(-1).some(([label, value]) => label === 'Next Wave' && value === 'Wave 2 in 48 hours'));
});


test('currentPreviewSendTarget sends only checked rows when any are selected', () => {
  const selected = [{ dataset: { phone: '+15025550001' } }];
  const eligible = [
    { dataset: { phone: '+15025550001' } },
    { dataset: { phone: '+15025550002' } },
  ];
  const fn = loadFunction(inviteSource, 'currentPreviewSendTarget', {
    getSelectedPreviewRows: () => selected,
    getVisiblePreviewRows: () => eligible,
    getMatchingPreviewRows: () => eligible,
    sortPreviewRows: (rows) => rows,
    getSendEligibleRows: () => eligible,
  });
  const target = fn();
  assert.equal(target.mode, 'selected');
  assert.deepEqual(Array.from(target.phones), ['+15025550001']);
});

test('currentPreviewSendTarget falls back to all filtered eligible rows when none are checked', () => {
  const eligible = [
    { dataset: { phone: '+15025550001' } },
    { dataset: { phone: '+15025550002' } },
  ];
  const fn = loadFunction(inviteSource, 'currentPreviewSendTarget', {
    getSelectedPreviewRows: () => [],
    getVisiblePreviewRows: () => eligible,
    getMatchingPreviewRows: () => eligible,
    sortPreviewRows: (rows) => rows,
    getSendEligibleRows: () => eligible,
  });
  const target = fn();
  assert.equal(target.mode, 'eligible');
  assert.deepEqual(Array.from(target.phones), ['+15025550001', '+15025550002']);
});

test('Invite UI does not infer residence/market from phone area code', () => {
  assert.equal(marketFromMember({ phone: '+16155551212' }), '');
});

test('Members UI does not display area-code-derived state as member market', () => {
  assert.equal(getMarket({ phone: '+16155551212' }), '—');
});

test('Members UI displays stored city/state location', () => {
  assert.equal(getMarket({ phone: '+16155551212', city: 'Louisville', state: 'KY' }), 'Louisville, KY');
});

test('public signup form requires ZIP and submits zipCode', () => {
  assert.match(indexSource, /autocomplete=["']postal-code["']/i);
  assert.match(indexSource, /id=["']zipInput["']/i);
  assert.match(appSource, /const\s+zipCode\s*=/);
  assert.match(appSource, /zipCode,/);
});


test('CSV first-name detection never steals a Last Name column', () => {
  const getColumnIndex = loadFunction(importSource, 'getColumnIndex');
  const headers = ['phone', 'last name', 'email'];
  const firstNameIndex = getColumnIndex(headers, ['first name', 'name', 'full name', 'attendee']);
  const lastNameIndex = getColumnIndex(headers, ['last name', 'lastname', 'surname']);
  assert.equal(firstNameIndex, -1);
  assert.equal(lastNameIndex, 1);
});

test('CSV header detection still accepts descriptive phone/name headers', () => {
  const getColumnIndex = loadFunction(importSource, 'getColumnIndex');
  const headers = ['primary phone number', 'guest full name', 'surname'];
  assert.equal(getColumnIndex(headers, ['phone', 'mobile', 'cell', 'number', 'tel']), 0);
  assert.equal(getColumnIndex(headers, ['first name', 'name', 'full name', 'attendee']), 1);
});

test('CSV import sends explicit consent confirmation instead of silently opting in', async () => {
  assert.match(adminIndexSource, /id=["']import-sms-consent["']/);
  assert.doesNotMatch(importSource, /smsOptIn:\s*true/);
  const state = { importRows: [{ phone: '+15025551212', name: 'Jordan', zipCode: '40205' }], importSkipped: 0 };
  const elements = {
    'import-confirm-btn': { textContent: 'Import', disabled: false },
    'import-sms-consent': { checked: false },
  };
  let request;
  const fn = loadFunction(importSource, 'confirmImport', {
    state,
    $: (id) => elements[id] || null,
    effectiveImportSource: () => 'csv',
    importConsentConfirmed: () => elements['import-sms-consent'].checked,
    apiJson: async (route, options) => { request = { route, options }; return { imported: 1, skipped: 0 }; },
    ROUTES: { ADMIN_MEMBER_IMPORT: '/admin/members/import' },
    showToast: () => {},
    closeImport: () => {},
    loadMembers: async () => {},
    loadStats: async () => {},
  });
  await fn();
  assert.equal(request.options.body.consentConfirmed, false);
  assert.equal(request.options.body.source, 'csv');
  assert.equal(request.options.body.members[0].smsOptIn, undefined);
});

test('CSV preview counts rows excluded for missing ZIP location', () => {
  const state = {};
  let previewRendered = false;
  let confirmEnabled;
  class FakeFileReader {
    readAsText() {
      this.onload({ target: { result: 'phone,name\n5025551212,Jordan' } });
    }
  }
  const parseCSV = loadFunction(importSource, 'parseCSV', {
    FileReader: FakeFileReader,
    parseCsvText: () => [['phone', 'name'], ['5025551212', 'Jordan']],
    getColumnIndex: (headers, choices) => headers.findIndex((header) => choices.includes(header)),
    state,
    renderImportPreview: () => { previewRendered = true; },
    setImportConfirmEnabled: (enabled) => { confirmEnabled = enabled; },
  });
  parseCSV({});
  assert.equal(state.importRows.length, 0);
  assert.equal(state.importLocationSkipped, 1);
  assert.equal(previewRendered, true);
  assert.equal(confirmEnabled, false);
});


test('Replacing a valid CSV with an empty file clears the previous import', () => {
  const state = { importRows: [{ phone: '5025551212', zipCode: '40205' }] };
  let enabled = true;
  class FakeReader {
    readAsText() { this.onload({ target: { result: '' } }); }
  }
  loadFunction(importSource, 'parseCSV', {
    FileReader: FakeReader, state, parseCsvText: () => [],
    renderImportPreview: () => {},
    setImportConfirmEnabled: (value) => { enabled = value; },
  })({});
  assert.equal(state.importRows.length, 0);
  assert.equal(enabled, false);
});

test('Manual invite override is exposed and sends through the existing manual backend path', async () => {
  assert.match(adminIndexSource, /id=["']manual-invite-phone["']/);
  assert.match(adminIndexSource, /id=["']manual-invite-message["']/);
  assert.match(adminIndexSource, /id=["']manual-invite-send-btn["']/);
  assert.match(adminAppSource, /manual-invite-send-btn/);

  let request;
  const elements = {
    'inv-female-pct': { value: '60' },
    'manual-invite-phone': { value: '+15025551212' },
    'manual-invite-message': { value: 'Jordan, RSVP Society. Reply yes if you are in.' },
  };
  const fn = loadFunction(inviteSource, 'executeManualInviteOverride', {
    $: (id) => elements[id] || null,
    apiJson: async (route, options) => { request = { route, options }; return { ok: true, jobId: 'manual-1' }; },
    ROUTES: { ADMIN_INVITE_SEND: '/admin/invite/send' },
    showToast: () => {},
    openJobStatusModal: () => {},
    currentEventLabel: () => 'RSVP Society',
  });
  await fn({
    eventId: 'rooftop-sept2026', capacity: 100, phone: '+15025551212',
    message: 'Jordan, RSVP Society. Reply yes if you are in.',
    button: { disabled: false, textContent: '' },
  });
  const body = request.options.body;
  assert.equal(request.route, '/admin/invite/send');
  assert.equal(body.waveNumber, 0);
  assert.equal(body.manualSend, true);
  assert.equal(body.autoWave, false);
  assert.equal(body.messageOverride, 'Jordan, RSVP Society. Reply yes if you are in.');
  assert.deepEqual(Array.from(body.phones), ['+15025551212']);
  assert.equal(body.confirmSend, true);
});


test('Event editor captures ZIP and per-event promotion radius', () => {
  assert.match(adminIndexSource, /id=["']ev-zip["']/);
  assert.match(adminIndexSource, /id=["']ev-promotion-radius["']/);
  assert.match(eventSource, /eventZipCode:\s*\(\$\('ev-zip'\)/);
  assert.match(eventSource, /promotionRadiusMiles:\s*\(\$\('ev-promotion-radius'\)/);
  assert.match(eventSource, /event_status === 'LIVE'/);
  assert.match(eventSource, /Promotion Radius must be greater than 0 before going Live/);
});

test('Manual reminder UI confirms, then calls the bounded reminder endpoint', async () => {
  assert.match(adminIndexSource, /id=["']manual-reminder-timing["']/);
  assert.match(adminIndexSource, /id=["']manual-reminder-send-btn["']/);
  assert.match(adminAppSource, /manual-reminder-send-btn[\s\S]*sendManualReminder/);
  assert.match(inviteSource, /confirm\.addEventListener\('click',[\s\S]*executeManualReminder\(\{ timing, button \}\)/);

  let request;
  let message;
  const fn = loadFunction(inviteSource, 'executeManualReminder', {
    apiJson: async (route, options) => {
      request = { route, options };
      return { sent: 250, continuationQueued: true, remainingRecipients: 18 };
    },
    ROUTES: { ADMIN_INVITE_REMINDER: '/admin/invite/reminder' },
    showToast: (value) => { message = value; },
  });
  const button = { disabled: false, textContent: 'Send Saved Reminder' };
  await fn({ timing: 'day_of', button });
  assert.equal(request.route, '/admin/invite/reminder');
  assert.equal(request.options.body.timing, 'day_of');
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Send Saved Reminder');
  assert.match(message, /250; 18 more queued/);
});

test('CSV imports 7500 rows with more than 100 ZIPs in bounded batches and deduplicates phones', async () => {
  const rows = Array.from({ length: 7500 }, (_, i) => ({ phone: `+1502${String(i).padStart(7, '0')}`, zipCode: String(10000 + i) }));
  const state = { importRows: [...rows, { ...rows[0], phone: rows[0].phone.slice(2) }] };
  const batches = [];
  const button = {};
  await loadFunction(importSource, 'confirmImport', {
    state, $: () => button, effectiveImportSource: () => 'csv', importConsentConfirmed: () => false,
    ROUTES: { ADMIN_MEMBER_IMPORT: '/import' },
    apiJson: async (_, options) => { batches.push(options.body); return { imported: options.body.members.length }; },
    showToast: () => {}, closeImport: () => {}, loadMembers: async () => {}, loadStats: async () => {},
  })();
  assert.equal(batches.length, 300);
  assert.ok(batches.every((b) => b.members.length === 25 && b.consentConfirmed === false));
  assert.equal(new Set(batches.flatMap((b) => b.members.map((m) => m.phone))).size, 7500);
  assert.equal(state.importRows.length, 0);
});

test('CSV stops at a failed batch without replaying completed or uncertain writes', async () => {
  const state = { importRows: Array.from({ length: 80 }, (_, i) => ({ phone: `+1502${String(i).padStart(7, '0')}`, zipCode: '40205' })) };
  let calls = 0; let warning = ''; const button = {};
  const fn = loadFunction(importSource, 'confirmImport', {
    state, $: () => button, effectiveImportSource: () => 'csv', importConsentConfirmed: () => true,
    ROUTES: { ADMIN_MEMBER_IMPORT: '/import' },
    apiJson: async () => { if (++calls === 2) throw new Error('timeout'); return { imported: 25 }; },
    showToast: (message) => { warning = message; }, closeImport: () => {}, loadMembers: async () => {}, loadStats: async () => {},
  });
  await fn(); await fn();
  assert.equal(calls, 2);
  assert.match(warning, /25 processed rows/);
  assert.match(warning, /last batch may have saved/);
  assert.equal(button.disabled, true);
});

test('admin requests ignore stored API overrides and reject external destinations', async () => {
  const source = fs.readFileSync(path.join(adminDir, 'api.js'), 'utf8');
  const calls = [];
  const apiFetch = loadFunction(source, 'apiFetch', {
    state: { apiBase: 'https://untrusted.example', adminToken: 'test-token' },
    API_DEFAULT: 'https://api.rsvpsociety.com', URL,
    FormData: class FormData {},
    fetch: async (...args) => { calls.push(args); return { ok: true }; },
  });
  await apiFetch('/admin/members', { redirect: 'follow' });
  assert.equal(calls[0][0], 'https://api.rsvpsociety.com/admin/members');
  assert.equal(calls[0][1].headers['x-admin-token'], 'test-token');
  assert.equal(calls[0][1].redirect, 'error');
  await assert.rejects(() => apiFetch('https://untrusted.example/steal'), /Untrusted API/);
  await assert.rejects(() => apiFetch('//untrusted.example/steal'), /Untrusted API/);
  assert.equal(calls.length, 1);
});

test('check-in preserves the nonmember guest star', () => {
  const source = fs.readFileSync(path.join(adminDir, 'checkin.js'), 'utf8');
  const node = (tag, options) => ({ tag, ...options, children: [], appendChild(child) { this.children.push(child); } });
  const render = loadFunction(source, 'renderPlusOne', { node, checkedInPlusOnes: new Set() });
  const guest = { phone: '+15025550123', plusOneName: 'Guest Name' };
  assert.equal(render({ ...guest, plusOneIsMember: false }).children[0].children[0].attrs.title, 'Not a member');
  assert.equal(render({ ...guest, plusOneIsMember: true }).children[0].children.length, 0);
});
