import { apiJson } from './api.js';
import { REMINDER_MODES, ROUTES } from './constants.js';
import { rememberEvent, resetPreviewState, state } from './state.js';
import { $, appendChildren, clearNode, createNode, emptyState, openModal, closeModal, sanitizePhoneId, showToast } from './ui.js';
import { createPreviewEmpty, createPreviewStat, createInviteEventSummary, renderWaveCommandCenter, loadConfirmedUpdateCount, loadInitialWaveCommandCenter, currentEventLabel, buildSummaryCard, selectedRecipientSample, openJobStatusModal } from './invite_view.js';
export { renderWaveCommandCenter } from './invite_view.js';


function displayMarketLabel(value = '') {
  return String(value || '').trim() || '—';
}

function marketFromMember(member = {}) {
  return String(member.market || member.city || '').trim();
}

function setSendButtonReady(isReady) {
  const sendButton = $('send-btn');
  if (!sendButton) return;
  sendButton.disabled = !isReady;
  sendButton.textContent = 'Send Next Wave';
  sendButton.classList.toggle('ready', isReady);
}

























function memberDisplayName(member = {}) {
  const full = String(member.fullName || member.displayName || '').trim();
  if (full) return full;
  const first = String(member.firstName || member.name || '').trim();
  const last = String(member.lastName || '').trim();
  if (first && last && !first.toLowerCase().includes(last.toLowerCase())) return `${first} ${last}`.trim();
  return first || last || '—';
}

function memberTier(member = {}) {
  return String(member.tier || member.accessTier || member.memberTier || member.inviteTier || '').trim() || '—';
}

function memberGender(member = {}) {
  return String(member.gender || member.sex || '').trim().slice(0, 1).toUpperCase() || '—';
}






function getPreviewSort() {
  if (!state.preview.sort) state.preview.sort = { key: 'name', dir: 'asc' };
  return state.preview.sort;
}

function setPreviewSort(key) {
  const current = getPreviewSort();
  state.preview.sort = {
    key,
    dir: current.key === key && current.dir === 'asc' ? 'desc' : 'asc',
  };
  state.preview.page = 1;
  applyPreviewVisibility();
}

function sortPreviewRows(rows = []) {
  const { key, dir } = getPreviewSort();
  const multiplier = dir === 'desc' ? -1 : 1;
  const statusRank = { NOT_INVITED: 1, INVITED: 2, CONFIRMED: 3, DECLINED: 4 };
  return [...rows].sort((a, b) => {
    let av = a.dataset[key] || '';
    let bv = b.dataset[key] || '';
    if (key === 'inviteStatus' || key === 'currentEventInviteStatus') {
      return ((statusRank[av] || 99) - (statusRank[bv] || 99)) * multiplier;
    }
    return av.localeCompare(bv, undefined, { numeric: true, sensitivity: 'base' }) * multiplier;
  });
}

function sortableHeader(label, key) {
  const button = createNode('button', { className: 'table-sort-btn', attrs: { type: 'button' } });
  const { key: activeKey, dir } = getPreviewSort();
  button.textContent = `${label} ${activeKey === key ? (dir === 'asc' ? '↑' : '↓') : '↕'}`;
  button.addEventListener('click', () => setPreviewSort(key));
  return button;
}

function getAllPreviewRows() {
  return Array.from(document.querySelectorAll('#preview-table-container tbody tr'));
}

function rowMatchesPreview(row) {
  if (!row) return false;
  if (state.preview.removedPhones.has(row.dataset.phone)) return false;
  const market = inviteFilterValue('invite-market-filter', 'preview-market-filter', state.preview.activeMarketFilter || 'All');
  state.preview.activeMarketFilter = market;
  const marketOk = market === 'All' || row.dataset.state === market;
  const tierFilter = String(inviteFilterValue('invite-tier-filter', 'preview-tier-filter', '')).toUpperCase();
  const tierOk = !tierFilter || String(row.dataset.tier || '').toUpperCase() === tierFilter;
  const genderFilter = String(inviteFilterValue('invite-gender-filter', 'preview-gender-filter', '')).toUpperCase();
  const genderOk = !genderFilter || String(row.dataset.gender || '').toUpperCase() === genderFilter;
  const statusFilter = String(inviteFilterValue('invite-status-filter', 'preview-invite-status-filter', '')).toUpperCase();
  const rowStatus = (row.dataset.currentEventInviteStatus || row.dataset.inviteStatus || '').toUpperCase();
  const statusOk = !statusFilter || rowStatus === statusFilter;
  const query = String(inviteFilterValue('invite-search', 'preview-search', '')).trim().toLowerCase();
  const haystack = `${row.dataset.name || ''} ${row.dataset.phone || ''} ${row.dataset.state || ''} ${row.dataset.tier || ''} ${row.dataset.gender || ''}`.toLowerCase();
  const searchOk = !query || haystack.includes(query);
  return marketOk && tierOk && genderOk && statusOk && searchOk;
}

function getMatchingPreviewRows() {
  return getAllPreviewRows().filter(rowMatchesPreview);
}

function getSendEligibleRows(rows = getMatchingPreviewRows()) {
  return rows.filter((row) => row.dataset.sendEligible === 'true');
}

function getVisiblePreviewRows() {
  return Array.from(document.querySelectorAll('#preview-table-container tbody tr:not([hidden])'));
}

function getSelectedPreviewRows() {
  const selected = state.preview.selectedPhones || new Set();
  return getSendEligibleRows().filter((row) => selected.has(row.dataset.phone));
}

function syncPreviewCheckboxes() {
  const selected = state.preview.selectedPhones || new Set();
  document.querySelectorAll('.preview-select-checkbox').forEach((checkbox) => {
    checkbox.checked = selected.has(checkbox.dataset.phone);
  });
}



function inviteFilterValue(primaryId, fallbackId, defaultValue = '') {
  const primary = $(primaryId);
  if (primary) return primary.value || defaultValue;
  const fallback = $(fallbackId);
  if (fallback) return fallback.value || defaultValue;
  return defaultValue;
}

function syncSelectOptions(selectId, options = [], keepValue = true) {
  const select = $(selectId);
  if (!select) return;
  const previous = keepValue ? select.value : '';
  select.replaceChildren();
  options.forEach(([value, text]) => select.appendChild(createNode('option', { text, attrs: { value } })));
  if (keepValue && [...select.options].some((option) => option.value === previous)) select.value = previous;
}

function syncAudienceFilterOptions(members = []) {
  const markets = [['All', 'All Markets']];
  const tiers = [['', 'All Tiers']];
  const genders = [['', 'All Genders']];
  const statuses = [
    ['', 'All Invite Statuses'],
    ['NOT_INVITED', 'Not invited'],
    ['INVITED', 'Invited'],
    ['CONFIRMED', 'Confirmed'],
    ['DECLINED', 'Declined'],
  ];
  const marketValues = [...new Set(members.map((m) => marketFromMember(m)).filter((v) => v && v !== '—'))].sort();
  const tierValues = [...new Set(members.map((m) => memberTier(m)).filter((v) => v && v !== '—'))].sort();
  const genderValues = [...new Set(members.map((m) => memberGender(m)).filter((v) => v && v !== '—'))].sort();
  marketValues.forEach((market) => markets.push([market, displayMarketLabel(market)]));
  tierValues.forEach((tier) => tiers.push([tier, `Tier ${tier}`]));
  genderValues.forEach((gender) => genders.push([gender, gender]));
  syncSelectOptions('invite-market-filter', markets);
  syncSelectOptions('invite-tier-filter', tiers);
  syncSelectOptions('invite-gender-filter', genders);
  syncSelectOptions('invite-status-filter', statuses);
}

let audienceFiltersBound = false;
function ensureAudienceFilterListeners() {
  if (audienceFiltersBound) return;
  audienceFiltersBound = true;
  ['invite-search','invite-market-filter','invite-tier-filter','invite-gender-filter','invite-status-filter'].forEach((id) => {
    const control = $(id);
    if (!control) return;
    control.addEventListener(control.tagName === 'INPUT' ? 'input' : 'change', () => {
      state.preview.page = 1;
      if (!state.preview.lastPreview) return;
      if (id === 'invite-status-filter') {
        applyPreviewVisibility();
        return;
      }
      clearPreviewUi({ message: 'Audience filters changed. Run Preview Next Wave again so the server can lock the correct audience.', keepVisible: true });
    });
  });
}

function createSelectionToolbar() {
  const row = createNode('div', { className: 'preview-selection-toolbar invite-filter-card' });
  appendChildren(
    row,
    createNode('button', { className: 'mini-btn preview-clear-selected-btn', text: 'Clear Checks', attrs: { type: 'button' } }),
    createNode('span', { className: 'preview-selection-count', attrs: { id: 'preview-selected-count' }, text: '0 selected' }),
    createNode('span', { className: 'preview-selection-count', attrs: { id: 'preview-remaining-count' }, text: '0 eligible' }),
    createNode('div', { className: 'preview-send-warning', attrs: { id: 'preview-send-warning' }, text: 'No boxes checked — Send Invites will text everyone matching the current filters who is not already invited.' }),
  );
  return row;
}

function renderPreviewPager(totalPages = 1) {
  const host = clearNode('preview-pagination');
  if (!host || totalPages <= 1) return;
  const make = (label, page, opts = {}) => {
    const btn = createNode('button', { className: `page-btn${opts.active ? ' active' : ''}`, text: label, attrs: { type: 'button', disabled: opts.disabled } });
    btn.addEventListener('click', () => { state.preview.page = page; applyPreviewVisibility(); });
    return btn;
  };
  host.appendChild(make('‹', state.preview.page - 1, { disabled: state.preview.page <= 1 }));
  const start = Math.max(1, state.preview.page - 2);
  const end = Math.min(totalPages, start + 4);
  for (let page = start; page <= end; page += 1) host.appendChild(make(String(page), page, { active: page === state.preview.page }));
  host.appendChild(make('›', state.preview.page + 1, { disabled: state.preview.page >= totalPages }));
}

function applyPreviewVisibility() {
  const matching = sortPreviewRows(getMatchingPreviewRows());
  const tbody = document.querySelector('#preview-table-container tbody');
  if (tbody) matching.forEach((row) => tbody.appendChild(row));
  const pageSize = 50;
  state.preview.pageSize = pageSize;
  const totalPages = Math.max(1, Math.ceil(matching.length / pageSize));
  state.preview.page = Math.min(Math.max(1, Number(state.preview.page) || 1), totalPages);
  const start = (state.preview.page - 1) * pageSize;
  const visibleSet = new Set(matching.slice(start, start + pageSize));
  getAllPreviewRows().forEach((row) => { row.hidden = !visibleSet.has(row); });
  renderPreviewPager(totalPages);
  updatePreviewCount();
}


function createGapNote(gap) {
  if (!gap) return null;
  const wrapper = createNode('div', { className: 'preview-gap-note' });
  appendChildren(
    wrapper,
    createNode('span', { className: 'preview-gap-kicker', text: 'Gap · ' }),
    document.createTextNode('Confirmed headcount: '),
    createNode('strong', { className: 'preview-gap-value', text: Number(gap.currentConfirmed) || 0 }),
    document.createTextNode(' / Need: '),
    createNode('strong', { className: 'preview-gap-value is-gold', text: Number(gap.targetConfirmed) || 0 }),
    document.createTextNode(' / Gap: '),
    createNode('strong', {
      className: `preview-gap-value ${(Number(gap.confirmationGap) || 0) > 0 ? 'is-danger' : 'is-success'}`,
      text: Number(gap.confirmationGap) || 0,
    }),
    document.createTextNode(` · Confirm rate: ${Number(gap.assumedConfirmRate) || 0}% · Show rate: ${Number(gap.assumedShowRate) || 0}%`),
  );
  return wrapper;
}

function createPreviewTable(members = []) {
  const table = createNode('table', { className: 'member-table invite-preview-table' });
  const thead = createNode('thead');
  const headRow = createNode('tr');
  const headers = [
    '',
    sortableHeader('Name', 'name'),
    'Phone',
    sortableHeader('Market', 'state'),
    sortableHeader('Tier', 'tier'),
    sortableHeader('Gender', 'gender'),
    sortableHeader('Invite', 'currentEventInviteStatus'),
    'Action',
  ];
  headers.forEach((header) => {
    const th = createNode('th');
    if (typeof header === 'string') th.textContent = header;
    else th.appendChild(header);
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);

  const tbody = createNode('tbody');
  members.forEach((member) => {
    const phone = String(member.phone || '').trim();
    const market = marketFromMember(member) || '—';
    const tier = memberTier(member);
    const gender = memberGender(member);
    const inviteStatus = String(member.currentEventInviteStatus || member.inviteStatus || 'NOT_INVITED').toUpperCase();
    const sendEligible = inviteStatus === 'NOT_INVITED';
    const name = memberDisplayName(member);
    const row = createNode('tr', {
      className: sendEligible ? '' : 'is-not-sendable',
      attrs: { id: `preview-row-${sanitizePhoneId(phone)}` },
      dataset: {
        phone,
        name,
        state: market,
        tier,
        gender,
        sendEligible: String(sendEligible),
        currentEventInviteStatus: inviteStatus,
        inviteStatus: String(member.inviteStatus || member.currentInviteStatus || member.status || 'NOT_INVITED').toUpperCase(),
      },
    });

    const cells = Array.from({ length: 8 }, () => createNode('td'));
    cells[0].appendChild(createNode('input', {
      className: 'preview-select-checkbox',
      dataset: { phone },
      attrs: { type: 'checkbox', disabled: !sendEligible, 'aria-label': `Select ${name || phone}` },
    }));
    cells[1].appendChild(createNode('span', { className: 'member-name', text: name }));
    cells[2].appendChild(createNode('span', { className: 'member-phone', text: phone || '—' }));
    cells[3].appendChild(createNode('span', { className: 'preview-market', text: displayMarketLabel(market) }));
    cells[4].appendChild(createNode('span', { className: 'tier-pill', text: tier }));
    cells[5].appendChild(createNode('span', { className: 'member-source', text: gender }));
    cells[6].appendChild(createNode('span', { className: 'status-badge APPROVED', text: inviteStatus.replace(/_/g, ' ') }));
    cells[7].appendChild(createNode('button', {
      className: 'action-btn delete preview-remove-btn',
      text: sendEligible ? 'Exclude' : 'Locked',
      dataset: { phone },
      attrs: { type: 'button', disabled: !sendEligible },
    }));

    appendChildren(row, cells);
    tbody.appendChild(row);
  });

  appendChildren(table, thead, tbody);
  return table;
}

function normalizeSummary(rawSummary = {}, members = []) {
  const summary = rawSummary || {};
  const breakdown = summary.breakdown || {};
  const totalInvites = Number(summary.totalInvites);
  const waveNumber = Number(summary.waveNumber);
  const femalePercent = Number(summary.femalePercent);
  const malePercent = Number(summary.malePercent);

  return {
    totalInvites: Number.isFinite(totalInvites) ? totalInvites : members.length,
    waveNumber: Number.isFinite(waveNumber) && waveNumber > 0 ? waveNumber : 1,
    femalePercent: Number.isFinite(femalePercent) ? femalePercent : 0,
    malePercent: Number.isFinite(malePercent) ? malePercent : Math.max(0, 100 - (Number.isFinite(femalePercent) ? femalePercent : 0)),
    breakdown: {
      femTier1: Number(breakdown.femTier1) || 0,
      femTier2: Number(breakdown.femTier2) || 0,
      maleTier1: Number(breakdown.maleTier1) || 0,
      maleTier2: Number(breakdown.maleTier2) || 0,
      tier3Skipped: Number(breakdown.tier3Skipped) || 0,
    },
    gapAnalysis: summary.gapAnalysis || null,
  };
}



export function removeFromPreview(phone) {
  if (!phone) return;
  state.preview.removedPhones.add(phone);
  state.preview.selectedPhones?.delete(phone);
  applyPreviewVisibility();
}

export function updatePreviewCount() {
  const visibleRows = getVisiblePreviewRows();
  const selectedRows = getSelectedPreviewRows();
  const matchingRows = getMatchingPreviewRows();
  const sendEligibleRows = getSendEligibleRows(matchingRows);
  const visible = visibleRows.length;
  const selected = selectedRows.length;
  const countNode = $('preview-remaining-count');
  const selectedNode = $('preview-selected-count');
  if (countNode) countNode.textContent = `${visible} visible · ${sendEligibleRows.length} send-eligible`;
  if (selectedNode) selectedNode.textContent = `${selected} selected`;
  const selectedStat = document.querySelector('.preview-stat-num.gold');
  if (selectedStat) selectedStat.textContent = String(selected);
  const sendButton = $('send-btn');
  if (sendButton) {
    const target = currentPreviewSendTarget();
    sendButton.textContent = 'Send Next Wave';
    sendButton.setAttribute('aria-label', target.mode === 'selected'
      ? `Send invites to ${target.activeRows.length} checked guests`
      : `Send invites to ${target.activeRows.length} filtered eligible guests`);
  }
  syncPreviewCheckboxes();
  renderSendModeWarning();
  setSendButtonReady(Boolean(state.preview.lastPreview) && sendEligibleRows.length > 0);
}


function clearPreviewUi({ message = '', keepVisible = false } = {}) {
  resetPreviewState();

  const previewBox = $('preview-box');
  if (previewBox) previewBox.classList.toggle('visible', keepVisible);

  clearNode('preview-stats');
  const table = clearNode('preview-table-container');
  if (table && message) table.appendChild(createPreviewEmpty(message));

  const countNode = $('preview-remaining-count');
  if (countNode) countNode.textContent = '0';
  setSendButtonReady(false);
}

export async function loadEventIntoInviteForm() {
  try {
    const data = await apiJson(ROUTES.ADMIN_EVENT);
    if (!(data.ok && data.event)) return;

    const event = data.event;
    state.currentEvent = event;
    rememberEvent(event);

    const eventId = event.eventSlug || event.eventId || '';
    const capacity = event.capacity || '';
    const previousEventId = $('inv-event-id').value.trim();

    $('inv-event-id').value = eventId;
    $('inv-capacity').value = capacity;

    if (state.preview.lastPreview && state.preview.lastPreview.eventId !== eventId) {
      clearPreviewUi();
    }

    const summary = clearNode('invite-event-summary');
    if (!summary) return;
    summary.appendChild(createInviteEventSummary(event, capacity));
    summary.classList.add('is-visible');
    syncAudienceFilterOptions([]);
    ensureAudienceFilterListeners();
    await loadInitialWaveCommandCenter(eventId, Number(capacity) || 0, event);
    await loadConfirmedUpdateCount(eventId);

  } catch {
    // preserve current UI behavior
  }
}


export async function runPreview() {
  const eventId = $('inv-event-id').value.trim();
  const capacity = parseInt($('inv-capacity').value, 10) || 0;
  const femalePercent = parseInt($('inv-female-pct').value, 10) || 60;
  const waveSizeRaw = parseInt($('inv-wave-size').value, 10);
  const waveSize = Number.isFinite(waveSizeRaw) ? waveSizeRaw : 0;
  const waveNumber = 0; // backend calculates the next wave from existing event invite rows
  const includeExisting = Boolean($('invite-include-existing')?.checked);
  const audienceFilters = {
    query: String($('invite-search')?.value || '').trim(),
    market: String($('invite-market-filter')?.value || 'All'),
    tier: String($('invite-tier-filter')?.value || ''),
    gender: String($('invite-gender-filter')?.value || ''),
    inviteStatus: String($('invite-status-filter')?.value || ''),
  };

  if (!eventId || !capacity) {
    showToast('Event ID and capacity required', 'error');
    clearPreviewUi();
    return;
  }

  showToast('Calculating...');

  try {
    const data = await apiJson(ROUTES.ADMIN_INVITE_PREVIEW, {
      method: 'POST',
      body: {
        eventId,
        capacity,
        femalePercent,
        autoWave: true,
        waveNumber,
        waveSize,
        includeExisting,
        audienceFilters,
      },
    });

    const resolvedWaveNumber = Number(data.summary?.waveNumber || 1);
    const previewSessionId = data.previewSessionId || data.summary?.previewSessionId || '';
    state.preview.lastPreview = {
      eventId,
      capacity,
      femalePercent,
      waveNumber: resolvedWaveNumber,
      waveSize: Number(data.summary?.waveSize || waveSize || 0),
      autoWave: false,
      lockedWave: true,
      previewSessionId,
      includeExisting,
      audienceFilters,
    };
    if ($('inv-wave-number')) $('inv-wave-number').value = String(resolvedWaveNumber);
    state.preview.members = Array.isArray(data.members) ? data.members : [];
    state.preview.removedPhones = new Set();
    state.preview.selectedPhones = new Set();
    state.preview.activeMarketFilter = 'All';

    renderPreview(data);
    // Auto-check the generated wave. Manual unchecked rows become an override.
    state.preview.selectedPhones = new Set(getSendEligibleRows().map((row) => row.dataset.phone).filter(Boolean));
    syncPreviewCheckboxes();
    updatePreviewCount();
    setSendButtonReady(state.preview.selectedPhones.size > 0);
    showToast(`Preview ready — Wave ${resolvedWaveNumber}`);
  } catch (error) {
    clearPreviewUi({ message: 'Preview failed. Fix the inputs and try again.', keepVisible: false });
    showToast(`Preview failed: ${error.message}`, 'error');
  }
}

export function renderPreview(data) {
  const members = Array.isArray(data.members) ? data.members : [];
  if (!members.length) {
    clearPreviewUi({ message: 'No eligible members matched this wave.', keepVisible: true });
    return;
  }

  const summary = normalizeSummary(data.summary, members);
  renderWaveCommandCenter(data.summary || {}, { capacity: $('inv-capacity')?.value || state.currentEvent?.capacity || 0 });
  const box = $('preview-box');
  box.classList.add('visible');

  const stats = clearNode('preview-stats');
  if (stats) {
    appendChildren(
      stats,
      createPreviewStat(summary.totalInvites, 'Preview Eligible'),
      createPreviewStat(0, 'Selected', 'gold'),
      createPreviewStat(`Wave ${summary.waveNumber}`, 'Locked Send'),
      createPreviewStat($('inv-capacity')?.value || state.currentEvent?.capacity || '—', 'Capacity'),
    );
    const gapNote = createGapNote(summary.gapAnalysis);
    if (gapNote) stats.appendChild(gapNote);
  }

  syncAudienceFilterOptions(members);
  ensureAudienceFilterListeners();

  const tableContainer = clearNode('preview-table-container');
  if (tableContainer) {
    tableContainer.appendChild(createSelectionToolbar());
    const scrollWrap = createNode('div', { className: 'table-scroll-wrap invite-table-scroll' });
    scrollWrap.appendChild(createPreviewTable(members));
    tableContainer.appendChild(scrollWrap);
    tableContainer.appendChild(createNode('div', { className: 'pagination-row preview-pagination-row', attrs: { id: 'preview-pagination' } }));
  }
  state.preview.page = 1;
  applyPreviewVisibility();
}











function currentPreviewSendTarget() {
  const selectedRows = getSelectedPreviewRows();
  const visibleRows = getVisiblePreviewRows();
  const matchingRows = sortPreviewRows(getMatchingPreviewRows());
  const eligibleRows = getSendEligibleRows(matchingRows);
  const mode = selectedRows.length ? 'selected' : 'eligible';
  const activeRows = selectedRows.length ? selectedRows : eligibleRows;
  return {
    mode,
    selectedRows,
    visibleRows,
    matchingRows,
    eligibleRows,
    activeRows,
    phones: Array.from(activeRows).map((row) => row.dataset.phone).filter(Boolean),
    modeLabel: mode,
  };
}

function renderSendModeWarning() {
  const existing = document.getElementById('preview-send-warning');
  if (!existing) return;
  const { mode, selectedRows, activeRows, matchingRows } = currentPreviewSendTarget();
  existing.textContent = mode === 'selected'
    ? `${selectedRows.length} selected — Send Invites will text only checked, send-eligible guests.`
    : `No boxes checked — Send Invites will text ${activeRows.length} send-eligible guest${activeRows.length !== 1 ? 's' : ''}. ${matchingRows.length - activeRows.length} already-invited/locked row${matchingRows.length - activeRows.length !== 1 ? 's are' : ' is'} visible but will not resend.`;
  existing.classList.toggle('is-warning', mode !== 'selected' && activeRows.length > 0);
}

function openSendConfirmationModal({ count, modeLabel, waveNumber, onConfirm }) {
  const { selectedRows, visibleRows, activeRows } = currentPreviewSendTarget();
  const removed = state.preview.removedPhones?.size || 0;
  const market = state.preview.activeMarketFilter || 'All';
  const body = createNode('div', { className: 'send-confirm-body' });
  appendChildren(
    body,
    createNode('p', {
      className: 'modal-copy send-confirm-copy',
      text: modeLabel === 'selected'
        ? `This will send Wave ${waveNumber} only to the ${count} selected guest${count !== 1 ? 's' : ''}.`
        : `This will send Wave ${waveNumber} to all ${count} filtered eligible guest${count !== 1 ? 's' : ''}.`,
    }),
    buildSummaryCard([
      ['Audience', modeLabel === 'selected' ? 'Checked guests only' : 'All filtered eligible guests'],
      ['Recipients', count],
      ['Checked', selectedRows.length],
      ['Visible matches', visibleRows.length],
      ['Removed', removed],
      ['Market Filter', market],
      ['Wave', waveNumber || 1],
      ['Event', currentEventLabel()],
      ['Capacity', $('inv-capacity')?.value || state.currentEvent?.capacity || '—'],
    ]),
  );
  const sample = selectedRecipientSample(activeRows);
  if (sample) body.appendChild(sample);
  body.appendChild(createNode('p', {
    className: modeLabel === 'selected' ? 'send-confirm-safe' : 'send-confirm-warning',
    text: modeLabel === 'selected'
      ? 'Only checked guests will receive this invite.'
      : 'No boxes are checked, so this sends to every guest matching the current filters. Excluded rows will not be sent.',
  }));

  const cancel = createNode('button', { className: 'btn-secondary', text: 'Cancel', attrs: { type: 'button' } });
  cancel.addEventListener('click', closeModal);
  const confirm = createNode('button', { className: 'btn-primary-muted', text: 'Send Next Wave', attrs: { type: 'button' } });
  confirm.addEventListener('click', () => {
    closeModal();
    onConfirm();
  });

  openModal({
    kicker: 'Invite Send Confirmation',
    title: 'Send invites?',
    body,
    actions: [cancel, confirm],
  });
}

async function executeInviteSend({ preview, phones, sendButton }) {
  sendButton.disabled = true;
  sendButton.textContent = 'Sending...';

  try {
    const data = await apiJson(ROUTES.ADMIN_INVITE_SEND, {
      method: 'POST',
      body: {
        ...preview,
        phones,
        confirmSend: true,
        removedPhones: Array.from(state.preview.removedPhones),
      },
    });

    const jobId = data.jobId ? ` Job ${data.jobId}` : '';
    showToast(`Wave ${preview.waveNumber || 1} queued — ${phones.length} recipient${phones.length !== 1 ? 's' : ''}.${jobId}`, 'success');
    if (data.jobId) openJobStatusModal(data.jobId, { mode: 'Invite Wave', count: phones.length, event: currentEventLabel() });
    sendButton.disabled = true;
    sendButton.textContent = 'Run Preview Again';
    sendButton.classList.remove('ready');
  } catch (error) {
    showToast(`Send failed: ${error.message}`, 'error');
    sendButton.disabled = false;
    sendButton.textContent = 'Send Next Wave';
    updatePreviewCount();
  }
}

export async function sendInvites() {
  const preview = state.preview.lastPreview;
  if (!preview) {
    showToast('Run preview first', 'error');
    return;
  }

  const { activeRows, phones, modeLabel } = currentPreviewSendTarget();
  const remaining = activeRows.length;
  if (remaining < 1) {
    setSendButtonReady(false);
    showToast('No recipients left in preview', 'error');
    return;
  }

  const sendButton = $('send-btn');
  openSendConfirmationModal({
    count: remaining,
    modeLabel,
    waveNumber: preview.waveNumber || 1,
    onConfirm: () => executeInviteSend({ preview, phones, sendButton }),
  });
}

export function togglePreviewSelection(phone, checked) {
  if (!phone) return;
  if (!state.preview.selectedPhones) state.preview.selectedPhones = new Set();
  if (checked) state.preview.selectedPhones.add(phone);
  else state.preview.selectedPhones.delete(phone);
  updatePreviewCount();
}


export function clearPreviewSelection() {
  state.preview.selectedPhones = new Set();
  updatePreviewCount();
}


async function executeManualInviteOverride({ eventId, capacity, phone, message, button }) {
  if (button) {
    button.disabled = true;
    button.textContent = 'Sending...';
  }
  try {
    const data = await apiJson(ROUTES.ADMIN_INVITE_SEND, {
      method: 'POST',
      body: {
        eventId,
        capacity,
        femalePercent: parseInt($('inv-female-pct')?.value || '60', 10) || 60,
        autoWave: false,
        waveNumber: 0,
        manualSend: true,
        waveSize: 1,
        phones: [phone],
        messageOverride: message,
        confirmSend: true,
      },
    });
    showToast(`Manual invite queued for ${phone}.`, 'success');
    const phoneField = $('manual-invite-phone');
    const messageField = $('manual-invite-message');
    if (phoneField) phoneField.value = '';
    if (messageField) messageField.value = '';
    if (data.jobId) openJobStatusModal(data.jobId, { mode: 'Manual / Resend', count: 1, event: currentEventLabel() });
  } catch (error) {
    showToast(`Manual invite failed: ${error.message}`, 'error');
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = 'Send Manual Invite';
    }
  }
}

export async function sendManualInviteOverride() {
  await loadEventIntoInviteForm();
  const eventId = $('inv-event-id')?.value?.trim() || state.currentEvent?.eventSlug || state.currentEvent?.eventId || '';
  const capacity = parseInt($('inv-capacity')?.value || state.currentEvent?.capacity || '0', 10) || 0;
  const phone = ($('manual-invite-phone')?.value || '').trim();
  const message = ($('manual-invite-message')?.value || '').trim();
  const button = $('manual-invite-send-btn');
  if (!eventId || capacity < 1) {
    showToast('Load a Live event with capacity first', 'error');
    return;
  }
  if (!phone) {
    showToast('Enter the approved member phone number', 'error');
    return;
  }
  if (!message) {
    showToast('Write the manual invite copy first', 'error');
    return;
  }

  const body = createNode('div', { className: 'send-confirm-body' });
  appendChildren(
    body,
    createNode('p', { className: 'modal-copy send-confirm-copy', text: 'This sends one manual invite using the custom copy below.' }),
    buildSummaryCard([['Phone', phone], ['Event', currentEventLabel()], ['Wave', 'Manual / Resend']]),
    createNode('p', { className: 'send-confirm-safe', text: message }),
  );
  const cancel = createNode('button', { className: 'btn-secondary', text: 'Cancel', attrs: { type: 'button' } });
  cancel.addEventListener('click', closeModal);
  const confirm = createNode('button', { className: 'btn-primary-muted', text: 'Send Manual Invite', attrs: { type: 'button' } });
  confirm.addEventListener('click', () => {
    closeModal();
    executeManualInviteOverride({ eventId, capacity, phone, message, button });
  });
  openModal({ kicker: 'Manual / Resend', title: 'Send one invite?', body, actions: [cancel, confirm] });
}

async function executeConfirmedUpdate({ eventId, message, button }) {
  if (button) {
    button.disabled = true;
    button.textContent = 'Sending...';
  }
  try {
    const data = await apiJson(ROUTES.ADMIN_INVITE_SEND, {
      method: 'POST',
      body: {
        eventId,
        confirmedUpdate: true,
        messageOverride: message,
        confirmSend: true,
      },
    });
    const count = Number(data.recipientCount ?? 0) || 0;
    showToast(`One-time text queued for ${count} confirmed member${count !== 1 ? 's' : ''}.`, 'success');
    // Clear the message so the same text can't be accidentally re-sent on the next tap.
    const messageField = $('confirmed-update-message');
    if (messageField) messageField.value = '';
    if (data.jobId) openJobStatusModal(data.jobId, { mode: 'Confirmed Guest Update', count, event: currentEventLabel() });
  } catch (error) {
    showToast(`One-time text failed: ${error.message}`, 'error');
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = 'Send One-Time Text';
    }
  }
}

export function prefillVenueReveal() {
  const ev = state.currentEvent || {};
  const venue = (ev.venue || '').trim();
  const address = (ev.address || '').trim();
  if (!venue && !address) {
    showToast('Add a venue or address to the event first', 'error');
    return;
  }
  const field = $('confirmed-update-message');
  if (!field) return;
  let location;
  if (venue && address) location = `${venue} — ${address}.`;
  else location = `${venue || address}.`;
  field.value = `The location: ${location}`;
  field.focus();
  showToast('Venue loaded — review, then Send One-Time Text', 'success');
}

export async function sendConfirmedUpdate() {
  const eventId = $('inv-event-id')?.value?.trim() || state.currentEvent?.eventSlug || state.currentEvent?.eventId || '';
  const message = ($('confirmed-update-message')?.value || '').trim();
  const button = $('confirmed-update-send-btn');
  if (!eventId) {
    showToast('Load an event before sending an update', 'error');
    return;
  }
  if (!message) {
    showToast('Write the one-time update first', 'error');
    return;
  }
  const body = createNode('div', { className: 'send-confirm-body' });
  appendChildren(
    body,
    createNode('p', { className: 'modal-copy send-confirm-copy', text: 'This sends one update text only to confirmed guests for this event. It does not invite pending, denied, declined, or not-invited members.' }),
    buildSummaryCard([
      ['Audience', 'Confirmed event guests only'],
      ['Event', currentEventLabel()],
      ['Message', message.length > 120 ? `${message.slice(0, 120)}…` : message],
    ]),
    createNode('p', { className: 'send-confirm-warning', text: 'Review the message before sending. This does not overwrite event invite templates.' }),
  );
  const cancel = createNode('button', { className: 'btn-secondary', text: 'Cancel', attrs: { type: 'button' } });
  cancel.addEventListener('click', closeModal);
  const confirm = createNode('button', { className: 'btn-primary-muted', text: 'Send One-Time Text', attrs: { type: 'button' } });
  confirm.addEventListener('click', () => {
    closeModal();
    executeConfirmedUpdate({ eventId, message, button });
  });
  openModal({ kicker: 'Confirmed Guest Update', title: 'Send one-time text?', body, actions: [cancel, confirm] });
}

async function executeManualReminder({ timing, button }) {
  if (button) {
    button.disabled = true;
    button.textContent = 'Sending...';
  }
  try {
    const data = await apiJson(ROUTES.ADMIN_INVITE_REMINDER, {
      method: 'POST',
      body: { timing },
    });
    const sent = Number(data.sent || 0);
    if (data.continuationQueued) {
      showToast(`Reminder started for ${sent}; ${Number(data.remainingRecipients || 0)} more queued.`, 'success');
    } else {
      showToast(`Reminder finished: ${sent} sent, ${Number(data.skippedAlreadySent || 0)} already sent, ${Number(data.skippedOptOut || 0)} opted out, ${Number(data.failed || 0)} failed.`, 'success');
    }
  } catch (error) {
    showToast(`Reminder failed: ${error.message}`, 'error');
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = 'Send Saved Reminder';
    }
  }
}

export function sendManualReminder() {
  const timing = $('manual-reminder-timing')?.value === 'day_of' ? 'day_of' : 'day_before';
  const button = $('manual-reminder-send-btn');
  if (!state.currentEvent) {
    showToast('Load an event before sending a reminder', 'error');
    return;
  }
  const timingLabel = timing === 'day_of' ? 'day-of' : 'day-before';
  const body = createNode('div', { className: 'send-confirm-body' });
  appendChildren(
    body,
    createNode('p', { className: 'modal-copy send-confirm-copy', text: `This sends the saved ${timingLabel} reminder now to confirmed guests who have not already received it.` }),
    buildSummaryCard([
      ['Audience', 'Eligible confirmed guests only'],
      ['Event', currentEventLabel()],
      ['Reminder', timingLabel],
    ]),
    createNode('p', { className: 'send-confirm-warning', text: 'This sends SMS messages. Check the reminder copy and event before continuing.' }),
  );
  const cancel = createNode('button', { className: 'btn-secondary', text: 'Cancel', attrs: { type: 'button' } });
  cancel.addEventListener('click', closeModal);
  const confirm = createNode('button', { className: 'btn-primary-muted', text: 'Send Saved Reminder', attrs: { type: 'button' } });
  confirm.addEventListener('click', () => {
    closeModal();
    executeManualReminder({ timing, button });
  });
  openModal({ kicker: 'Manual Reminder', title: 'Send reminder now?', body, actions: [cancel, confirm] });
}
