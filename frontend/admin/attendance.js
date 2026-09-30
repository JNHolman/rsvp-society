import { apiJson, fetchAllPages } from './api.js';
import { ROUTES } from './constants.js';
import { $, appendChildren, clearNode, createNode, emptyState, loadingState, sanitizePhoneId, showToast } from './ui.js';

function attendanceActionButton(member, attended, eventId, guestType = 'member') {
  const isPlusOne = guestType === 'plus_one';
  const isCheckedIn = isPlusOne ? Boolean(member.plusOneCheckedIn) : Boolean(member.checkedIn);
  const isDisabled = isPlusOne ? (isCheckedIn || !attended) : ((attended && isCheckedIn) || (!attended && isCheckedIn));
  return createNode('button', {
    className: `action-btn ${attended ? 'approve att-btn-yes' : 'deny att-btn-no'} attendance-action-btn${isDisabled ? ' is-disabled-soft' : ''}`,
    text: attended ? (isCheckedIn ? (isPlusOne ? '✓ +1 In' : '✓ In') : (isPlusOne ? 'Check +1' : 'Attended')) : 'No Show',
    dataset: {
      phone: member.phone || '',
      sponsorPhone: member.phone || '',
      attended: attended ? 'true' : 'false',
      eventId: eventId || '',
      guestType,
    },
    attrs: { type: 'button', disabled: isDisabled },
  });
}

function historyText(member) {
  if (member.checkedIn) return '✓ Checked in';
  if (member.confirmedAt) return 'Confirmed';
  return 'Invited';
}

function buildAttendanceTable(members, eventId) {
  const table = createNode('table', { className: 'member-table attendance-table' });
  const thead = createNode('thead');
  const headerRow = createNode('tr');
  ['Guest', 'Phone / Sponsor', 'History', 'Mark'].forEach((label) => headerRow.appendChild(createNode('th', { text: label })));
  thead.appendChild(headerRow);

  const tbody = createNode('tbody');
  const appendMemberRow = (member) => {
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
    const actions = createNode('div', { className: 'action-btns attendance-action-btns' });
    appendChildren(actions, attendanceActionButton(member, true, eventId), attendanceActionButton(member, false, eventId));
    actionTd.appendChild(actions);

    appendChildren(row, nameTd, phoneTd, historyTd, actionTd);
    tbody.appendChild(row);
  };

  const appendPlusOneRow = (member) => {
    if (!member.plusOneName) return;
    const row = createNode('tr', { className: 'attendance-plus-one-row', attrs: { id: `att-plusone-row-${sanitizePhoneId(member.phone)}` } });
    const nameTd = createNode('td');
    nameTd.appendChild(createNode('span', { className: 'member-name', text: `+1: ${member.plusOneName}` }));
    const sponsorTd = createNode('td');
    sponsorTd.appendChild(createNode('span', { className: 'member-phone', text: `Sponsor ${member.phone || '—'}` }));
    const historyTd = createNode('td');
    historyTd.appendChild(createNode('span', { className: 'member-date', text: member.plusOneCheckedIn ? '✓ Checked in' : 'Confirmed +1' }));
    const actionTd = createNode('td');
    const actions = createNode('div', { className: 'action-btns attendance-action-btns' });
    actions.appendChild(attendanceActionButton(member, true, eventId, 'plus_one'));
    actionTd.appendChild(actions);
    appendChildren(row, nameTd, sponsorTd, historyTd, actionTd);
    tbody.appendChild(row);
  };

  members.forEach((member) => {
    appendMemberRow(member);
    appendPlusOneRow(member);
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

export async function markAttendance(phone, attended, eventId, guestType = 'member', sponsorPhone = '') {
  const row = document.getElementById(`${guestType === 'plus_one' ? 'att-plusone-row' : 'att-row'}-${sanitizePhoneId(phone)}`);
  if (row) row.classList.add('is-busy');

  try {
    const data = await apiJson(ROUTES.ADMIN_MEMBER_ATTENDANCE, {
      method: 'POST',
      body: { phone, sponsorPhone: sponsorPhone || phone, attended, eventId, guestType },
    });

    // Backend returns alreadyCheckedIn=true when no-show is blocked
    // because the member was already physically checked in.
    if (!attended && data.alreadyCheckedIn) {
      if (row) row.classList.remove('is-busy');
      showToast('Already checked in — can\'t mark no show', 'error');
      return;
    }

    showToast(`Marked ${guestType === 'plus_one' ? '+1 ' : ''}${attended ? 'attended' : 'no show'}`, attended ? 'success' : '');
    window.dispatchEvent(new CustomEvent('rsvp:attendance-updated', { detail: { eventId } }));
    if (row) row.remove();
  } catch (error) {
    if (row) row.classList.remove('is-busy');
    showToast(`Failed to mark attendance: ${error.message || 'try again'}`, 'error');
  }
}
