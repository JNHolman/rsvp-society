import { apiJson } from './api.js';
import { closeDrawer } from './drawer.js';
import { ROUTES } from './constants.js';
import { setMembers, state } from './state.js';
import {
  $,
  appendChildren,
  clearNode,
  createNode,
  emptyState,
  loadingState,
  openModal,
  closeModal,
  sanitizePhoneId,
  setActiveButton,
  showToast,
} from './ui.js';

const STATUS_TITLES = {
  PENDING: 'Pending Review',
  APPROVED: 'Approved Members',
  DENIED: 'Denied Requests',
};

function setStatNode(status, value) {
  const statNode = document.getElementById(`stat-${String(status || '').toLowerCase()}`);
  if (statNode) statNode.textContent = value;
}

function getMemberCount(data) {
  if (Number.isFinite(data?.total)) return data.total;
  return Array.isArray(data?.members) ? data.members.length : 0;
}

function normalizePhone(phone) {
  return String(phone || '').trim();
}


function getMarket(member = {}) {
  const market = String(member.market || member.city || '').trim();
  const state = String(member.state || '').trim();
  if (market && state) return `${market}, ${state}`;
  return market || state || '—';
}

function currentMemberFilters() {
  return {
    query: String($('member-search')?.value || '').trim().toLowerCase(),
  };
}

function memberPageKey(status, filters = currentMemberFilters()) {
  return JSON.stringify({ status, query: filters.query || '' });
}

function buildMemberQuery(status, filters = currentMemberFilters(), options = {}) {
  const params = new URLSearchParams();
  params.set('status', status);
  params.set('limit', String(options.limit || state.pageSize || 50));
  if (filters.query) params.set('q', filters.query);
  if (options.cursor) params.set('nextPageToken', options.cursor);
  if (options.includeTotal) params.set('includeTotal', '1');
  return `${ROUTES.ADMIN_MEMBERS}?${params.toString()}`;
}

function hasActiveMemberFilters(filters = currentMemberFilters()) {
  return Boolean(filters.query);
}

function memberHistoryRows(member = {}) {
  return [
    ['Invites Sent', Number(member.invitedCount) || 0],
    ['Events Attended', Number(member.attendedCount) || 0],
    ['Timely Cancellations', Number(member.timelyCancellationCount) || 0],
    ['No-Shows', Number(member.noShowCount) || 0],
    ['Current Event', member.currentEventId || '—'],
    ['Current Invite', member.currentEventInviteStatus || member.currentInviteStatus || member.inviteStatus || '—'],
    ['Last Invited', member.lastInvitedAt || member.invitedAt || '—'],
    ['Last Confirmed', member.lastConfirmedAt || member.confirmedAt || '—'],
    ['Delivered', member.deliveredAt || '—'],
    ['Last Seen', member.lastSeenAt || member.attendedAt || '—'],
    ['Last SMS', member.lastSmsStatus || member.smsStatus || '—'],
  ];
}

function renderMemberHistoryRows(container, history = []) {
  clearNode(container);
  if (!Array.isArray(history) || !history.length) {
    container.appendChild(createNode('p', { className: 'member-intel-note', text: 'No per-event invite history yet.' }));
    return;
  }
  history.slice(0, 10).forEach((row) => {
    const eventLabel = row.eventLabel || row.eventSlug || row.eventId || 'Event';
    const cancelled = row.status === 'DECLINED' && row.cancelledAt;
    const stamp = (cancelled && row.cancelledAt) || row.attendedAt || row.confirmedAt || row.declinedAt || row.invitedAt || '—';
    const status = cancelled
      ? (row.tierExcused ? 'Cancelled on time · excluded from tier rate' : 'Cancelled · counts toward tier rate')
      : (row.status || '—');
    container.appendChild(detailRow(eventLabel, `${status} · ${stamp}`));
  });
}

function memberMatchesFilters(member = {}, filters = currentMemberFilters()) {
  const haystack = [member.name, member.lastName, member.phone, member.email, member.instagram, getMarket(member)]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();

  if (filters.query && !haystack.includes(filters.query)) return false;
  return true;
}


function findMember(phone) {
  const normalized = normalizePhone(phone);
  return state.memberList.find((member) => normalizePhone(member.phone) === normalized)
    || state.filteredMembers.find((member) => normalizePhone(member.phone) === normalized)
    || null;
}

function detailRow(label, value) {
  const row = createNode('div', { className: 'member-detail-row' });
  appendChildren(
    row,
    createNode('span', { className: 'member-detail-label', text: label }),
    createNode('span', { className: 'member-detail-value', text: value || '—' }),
  );
  return row;
}

function detailControlRow(label, control) {
  const row = createNode('div', { className: 'member-detail-row member-detail-control-row' });
  appendChildren(
    row,
    createNode('span', { className: 'member-detail-label', text: label }),
    control,
  );
  return row;
}

function mergeMemberRecords(existing = {}, incoming = {}) {
  return {
    ...existing,
    ...incoming,
    phone: normalizePhone(incoming.phone || existing.phone),
    name: incoming.name ?? existing.name ?? '',
    lastName: incoming.lastName ?? existing.lastName ?? '',
  };
}

function dedupeMembersByPhone(members = []) {
  const byPhone = new Map();
  members.forEach((member) => {
    const phone = normalizePhone(member?.phone);
    if (!phone) return;
    const previous = byPhone.get(phone) || {};
    byPhone.set(phone, mergeMemberRecords(previous, member));
  });
  return [...byPhone.values()];
}

async function fetchMembersByStatus(status, options = {}) {
  const filters = options.useFilters ? currentMemberFilters() : {};
  const cursor = options.cursor || '';
  const path = buildMemberQuery(status, filters, {
    cursor,
    includeTotal: Boolean(options.includeTotal),
    limit: state.pageSize,
  });
  const payload = options.initialPayload && !cursor ? options.initialPayload : await apiJson(path);
  const members = dedupeMembersByPhone(payload?.members || []);
  const total = Number.isFinite(payload?.total) ? payload.total : null;
  return {
    members,
    total,
    nextPageToken: payload?.nextPageToken || '',
    hasMore: Boolean(payload?.hasMore || payload?.nextPageToken),
    pageSize: Number(payload?.pageSize || state.pageSize || 50),
  };
}

function requestedDateText(member) {
  return member.createdAt
    ? new Date(member.createdAt).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' })
    : '—';
}

function createGenderSelect(phone, value = '') {
  const select = createNode('select', {
    className: 'inline-select member-gender-select',
    dataset: { phone },
  });

  const normalized = String(value || '').trim().toUpperCase();

  if (!normalized) {
    const placeholder = createNode('option', {
      text: '—',
      attrs: { value: '', disabled: true, hidden: true },
    });
    placeholder.selected = true;
    select.appendChild(placeholder);
  }

  [
    ['F', 'F'],
    ['M', 'M'],
    ['O', 'O'],
  ].forEach(([optionValue, label]) => {
    const option = createNode('option', { text: label, attrs: { value: optionValue } });
    if (optionValue === normalized) option.selected = true;
    select.appendChild(option);
  });

  return select;
}

function createTierSelect(phone, value = '0') {
  const select = createNode('select', {
    className: 'inline-select member-tier-select',
    dataset: { phone },
  });

  [
    ['0', 'Auto'],
    ['1', 'T1'],
    ['2', 'T2'],
    ['3', 'T3'],
  ].forEach(([optionValue, label]) => {
    const option = createNode('option', { text: label, attrs: { value: optionValue } });
    if (optionValue === value) option.selected = true;
    select.appendChild(option);
  });

  return select;
}

function createActionButton({ label, className, dataset = {}, disabled = false }) {
  return createNode('button', {
    className: `action-btn ${className}`.trim(),
    text: label,
    dataset,
    attrs: { type: 'button', disabled },
  });
}

function createMemberRow(member) {
  const phone = normalizePhone(member.phone);
  const tr = createNode('tr', { className: 'member-row', dataset: { phone }, attrs: { id: `row-${sanitizePhoneId(phone)}` } });

  const nameTd = createNode('td');
  const fullName = [member.name || '', member.lastName || ''].filter(Boolean).join(' ') || '—';
  nameTd.appendChild(createNode('span', { className: 'member-name member-name-open', text: fullName, dataset: { phone }, attrs: { title: 'Double-click for member details', tabindex: '0', role: 'button' } }));

  const phoneTd = createNode('td');
  phoneTd.appendChild(createNode('span', { className: 'member-phone', text: phone || '—' }));

  const marketTd = createNode('td');
  marketTd.appendChild(createNode('span', { className: 'preview-market', text: getMarket(member) }));

  const requestedTd = createNode('td');
  requestedTd.appendChild(createNode('span', { className: 'member-date', text: requestedDateText(member) }));

  const genderTd = createNode('td');
  genderTd.appendChild(createNode('span', { className: 'member-source', text: String(member.gender || '—').toUpperCase() }));

  const tierTd = createNode('td');
  const tierValue = String(Number(member.tierOverride ?? member.tier ?? 0) || 0);
  tierTd.appendChild(createNode('span', { className: 'tier-pill', text: tierValue === '0' ? 'Auto' : `T${tierValue}` }));

  const statusTd = createNode('td');
  statusTd.appendChild(createNode('span', { className: `status-badge ${state.currentStatus || member.status || 'PENDING'}`, text: state.currentStatus || member.status || 'PENDING' }));

  const actionsTd = createNode('td');
  const actions = createNode('div', { className: 'action-btns' });
  if (state.currentStatus === 'PENDING' || state.currentStatus === 'DENIED') {
    actions.appendChild(createActionButton({
      label: 'Approve',
      className: 'approve member-status-btn',
      dataset: { phone, status: 'APPROVED' },
    }));
  }

  if (state.currentStatus === 'DENIED') {
    actions.appendChild(createActionButton({
      label: 'Pending',
      className: 'pending member-status-btn',
      dataset: { phone, status: 'PENDING' },
    }));
  }

  if (state.currentStatus === 'PENDING') {
    actions.appendChild(createActionButton({
      label: 'Deny',
      className: 'deny member-status-btn',
      dataset: { phone, status: 'DENIED' },
    }));
  }

  actionsTd.appendChild(actions);
  appendChildren(tr, nameTd, phoneTd, marketTd, requestedTd, genderTd, tierTd, statusTd, actionsTd);
  return tr;
}

function buildMemberTable(members = []) {
  const table = createNode('table', { className: 'member-table members-simple-table' });
  const thead = createNode('thead');
  const headerRow = createNode('tr');
  ['Name', 'Phone', 'Market', 'Requested', 'Gender', 'Tier', 'Status', 'Actions'].forEach((label) => {
    headerRow.appendChild(createNode('th', { text: label }));
  });
  thead.appendChild(headerRow);

  const tbody = createNode('tbody');
  members.forEach((member) => tbody.appendChild(createMemberRow(member)));
  appendChildren(table, thead, tbody);
  return table;
}

function paginationButton(label, page, { disabled = false, active = false } = {}) {
  return createNode('button', {
    className: `page-btn${active ? ' active' : ''}`,
    text: label,
    dataset: { page },
    attrs: { type: 'button', disabled },
  });
}

function renderPagination() {
  const paginationEl = clearNode('pagination');
  if (!paginationEl) return;
  const pager = state.memberPagination || { page: 1, hasMore: false };
  const hasPrev = Number(pager.page || 1) > 1;
  const hasNext = Boolean(pager.hasMore);
  if (!hasPrev && !hasNext) return;
  paginationEl.appendChild(paginationButton('‹ Previous', Number(pager.page || 1) - 1, { disabled: !hasPrev }));
  paginationEl.appendChild(createNode('span', { className: 'page-ellipsis', text: `Page ${pager.page || 1}` }));
  paginationEl.appendChild(paginationButton('Next ›', Number(pager.page || 1) + 1, { disabled: !hasNext }));
}

function updateSectionCount() {
  const pageCount = state.filteredMembers.length;
  const total = state.memberTotals[state.currentStatus];
  const pager = state.memberPagination || {};
  if (Number.isFinite(total) && total > 0) {
    $('section-count').textContent = `${total} total · page ${pager.page || 1}`;
    return;
  }
  $('section-count').textContent = pageCount ? `${pageCount} shown · page ${pager.page || 1}` : '';
}

function adjustLocalTotals(changes = {}) {
  Object.entries(changes).forEach(([status, delta]) => {
    const normalized = String(status || '').toUpperCase();
    if (!Object.prototype.hasOwnProperty.call(state.memberTotals, normalized)) return;
    const nextValue = Math.max(0, Number(state.memberTotals[normalized] || 0) + Number(delta || 0));
    state.memberTotals[normalized] = nextValue;
    setStatNode(normalized, nextValue);
  });
}

export async function loadMembers(status = 'PENDING', options = {}) {
  state.currentStatus = status;
  const searchInput = $('member-search');
  if (searchInput && !options.preserveFilters) searchInput.value = '';

  const filters = options.useFilters ? currentMemberFilters() : {};
  const key = memberPageKey(status, filters);
  const requestedPage = Math.max(1, Number(options.page || 1));
  if (!state.memberPagination || state.memberPagination.key !== key || requestedPage === 1) {
    state.memberPagination = { key, page: 1, tokens: [''], nextPageToken: '', hasMore: false };
  }
  const cursor = state.memberPagination.tokens[requestedPage - 1] || '';

  setActiveButton('.filter-btn', `filter-${status}`);
  $('section-title').textContent = STATUS_TITLES[status] || 'Members';
  const container = clearNode('members-container');
  container?.appendChild(loadingState());

  try {
    const { members, total, nextPageToken, hasMore } = await fetchMembersByStatus(status, {
      ...options,
      cursor,
      includeTotal: requestedPage === 1 && !options.useFilters,
    });
    state.memberPagination.page = requestedPage;
    state.memberPagination.nextPageToken = nextPageToken || '';
    state.memberPagination.hasMore = Boolean(hasMore);
    if (nextPageToken) state.memberPagination.tokens[requestedPage] = nextPageToken;
    if (Number.isFinite(total)) {
      state.memberTotals[status] = total;
      setStatNode(status, total);
    }
    setMembers(members);
    renderMembers(state.memberList);
  } catch {
    clearNode('members-container')?.appendChild(emptyState('Failed to load members.'));
    clearNode('pagination');
  }
}

export async function loadStats(options = {}) {
  const skipStatuses = new Set((options.skipStatuses || []).map((status) => String(status || '').toUpperCase()));
  const requestedStatuses = Array.isArray(options.onlyStatuses) && options.onlyStatuses.length
    ? options.onlyStatuses.map((status) => String(status || '').toUpperCase())
    : ['PENDING', 'APPROVED', 'DENIED'];
  const statuses = requestedStatuses.filter((status) => !skipStatuses.has(status));

  await Promise.all(
    statuses.map(async (status) => {
      try {
        const { total } = await fetchMembersByStatus(status, { includeTotal: true });
        state.memberTotals[status] = total;
        setStatNode(status, total);
      } catch {
        setStatNode(status, '—');
      }
    }),
  );
}

async function reconcileStats(statuses = []) {
  const onlyStatuses = [...new Set(statuses.map((status) => String(status || '').toUpperCase()).filter(Boolean))];
  if (!onlyStatuses.length) return;

  try {
    await loadStats({ onlyStatuses });
  } catch {
    // keep local totals if reconciliation fails
  }
}

export function renderMembers(members = []) {
  const sortedMembers = [...members].sort((a, b) => {
    const aLast = (a.lastName || a.name || '').toLowerCase();
    const bLast = (b.lastName || b.name || '').toLowerCase();
    const aFirst = (a.name || '').toLowerCase();
    const bFirst = (b.name || '').toLowerCase();
    return aLast < bLast ? -1 : aLast > bLast ? 1 : aFirst < bFirst ? -1 : aFirst > bFirst ? 1 : 0;
  });

  state.filteredMembers = sortedMembers;
  state.currentPage = state.memberPagination?.page || 1;
  const container = clearNode('members-container');
  updateSectionCount();

  if (!sortedMembers.length) {
    container?.appendChild(emptyState(`No ${state.currentStatus.toLowerCase()} members on this page.`));
    renderPagination();
    return;
  }

  if (container) {
    const wrap = createNode('div', { className: 'table-scroll-wrap member-table-scroll' });
    wrap.appendChild(buildMemberTable(sortedMembers));
    container.appendChild(wrap);
  }
  renderPagination();
}

export function goPage(page) {
  const target = Math.max(1, Number(page) || 1);
  if (target === state.memberPagination?.page) return;
  loadMembers(state.currentStatus, { page: target, preserveFilters: true, useFilters: hasActiveMemberFilters() });
}

export function filterMembers() {
  // Search is server-driven. While the debounce runs, keep the current page visible
  // rather than pretending local filtering represents the whole member table.
  renderMembers(state.memberList.filter((member) => memberMatchesFilters(member)));
}

let memberFilterTimer = null;
export function refreshMembersWithServerFilters() {
  const filters = currentMemberFilters();

  // Always give immediate local feedback while the debounced server refresh runs.
  filterMembers();

  window.clearTimeout(memberFilterTimer);
  memberFilterTimer = window.setTimeout(async () => {
    try {
      if (!hasActiveMemberFilters(filters)) {
        // Critical: if the previous request replaced state.memberList with a server-filtered
        // subset, clearing filters must reload the full status list from the backend.
        await loadMembers(state.currentStatus, { preserveFilters: true, useFilters: false });
        return;
      }

      await loadMembers(state.currentStatus, { preserveFilters: true, useFilters: true });
    } catch {
      filterMembers();
    }
  }, 350);
}


export async function openMemberDetail(phone) {
  const member = findMember(phone);
  if (!member) {
    showToast('Member not found in current list', 'error');
    return;
  }
  const normalizedPhone = normalizePhone(member.phone);
  const fullName = [member.name || '', member.lastName || ''].filter(Boolean).join(' ') || 'Unnamed member';
  const body = createNode('div', { className: 'member-detail-shell member-detail-compact' });

  const profile = createNode('div', { className: 'member-detail-grid member-detail-grid-primary' });
  appendChildren(
    profile,
    detailRow('Phone', normalizedPhone),
    detailRow('Status', member.status || state.currentStatus),
    detailControlRow('Gender', createGenderSelect(normalizedPhone, member.gender || '')),
    detailControlRow('Tier', createTierSelect(normalizedPhone, String(Number(member.tierOverride ?? member.tier ?? 0) || 0))),
    detailRow('Joined', requestedDateText(member)),
  );
  const profileInputs = {};
  [['market', 'Market'], ['zipCode', 'ZIP code (optional)'], ['city', 'City'], ['state', 'State'], ['email', 'Email'], ['instagram', 'Instagram']].forEach(([key, label]) => {
    const input = createNode('input', { attrs: { type: key === 'email' ? 'email' : 'text', 'aria-label': label, maxlength: '254' } });
    input.value = member[key] || '';
    profileInputs[key] = input;
    profile.appendChild(detailControlRow(label, input));
  });
  const saveProfile = createNode('button', { text: 'Save Profile', attrs: { type: 'button' } });
  saveProfile.onclick = async () => {
    saveProfile.disabled = true;
    try {
      const fields = Object.fromEntries(Object.entries(profileInputs).map(([key, input]) => [key, input.value.trim()]));
      const result = await apiJson(ROUTES.ADMIN_MEMBER_PROFILE, { method: 'POST', body: { phone: normalizedPhone, ...fields } });
      Object.assign(member, result.member || fields);
      state.memberList = state.memberList.map((row) => row.phone === normalizedPhone ? { ...row, ...member } : row);
      filterMembers();
      showToast('Profile saved', 'success');
    } catch (error) { showToast(error.message, 'error'); }
    finally { saveProfile.disabled = false; }
  };
  profile.appendChild(saveProfile);
  body.appendChild(profile);

  const historyDetails = createNode('details', { className: 'member-modal-details' });
  historyDetails.appendChild(createNode('summary', { text: 'Event History' }));
  const historyPanel = createNode('div', { className: 'member-intel-grid member-intel-grid-compact' });
  historyPanel.appendChild(createNode('p', { className: 'member-intel-note', text: 'Loading per-event history…' }));
  historyDetails.appendChild(historyPanel);
  body.appendChild(historyDetails);

  const intelDetails = createNode('details', { className: 'member-modal-details' });
  intelDetails.appendChild(createNode('summary', { text: 'Invite + Attendance' }));
  const intelGrid = createNode('div', { className: 'member-intel-grid member-intel-grid-compact' });
  memberHistoryRows(member).slice(0, 6).forEach(([label, value]) => intelGrid.appendChild(detailRow(label, value)));
  intelDetails.appendChild(intelGrid);
  body.appendChild(intelDetails);

  // Danger Zone is a plain section (not a collapsible "verify" step). The Delete
  // Member button opens the type-to-confirm modal directly — one step, not two.
  const danger = createNode('div', { className: 'member-modal-details member-danger-zone member-danger-open' });
  danger.appendChild(createNode('p', { className: 'member-danger-heading', text: 'Danger Zone' }));
  const dangerBody = createNode('div', { className: 'member-danger-body' });
  dangerBody.appendChild(createNode('p', { className: 'member-intel-note', text: 'Delete removes this member from operational lists and future invites.' }));
  const deleteButton = createActionButton({
    label: 'Delete Member',
    className: 'delete member-delete-btn',
    dataset: { phone: normalizedPhone },
  });
  dangerBody.appendChild(deleteButton);
  danger.appendChild(dangerBody);
  body.appendChild(danger);

  // Centered modal in the middle of the screen (was a side drawer pinned to the edge).
  openModal({
    kicker: 'Member Profile',
    title: fullName,
    body,
    actions: [],
  });

  try {
    const data = await apiJson(`${ROUTES.ADMIN_MEMBER_HISTORY}?phone=${encodeURIComponent(normalizedPhone)}`);
    renderMemberHistoryRows(historyPanel, data.history || []);
  } catch (error) {
    clearNode(historyPanel);
    historyPanel.appendChild(createNode('p', { className: 'member-intel-note', text: `History unavailable: ${error.message}` }));
  }
}

export async function updateGender(phone, gender) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return;

  try {
    await apiJson(ROUTES.ADMIN_MEMBER_GENDER, {
      method: 'POST',
      body: { phone: normalizedPhone, gender },
    });

    state.memberList = state.memberList.map((member) => (
      normalizePhone(member.phone) === normalizedPhone ? { ...member, gender } : member
    ));
    state.filteredMembers = state.filteredMembers.map((member) => (
      normalizePhone(member.phone) === normalizedPhone ? { ...member, gender } : member
    ));
    showToast('Gender updated', 'success');
  } catch (error) {
    showToast(`Failed to update gender: ${error.message}`, 'error');
  }
}

export async function updateTier(phone, tierOverride) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return;

  try {
    await apiJson(ROUTES.ADMIN_MEMBER_TIER, {
      method: 'POST',
      body: { phone: normalizedPhone, tier: Number(tierOverride) || 0 },
    });

    state.memberList = state.memberList.map((member) => (
      normalizePhone(member.phone) === normalizedPhone ? { ...member, tierOverride: Number(tierOverride) || 0, tier: Number(tierOverride) || 0 } : member
    ));
    state.filteredMembers = state.filteredMembers.map((member) => (
      normalizePhone(member.phone) === normalizedPhone ? { ...member, tierOverride: Number(tierOverride) || 0, tier: Number(tierOverride) || 0 } : member
    ));
    showToast('Tier updated', 'success');
  } catch (error) {
    showToast(`Failed to update tier: ${error.message}`, 'error');
  }
}

export async function setStatus(phone, status) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return;

  try {
    await apiJson(ROUTES.ADMIN_MEMBER_STATUS, {
      method: 'POST',
      body: { phone: normalizedPhone, status },
    });

    const previousStatus = state.currentStatus;
    state.memberList = state.memberList.filter((member) => normalizePhone(member.phone) !== normalizedPhone);
    state.filteredMembers = state.filteredMembers.filter((member) => normalizePhone(member.phone) !== normalizedPhone);

    adjustLocalTotals({
      [previousStatus]: -1,
      [status]: 1,
    });

    renderMembers(state.filteredMembers, state.currentPage);
    await reconcileStats([previousStatus, status]);
    showToast(`Moved to ${status.toLowerCase()}`, 'success');
  } catch (error) {
    showToast(`Failed to update status: ${error.message}`, 'error');
  }
}

export async function deleteMember(phone) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return;
  const member = findMember(normalizedPhone) || { phone: normalizedPhone };
  const label = [member.name, member.lastName].filter(Boolean).join(' ') || normalizedPhone;
  const body = createNode('div', { className: 'send-confirm-body danger-confirm-body' });
  appendChildren(
    body,
    createNode('p', { className: 'modal-copy send-confirm-copy', text: 'This will soft-delete the member and remove them from operational lists.' }),
    detailRow('Member', label),
    detailRow('Phone', normalizedPhone),
    detailRow('Status', member.status || state.currentStatus),
    createNode('p', { className: 'send-confirm-warning', text: 'Operator check: delete only if this member should no longer receive access or invites.' }),
    createNode('label', { className: 'danger-confirm-label', text: `Type ${normalizedPhone} to delete this member.` }),
  );
  const input = createNode('input', { className: 'danger-confirm-input', attrs: { type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: normalizedPhone } });
  body.appendChild(input);

  const cancel = createNode('button', { className: 'btn-secondary', text: 'Cancel', attrs: { type: 'button' } });
  cancel.addEventListener('click', closeModal);
  const confirm = createNode('button', { className: 'btn-primary-muted danger', text: 'Delete Member', attrs: { type: 'button', disabled: true } });
  input.addEventListener('input', () => { confirm.disabled = input.value.trim() !== normalizedPhone; });
  confirm.addEventListener('click', async () => {
    if (input.value.trim() !== normalizedPhone) return;
    confirm.disabled = true;
    confirm.textContent = 'Deleting...';
    try {
      await apiJson(ROUTES.ADMIN_MEMBERS, {
        method: 'DELETE',
        body: { phone: normalizedPhone, confirmPhone: normalizedPhone },
      });

      state.memberList = state.memberList.filter((m) => normalizePhone(m.phone) !== normalizedPhone);
      state.filteredMembers = state.filteredMembers.filter((m) => normalizePhone(m.phone) !== normalizedPhone);

      adjustLocalTotals({ [state.currentStatus]: -1 });
      renderMembers(state.filteredMembers, state.currentPage);
      await reconcileStats([state.currentStatus]);
      closeModal();
      closeDrawer();
      showToast('Member deleted', 'success');
    } catch (error) {
      confirm.disabled = false;
      confirm.textContent = 'Delete Member';
      showToast(`Failed to delete member: ${error.message}`, 'error');
    }
  });

  openModal({
    kicker: 'Confirm Delete',
    title: 'Delete this member?',
    body,
    actions: [cancel, confirm],
  });
}
