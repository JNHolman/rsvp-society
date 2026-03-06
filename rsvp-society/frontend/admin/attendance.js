import { apiJson, fetchAllPages } from './api.js';
import { ROUTES } from './constants.js';
import { $, appendChildren, clearNode, createNode, emptyState, loadingState, sanitizePhoneId, showToast } from './ui.js';

function attendanceActionButton(member, attended, eventId) {
  return createNode('button', {
    className: `action-btn ${attended ? 'approve att-btn-yes' : 'deny att-btn-no'} attendance-action-btn${attended && member.checkedIn ? ' is-disabled-soft' : ''}`,
    text: attended ? 'Attended' : 'No Show',
    dataset: {
      phone: member.phone || '',
      attended: attended ? 'true' : 'false',
      eventId: eventId || '',
    },
    attrs: { type: 'button', disabled: attended && member.checkedIn },
  });
}

function historyText(member) {
  if (member.checkedIn) return '✓ Checked in';
  if (member.confirmedAt) return 'Confirmed';
  return 'Invited';
}

function buildAttendanceTable(members, eventId) {
  const table = createNode('table', { className: 'member-table' });
  const thead = createNode('thead');
  const headerRow = createNode('tr');
  ['Name', 'Phone', 'History', 'Mark'].forEach((label) => headerRow.appendChild(createNode('th', { text: label })));
  thead.appendChild(headerRow);

  const tbody = createNode('tbody');
  members.forEach((member) => {
    const row = createNode('tr', { attrs: { id: `att-row-${sanitizePhoneId(member.phone)}` } });

    const nameTd = createNode('td');
    nameTd.appendChild(createNode('span', {
      className: 'member-name',
      text: [member.name || '', member.lastName || ''].filter(Boolean).join(' ') || '—',
    }));

    const phoneTd = createNode('td');
    phoneTd.appendChild(createNode('span', { className: 'member-phone', text: member.phone || '—' }));

    const historyTd = createNode('td');
    historyTd.appendChild(createNode('span', { className: 'member-date', text: historyText(member) }));

    const actionTd = createNode('td');
    const actions = createNode('div', { className: 'action-btns' });
    appendChildren(actions, attendanceActionButton(member, true, eventId), attendanceActionButton(member, false, eventId));
    actionTd.appendChild(actions);

    appendChildren(row, nameTd, phoneTd, historyTd, actionTd);
    tbody.appendChild(row);
  });

  appendChildren(table, thead, tbody);
  return table;
}

export async function loadAttendance() {
  const eventId = $('att-event-id').value.trim();
  if (!eventId) {
    showToast('Enter an event ID', 'error');
    return;
  }

  clearNode('attendance-container')?.appendChild(loadingState('Loading invitees...'));

  try {
    const { items } = await fetchAllPages(
      `${ROUTES.ADMIN_MEMBER_CONFIRMED}?eventId=${encodeURIComponent(eventId)}`,
      (payload) => payload?.members || [],
    );
    const byPhone = new Map();

    items.forEach((member) => {
      const phone = String(member?.phone || '').trim();
      if (!phone) return;
      const previous = byPhone.get(phone) || {};
      byPhone.set(phone, { ...previous, ...member, phone });
    });

    const members = [...byPhone.values()];
    const container = clearNode('attendance-container');

    if (!members.length) {
      container?.appendChild(emptyState('No confirmed members found for this event.'));
      return;
    }

    container?.appendChild(buildAttendanceTable(members, eventId));
  } catch {
    clearNode('attendance-container')?.appendChild(emptyState('Failed to load members.'));
  }
}

export async function markAttendance(phone, attended, eventId) {
  const row = document.getElementById(`att-row-${sanitizePhoneId(phone)}`);
  if (row) row.classList.add('is-busy');

  try {
    await apiJson(ROUTES.ADMIN_MEMBER_ATTENDANCE, {
      method: 'POST',
      body: { phone, attended, eventId },
    });
    showToast(`Marked ${attended ? 'attended' : 'no show'}`, attended ? 'success' : '');
    if (row) row.remove();
  } catch {
    if (row) row.classList.remove('is-busy');
    showToast('Failed to mark attendance', 'error');
  }
}
