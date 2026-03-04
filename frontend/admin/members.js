import { apiJson, fetchAllPages } from './api.js';
import { ROUTES } from './constants.js';
import { setMembers, state } from './state.js';
import {
  $,
  appendChildren,
  clearNode,
  createNode,
  emptyState,
  loadingState,
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
  const encodedStatus = encodeURIComponent(status);
  const fetchOptions = {};

  if (options.initialPayload) {
    fetchOptions.initialPages = [options.initialPayload];
    fetchOptions.initialPath = `${ROUTES.ADMIN_MEMBERS}?status=${encodedStatus}`;
  }

  const { pages, items } = await fetchAllPages(`${ROUTES.ADMIN_MEMBERS}?status=${encodedStatus}`, (payload) => payload?.members || [], fetchOptions);
  const firstPage = pages[0] || options.initialPayload || {};
  const members = dedupeMembersByPhone(items);
  const total = Math.max(getMemberCount(firstPage), members.length);
  return { members, total };
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
  const tr = createNode('tr', { attrs: { id: `row-${sanitizePhoneId(phone)}` } });

  const nameTd = createNode('td');
  const fullName = [member.name || '', member.lastName || ''].filter(Boolean).join(' ') || '—';
  nameTd.appendChild(createNode('span', { className: 'member-name', text: fullName }));

  const phoneTd = createNode('td');
  phoneTd.appendChild(createNode('span', { className: 'member-phone', text: phone || '—' }));

  const optInTd = createNode('td');
  optInTd.appendChild(createNode('span', {
    className: `optin-dot${member.smsOptIn ? '' : ' no'}`,
    attrs: { title: member.smsOptIn ? 'SMS opted in' : 'No SMS opt-in' },
  }));

  const genderTd = createNode('td');
  genderTd.appendChild(createGenderSelect(phone, member.gender || ''));

  const tierTd = createNode('td');
  tierTd.appendChild(createTierSelect(phone, String(member.tierOverride ?? member.tier ?? '0')));

  const requestedTd = createNode('td');
  requestedTd.appendChild(createNode('span', { className: 'member-date', text: requestedDateText(member) }));

  const statusTd = createNode('td');
  statusTd.appendChild(createNode('span', {
    className: `status-badge ${member.status || state.currentStatus}`,
    text: member.status || state.currentStatus,
  }));

  const actionsTd = createNode('td');
  const actions = createNode('div', { className: 'action-btns' });

  if (state.currentStatus === 'PENDING') {
    appendChildren(
      actions,
      createActionButton({
        label: 'Approve',
        className: 'approve member-status-btn',
        dataset: { phone, status: 'APPROVED' },
      }),
      createActionButton({
        label: 'Deny',
        className: 'deny member-status-btn',
        dataset: { phone, status: 'DENIED' },
      }),
    );
  } else {
    appendChildren(
      actions,
      createActionButton({
        label: state.currentStatus === 'APPROVED' ? 'Move to Pending' : 'Restore',
        className: 'pending member-status-btn',
        dataset: { phone, status: 'PENDING' },
      }),
    );
  }

  actions.appendChild(createActionButton({
    label: 'Delete',
    className: 'delete member-delete-btn',
    dataset: { phone },
  }));

  actionsTd.appendChild(actions);
  appendChildren(tr, nameTd, phoneTd, optInTd, genderTd, tierTd, requestedTd, statusTd, actionsTd);
  return tr;
}

function buildMemberTable(members = []) {
  const table = createNode('table', { className: 'member-table' });
  const thead = createNode('thead');
  const headerRow = createNode('tr');
  ['Name', 'Phone', 'SMS', 'Gender', 'Tier', 'Requested', 'Status', 'Actions'].forEach((label) => {
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

function renderPagination(totalPages) {
  const paginationEl = clearNode('pagination');
  if (!paginationEl || totalPages <= 1) return;

  paginationEl.appendChild(paginationButton('‹', state.currentPage - 1, { disabled: state.currentPage === 1 }));

  const pageNumbers = new Set([1, totalPages]);
  for (let page = Math.max(1, state.currentPage - 2); page <= Math.min(totalPages, state.currentPage + 2); page += 1) {
    pageNumbers.add(page);
  }

  const sorted = [...pageNumbers].sort((a, b) => a - b);
  let previous = 0;

  sorted.forEach((page) => {
    if (previous && page - previous > 1) {
      paginationEl.appendChild(createNode('span', { className: 'page-ellipsis', text: '…' }));
    }
    paginationEl.appendChild(paginationButton(String(page), page, { active: page === state.currentPage }));
    previous = page;
  });

  paginationEl.appendChild(paginationButton('›', state.currentPage + 1, { disabled: state.currentPage === totalPages }));
}

function updateSectionCount() {
  const count = state.filteredMembers.length;
  $('section-count').textContent = count ? `${count} member${count !== 1 ? 's' : ''}` : '';
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
  if (searchInput) searchInput.value = '';

  setActiveButton('.filter-btn', `filter-${status}`);
  $('section-title').textContent = STATUS_TITLES[status] || 'Members';
  const container = clearNode('members-container');
  container?.appendChild(loadingState());

  try {
    const { members, total } = await fetchMembersByStatus(status, options);
    state.memberTotals[status] = total;
    setStatNode(status, total);
    setMembers(members);
    renderMembers(state.memberList);
  } catch {
    clearNode('members-container')?.appendChild(emptyState('Failed to load members.'));
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
        const { total } = await fetchMembersByStatus(status);
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

export function renderMembers(members = [], page = 1) {
  const sortedMembers = [...members].sort((a, b) => {
    const aLast = (a.lastName || a.name || '').toLowerCase();
    const bLast = (b.lastName || b.name || '').toLowerCase();
    const aFirst = (a.name || '').toLowerCase();
    const bFirst = (b.name || '').toLowerCase();
    return aLast < bLast ? -1 : aLast > bLast ? 1 : aFirst < bFirst ? -1 : aFirst > bFirst ? 1 : 0;
  });

  state.filteredMembers = sortedMembers;
  state.currentPage = page;

  const totalPages = Math.ceil(sortedMembers.length / state.pageSize) || 1;
  state.currentPage = Math.min(Math.max(state.currentPage, 1), totalPages);

  const startIndex = (state.currentPage - 1) * state.pageSize;
  const paginatedMembers = sortedMembers.slice(startIndex, startIndex + state.pageSize);
  const container = clearNode('members-container');
  updateSectionCount();

  if (!sortedMembers.length) {
    container?.appendChild(emptyState(`No ${state.currentStatus.toLowerCase()} members.`));
    clearNode('pagination');
    return;
  }

  container?.appendChild(buildMemberTable(paginatedMembers));
  renderPagination(totalPages);
}

export function goPage(page) {
  renderMembers(state.filteredMembers, Number(page));
}

export function filterMembers(query) {
  const normalizedQuery = String(query || '').trim().toLowerCase();
  if (!normalizedQuery) {
    renderMembers(state.memberList, 1);
    return;
  }

  const filtered = state.memberList.filter((member) => {
    const haystack = [member.name, member.lastName, member.phone, member.email, member.instagram]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(normalizedQuery);
  });

  renderMembers(filtered, 1);
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

  if (!window.confirm(`Delete member ${normalizedPhone}?`)) return;

  try {
    await apiJson(ROUTES.ADMIN_MEMBERS, {
      method: 'DELETE',
      body: { phone: normalizedPhone },
    });

    state.memberList = state.memberList.filter((member) => normalizePhone(member.phone) !== normalizedPhone);
    state.filteredMembers = state.filteredMembers.filter((member) => normalizePhone(member.phone) !== normalizedPhone);

    adjustLocalTotals({ [state.currentStatus]: -1 });
    renderMembers(state.filteredMembers, state.currentPage);
    await reconcileStats([state.currentStatus]);
    showToast('Member deleted', 'success');
  } catch (error) {
    showToast(`Failed to delete member: ${error.message}`, 'error');
  }
}
