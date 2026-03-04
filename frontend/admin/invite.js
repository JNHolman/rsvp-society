import { apiJson } from './api.js';
import { AREA_TO_STATE, REMINDER_MODES, ROUTES } from './constants.js';
import { rememberEvent, resetPreviewState, state } from './state.js';
import { $, appendChildren, clearNode, createNode, emptyState, sanitizePhoneId, showToast } from './ui.js';

function setSendButtonReady(isReady) {
  const sendButton = $('send-btn');
  if (!sendButton) return;
  sendButton.disabled = !isReady;
  sendButton.textContent = 'Send Invites';
  sendButton.classList.toggle('ready', isReady);
}

function paragraphWithStrongParts(parts = []) {
  const wrapper = document.createDocumentFragment();
  parts.forEach((part) => {
    if (typeof part === 'string') {
      wrapper.appendChild(document.createTextNode(part));
      return;
    }
    const node = createNode(part.tag || 'strong', { className: part.className || '', text: part.text || '' });
    wrapper.appendChild(node);
  });
  return wrapper;
}

function createGapFeedback(message) {
  return createNode('div', { className: 'gap-feedback', text: message });
}

function createPreviewEmpty(message) {
  return createNode('div', { className: 'analytics-empty analytics-empty-padded', text: message });
}

function createPreviewStat(value, label, extraClass = '') {
  const stat = createNode('div', { className: 'preview-stat' });
  appendChildren(
    stat,
    createNode('div', { className: `preview-stat-num ${extraClass}`.trim(), text: value }),
    createNode('div', { className: 'preview-stat-label', text: label }),
  );
  return stat;
}

function createInviteEventSummary(event, capacity) {
  const card = createNode('div', { className: 'invite-event-card' });
  card.appendChild(createNode('div', { className: 'invite-event-kicker', text: 'Current Event' }));
  const grid = createNode('div', { className: 'invite-event-grid' });

  const addItem = (label, valueNode) => {
    const item = createNode('div');
    appendChildren(
      item,
      createNode('div', { className: 'invite-event-item-label', text: label }),
      valueNode,
    );
    grid.appendChild(item);
  };

  if (event.date) addItem('DATE', createNode('div', { className: 'invite-event-item-value', text: event.date }));

  if (event.venue) {
    const venueValue = createNode('div', { className: 'invite-event-item-value' });
    venueValue.appendChild(document.createTextNode(event.venue));
    venueValue.appendChild(document.createTextNode(' '));
    venueValue.appendChild(createNode('span', {
      className: event.revealVenue ? 'invite-venue-revealed' : 'invite-venue-hidden',
      text: event.revealVenue ? 'revealed' : 'hidden',
    }));
    addItem('VENUE', venueValue);
  }

  if (event.vibe_tag) addItem('VIBE', createNode('div', { className: 'invite-event-item-value', text: event.vibe_tag }));
  if (capacity) addItem('CAPACITY', createNode('div', { className: 'invite-event-item-value', text: capacity }));

  card.appendChild(grid);
  return card;
}

function createGapStat(value, label, extraClass = '') {
  const stat = createNode('div', { className: 'gap-stat' });
  appendChildren(
    stat,
    createNode('div', { className: `gap-stat-num ${extraClass}`.trim(), text: value }),
    createNode('div', { className: 'gap-stat-label', text: label }),
  );
  return stat;
}

function createMarketPills(members = []) {
  const counts = {};
  members.forEach((member) => {
    const market = getState(member.phone);
    counts[market] = (counts[market] || 0) + 1;
  });

  const row = createNode('div', { className: 'preview-market-filter-row' });
  row.appendChild(createNode('span', { className: 'preview-market-filter-label', text: 'Filter' }));

  ['All', ...Object.keys(counts).sort()].forEach((market) => {
    const count = market === 'All' ? members.length : counts[market];
    const button = createNode('button', {
      className: `market-pill${market === 'All' ? ' is-active' : ''}`,
      text: market,
      dataset: { market },
      attrs: { type: 'button' },
    });
    button.appendChild(document.createTextNode(' '));
    button.appendChild(createNode('span', { className: 'market-pill-count', text: count }));
    row.appendChild(button);
  });

  return row;
}

function createGapNote(gap) {
  if (!gap) return null;
  const wrapper = createNode('div', { className: 'preview-gap-note' });
  appendChildren(
    wrapper,
    createNode('span', { className: 'preview-gap-kicker', text: 'Gap · ' }),
    document.createTextNode('Confirmed: '),
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
  const table = createNode('table', { className: 'member-table' });
  const thead = createNode('thead');
  const headRow = createNode('tr');
  ['Name', 'Phone', 'Gender', 'Tier', 'Attendance', 'Market', ''].forEach((label) => headRow.appendChild(createNode('th', { text: label })));
  thead.appendChild(headRow);

  const tbody = createNode('tbody');
  members.forEach((member) => {
    const phone = String(member.phone || '').trim();
    const market = getState(phone);
    const row = createNode('tr', {
      attrs: { id: `preview-row-${sanitizePhoneId(phone)}` },
      dataset: { phone, state: market },
    });

    const cells = [
      createNode('td'),
      createNode('td'),
      createNode('td'),
      createNode('td'),
      createNode('td'),
      createNode('td'),
      createNode('td'),
    ];

    cells[0].appendChild(createNode('span', { className: 'member-name', text: member.name || '—' }));
    cells[1].appendChild(createNode('span', { className: 'member-phone', text: phone || '—' }));
    cells[2].appendChild(createNode('span', { className: 'member-source', text: member.gender || '?' }));
    cells[3].appendChild(createNode('span', { className: 'status-badge PENDING', text: `Tier ${Number(member.tier) || 0}` }));
    cells[4].appendChild(createNode('span', { className: 'member-date', text: `${Number(member.attendedCount) || 0}/${Number(member.invitedCount) || 0} attended` }));
    cells[5].appendChild(createNode('span', { className: 'preview-market', text: market }));
    cells[6].appendChild(createNode('button', {
      className: 'action-btn delete preview-remove-btn',
      text: 'Remove',
      dataset: { phone },
      attrs: { type: 'button' },
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

export function getState(phone) {
  const digits = (phone || '').replace(/\D/g, '');
  const area = digits.length >= 10 ? digits.slice(digits.length - 10, digits.length - 7) : '???';
  return AREA_TO_STATE[area] || `+${area}`;
}

export function removeFromPreview(phone) {
  state.preview.removedPhones.add(phone);
  const row = document.getElementById(`preview-row-${sanitizePhoneId(phone)}`);
  if (row) row.hidden = true;
  updatePreviewCount();
}

export function updatePreviewCount() {
  const remaining = document.querySelectorAll('#preview-table-container tbody tr:not([hidden])').length;
  const countNode = $('preview-remaining-count');
  if (countNode) countNode.textContent = remaining;
  setSendButtonReady(Boolean(state.preview.lastPreview) && remaining > 0);
}

export function filterByMarket(market) {
  state.preview.activeMarketFilter = market;

  document.querySelectorAll('.market-pill').forEach((pill) => {
    pill.classList.toggle('is-active', pill.dataset.market === market);
  });

  document.querySelectorAll('#preview-table-container tbody tr').forEach((row) => {
    if (market === 'All') {
      row.hidden = state.preview.removedPhones.has(row.dataset.phone);
      return;
    }
    row.hidden = !(row.dataset.state === market && !state.preview.removedPhones.has(row.dataset.phone));
  });

  updatePreviewCount();
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

    if (eventId && previousEventId !== eventId) await onWaveChange();
  } catch {
    // preserve current UI behavior
  }
}

export async function onWaveChange() {
  const wave = parseInt($('inv-wave-number').value, 10) || 1;
  const panel = $('wave-gap-panel');
  const waveSizeInput = $('inv-wave-size');
  const gapContent = clearNode('wave-gap-content');
  const gapSuggestion = clearNode('wave-gap-suggestion');

  if (wave === 1) {
    panel.classList.remove('is-visible');
    waveSizeInput.value = '';
    waveSizeInput.placeholder = 'Auto';
    return;
  }

  const eventId = $('inv-event-id').value.trim();
  const capacity = parseInt($('inv-capacity').value, 10) || 0;
  if (!eventId || !capacity) {
    panel.classList.remove('is-visible');
    return;
  }

  panel.classList.add('is-visible');
  gapContent?.appendChild(createGapFeedback('Loading...'));

  try {
    const data = await apiJson(`${ROUTES.ADMIN_EVENT_ANALYTICS}?eventId=${encodeURIComponent(eventId)}`);
    renderGapPanel(capacity, data.analytics?.totals || {}, wave);
  } catch {
    clearNode('wave-gap-content')?.appendChild(createGapFeedback('Could not load analytics — enter Wave Size manually'));
  }
}

export function renderGapPanel(capacity, totals, wave) {
  const SHOW_RATE = 0.60;
  const invited = Number(totals.invited) || 0;
  const confirmed = Number(totals.confirmed) || 0;
  const confirmRate = invited > 0 ? confirmed / invited : 0.30;
  const targetConfirmed = Math.ceil(capacity / SHOW_RATE);
  const gap = Math.max(0, targetConfirmed - confirmed);
  const suggested = gap > 0 ? Math.ceil(gap / (confirmRate || 0.30)) : 0;

  const confirmRatePct = Math.round((confirmRate || 0.30) * 100);
  const isRealRate = invited > 0;

  const content = clearNode('wave-gap-content');
  if (content) {
    appendChildren(
      content,
      createGapStat(confirmed, 'Confirmed'),
      createGapStat(targetConfirmed, 'Need Confirmed', 'is-gold'),
      createGapStat(gap, 'Conf. Gap', gap > 0 ? 'is-danger' : 'is-success'),
      createGapStat(suggested, 'Suggested Invites', 'is-gold'),
    );
  }

  const suggestion = clearNode('wave-gap-suggestion');
  if (suggestion) {
    if (isRealRate) {
      suggestion.appendChild(paragraphWithStrongParts([
        `Your actual confirm rate is ${confirmRatePct}% (${confirmed} confirmed / ${invited} invited). At ${SHOW_RATE * 100}% show rate you need `,
        { className: 'gap-copy-strong', text: `${targetConfirmed} confirmed` },
        ` to fill ${capacity} seats. `,
        ...(gap > 0
          ? [
            "You're ",
            { className: 'gap-copy-strong is-danger', text: `${gap} short` },
            ' — sending ',
            { className: 'gap-copy-strong is-gold', text: `${suggested} more invites` },
            ' should close the gap.',
          ]
          : [
            { className: 'gap-copy-strong is-success', text: "You're on track" },
            ' — no additional invites needed to fill the room.',
          ]),
      ]));
    } else {
      suggestion.appendChild(paragraphWithStrongParts([
        `No invite data yet — using default 30% confirm rate. At ${SHOW_RATE * 100}% show rate you need `,
        { className: 'gap-copy-strong', text: `${targetConfirmed} confirmed` },
        ` to fill ${capacity} seats.`,
      ]));
    }
  }

  if (wave > 1) {
    $('inv-wave-size').value = suggested > 0 ? String(suggested) : '';
    $('inv-wave-size').placeholder = suggested > 0 ? String(suggested) : 'Auto';
  }
}

export async function runPreview() {
  const eventId = $('inv-event-id').value.trim();
  const capacity = parseInt($('inv-capacity').value, 10) || 0;
  const femalePercent = parseInt($('inv-female-pct').value, 10) || 60;
  const tier2BufferPct = parseInt($('inv-buffer').value, 10) || 30;
  const waveNumber = parseInt($('inv-wave-number').value, 10) || 1;
  const waveSizeRaw = parseInt($('inv-wave-size').value, 10);
  const waveSize = waveNumber > 1 && Number.isFinite(waveSizeRaw) ? waveSizeRaw : 0;

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
        tier2BufferPct,
        waveNumber,
        waveSize,
      },
    });

    state.preview.lastPreview = { eventId, capacity, femalePercent, tier2BufferPct, waveNumber, waveSize };
    state.preview.members = Array.isArray(data.members) ? data.members : [];
    state.preview.removedPhones = new Set();
    state.preview.activeMarketFilter = 'All';

    renderPreview(data);
    setSendButtonReady(state.preview.members.length > 0);
    showToast(`Preview ready — Wave ${waveNumber}`);
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
  const box = $('preview-box');
  box.classList.add('visible');

  const stats = clearNode('preview-stats');
  if (stats) {
    appendChildren(
      stats,
      createPreviewStat(summary.totalInvites, `Wave ${summary.waveNumber} Selected`),
      createPreviewStat(summary.breakdown.femTier1 + summary.breakdown.femTier2, `Female (${summary.femalePercent}%)`, 'gold'),
      createPreviewStat(summary.breakdown.maleTier1 + summary.breakdown.maleTier2, `Male (${summary.malePercent}%)`),
      createPreviewStat(summary.breakdown.tier3Skipped, 'Tier 3 Skipped', 'red'),
    );
    const gapNote = createGapNote(summary.gapAnalysis);
    if (gapNote) stats.appendChild(gapNote);
    stats.appendChild(createMarketPills(members));
  }

  const tableContainer = clearNode('preview-table-container');
  tableContainer?.appendChild(createPreviewTable(members));
  updatePreviewCount();
}

export async function sendInvites() {
  const preview = state.preview.lastPreview;
  if (!preview) {
    showToast('Run preview first', 'error');
    return;
  }

  const visibleRows = document.querySelectorAll('#preview-table-container tbody tr:not([hidden])');
  const remaining = visibleRows.length;
  if (remaining < 1) {
    setSendButtonReady(false);
    showToast('No recipients left in preview', 'error');
    return;
  }

  if (!window.confirm(`Send invites to ${remaining} member${remaining !== 1 ? 's' : ''} in Wave ${preview.waveNumber || 1}?`)) return;

  const phones = Array.from(visibleRows).map((row) => row.dataset.phone).filter(Boolean);
  const sendButton = $('send-btn');
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

    showToast(`Wave ${preview.waveNumber || 1} done — ${data.invitesWritten} invites written, ${data.smsSent} SMS sent`, 'success');
    clearPreviewUi();
    $('preview-box').classList.remove('visible');
  } catch (error) {
    showToast(`Send failed: ${error.message}`, 'error');
    sendButton.disabled = false;
    sendButton.textContent = 'Send Invites';
  }
}

export async function sendReminderBlast() {
  const button = $('reminder-blast-btn');
  const modeSelect = $('reminder-mode-select');
  if (!button) return;

  const reminderMode = modeSelect?.value || REMINDER_MODES.DAY_BEFORE;
  const isDayOf = reminderMode === REMINDER_MODES.DAY_OF;
  const reminderLabel = isDayOf ? 'day-of' : 'day-before';

  let eventLabel = 'current event';
  let reminderTiming = REMINDER_MODES.MANUAL;

  try {
    const eventData = await apiJson(ROUTES.ADMIN_EVENT);
    if (eventData.ok && eventData.event) {
      eventLabel = eventData.event.eventSlug || eventData.event.date || 'current event';
      reminderTiming = eventData.event.reminderTiming || REMINDER_MODES.MANUAL;
    }
  } catch {
    // keep defaults
  }

  const shouldContinue = window.confirm(
    `Send ${reminderLabel} reminder blast for ${eventLabel}? Current auto-reminder setting is “${reminderTiming}”.`,
  );
  if (!shouldContinue) return;

  button.disabled = true;
  button.textContent = 'Sending...';

  try {
    const data = await apiJson(ROUTES.ADMIN_INVITE_REMINDER, {
      method: 'POST',
      body: { timing: reminderMode, is_day_of: isDayOf },
    });
    const sentCount = Number(data.sent ?? data.smsSent ?? 0);
    showToast(`${reminderLabel} reminder blast sent — ${sentCount} SMS delivered`, 'success');
  } catch (error) {
    showToast(`Reminder blast failed: ${error.message}`, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Reminder Blast';
  }
}
