import { apiJson } from './api.js';
import { rememberEvent, state } from './state.js';
import { REMINDER_MODES, ROUTES, VIBE_TAGS } from './constants.js';
import { $, appendChildren, clearNode, createNode, setHidden, showToast, openModal, closeModal } from './ui.js';

const TIMEZONE_LABELS = Object.freeze({
  'America/New_York': 'Eastern Time (ET)',
  'America/Chicago': 'Central Time (CT)',
  'America/Denver': 'Mountain Time (MT)',
  'America/Los_Angeles': 'Pacific Time (PT)',
  'America/Phoenix': 'Arizona Time (MST)',
  'America/Anchorage': 'Alaska Time (AKT)',
  'Pacific/Honolulu': 'Hawaii Time (HT)',
});

const EVENT_STATUS_LABELS = Object.freeze({
  DRAFT: 'Draft',
  LIVE: 'Live',
  ARCHIVED: 'Archived',
});

const VALID_EVENT_TRANSITIONS = Object.freeze({
  DRAFT: ['DRAFT', 'LIVE', 'ARCHIVED'],
  LIVE: ['LIVE', 'DRAFT', 'ARCHIVED'],
  ARCHIVED: ['ARCHIVED'],
});

const ACTIVE_ELIGIBLE_STATUSES = new Set(['LIVE']);

let eventFormMode = 'edit';
let eventFormSourceSlug = '';

function setEventEditorOpen(open) {
  const shell = $('event-editor-shell') || document.querySelector('.event-form');
  if (!shell) return;
  shell.classList.toggle('is-closed', !open);
  if (open) shell.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function timezoneLabel(value) {
  return TIMEZONE_LABELS[value] || value || 'America/New_York';
}

function formatDateForDisplay(value) {
  const raw = (value || '').trim();
  if (!raw) return '';
  const date = new Date(`${raw}T00:00:00`);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}

function formatTimeForDisplay(value) {
  const raw = (value || '').trim();
  if (!raw) return '';
  const match = raw.match(/^(\d{2}):(\d{2})$/);
  if (!match) return raw;
  const [_, hh, mm] = match;
  const date = new Date(`2000-01-01T${hh}:${mm}:00`);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}


function isFiveMinuteAligned(value) {
  const raw = (value || '').trim();
  const match = raw.match(/^(\d{2}):(\d{2})$/);
  if (!match) return false;
  return Number(match[2]) % 5 === 0;
}

function normalizeTimeInput(value) {
  const raw = (value || '').trim();
  if (!raw) return '';
  const match = raw.match(/^(\d{1,2}):(\d{2})(?:\s*([AP]M))?$/i);
  if (!match) return raw;
  let hours = Number(match[1]);
  const minutes = match[2];
  const meridiem = (match[3] || '').toUpperCase();
  if (meridiem) {
    if (hours === 12) hours = 0;
    if (meridiem === 'PM') hours += 12;
  }
  return `${String(hours).padStart(2, '0')}:${minutes}`;
}


export function updateVibeTags() {
  const type = $('ev-type').value;
  const select = $('ev-vibe-tag');
  const tags = VIBE_TAGS[type] || [];
  if (!select) return;

  select.replaceChildren();
  const placeholder = createNode('option', {
    text: tags.length ? 'Select vibe tag...' : 'Select event type first...',
    attrs: { value: '' },
  });
  select.appendChild(placeholder);
  tags.forEach((tag) => select.appendChild(createNode('option', { text: tag, attrs: { value: tag } })));
}

export function previewJadeMessages() {
  const label = $('ev-label').value.trim();
  const date = $('ev-date').value.trim();
  const time = formatTimeForDisplay($('ev-time').value.trim());
  const vibe = $('ev-vibe-tag').value.trim();
  const address = $('ev-address').value.trim();
  const venue = $('ev-venue').value.trim();
  const revealVenue = $('ev-reveal-venue').checked;
  const dresscode = $('ev-dresscode').value.trim();
  const allowPlusOnes = $('ev-allow-plus-ones').checked;

  if (!date) {
    showToast('Add a date first', 'error');
    return;
  }

  const dateDisplay = formatDateForDisplay(date);


  const inviteParts = ['{name}.'];
  if (label) inviteParts.push(`${label}.`);
  inviteParts.push(`${dateDisplay}.`);
  if (vibe) inviteParts.push(`${vibe}.`);
  if (time) inviteParts.push(`${time}.`);
  // Initial invites never reveal venue/address. Confirmed reminders/logistics may.
  if (dresscode) inviteParts.push(`${dresscode}.`);
  if (allowPlusOnes) inviteParts.push('+1 welcome.');
  inviteParts.push('Let me know.');

  const dayBeforeParts = ['{name}.', 'Tomorrow.'];
  if (label) dayBeforeParts.push(`${label}.`);
  if (time) dayBeforeParts.push(`Doors at ${time}.`);
  if (revealVenue && venue) dayBeforeParts.push(`${venue}.`);

  const dayOfParts = ['{name}.', 'Tonight.'];
  if (label) dayOfParts.push(`${label}.`);
  if (time) dayOfParts.push(`Doors at ${time}.`);
  if (revealVenue && venue) dayOfParts.push(`${venue}.`);
  if (revealVenue && address) dayOfParts.push(`${address}.`);

  $('jade-invite-preview').value = inviteParts.join(' ');
  $('jade-day-before-preview').value = dayBeforeParts.join(' ');
  $('jade-day-of-preview').value = dayOfParts.join(' ');

  setHidden('jade-preview-panel', false);
  const lockStatus = $('template-lock-status');
  if (lockStatus) {
    lockStatus.textContent = 'Edit above, then Save Draft or Publish Event to lock in.';
    lockStatus.classList.remove('is-success');
    lockStatus.classList.add('is-muted');
  }
  $('jade-preview-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}


export async function draftWithJade() {
  const date = $('ev-date').value.trim();
  const label = $('ev-label').value.trim();
  if (!date && !label) {
    showToast('Add a label or date first', 'error');
    return;
  }

  const btn = $('event-draft-jade-btn');
  const prevText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Drafting…'; }

  const body = {
    eventSlug: $('ev-id').value.trim(),
    event_label: label,
    date,
    startTime: $('ev-time').value.trim(),
    vibe_tag: $('ev-vibe-tag').value.trim(),
    dresscode: $('ev-dresscode').value.trim(),
    venue: $('ev-venue').value.trim(),
    address: $('ev-address').value.trim(),
    allowPlusOnes: $('ev-allow-plus-ones').checked,
  };

  try {
    const data = await apiJson(ROUTES.ADMIN_EVENT_DRAFT_MESSAGE, { method: 'POST', body });
    const drafts = data?.drafts || {};
    if (drafts.invite)    $('jade-invite-preview').value = drafts.invite;
    if (drafts.dayBefore) $('jade-day-before-preview').value = drafts.dayBefore;
    if (drafts.dayOf)     $('jade-day-of-preview').value = drafts.dayOf;

    setHidden('jade-preview-panel', false);
    const lockStatus = $('template-lock-status');
    if (lockStatus) {
      lockStatus.textContent = 'Jade drafted these. Edit above, then Save Draft or Publish Event to lock in.';
      lockStatus.classList.remove('is-success');
      lockStatus.classList.add('is-muted');
    }
    if (!drafts.invite) {
      showToast('Invite draft was withheld (venue/address slipped in) — try again', 'error');
    }
    $('jade-preview-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (error) {
    showToast(error?.message || 'Draft failed', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prevText; }
  }
}


function displayEventTitle(event = {}) {
  return event.event_label || event.label || event.eventSlug || event.slug || event.eventId || 'Untitled Event';
}

function eventSlug(event = {}) {
  return String(event.eventSlug || event.slug || event.eventId || '').trim();
}

function eventSubline(event = {}) {
  return [formatDateForDisplay(event.date), formatTimeForDisplay(event.startTime), event.venue, event.city]
    .filter(Boolean)
    .join(' · ') || 'No date/time configured';
}

function setEventStatusOptions(currentStatus = 'DRAFT') {
  const select = $('ev-status');
  if (!select) return;
  // INVITING was retired from the UI; legacy events still carrying it behave like
  // LIVE (public + sendable), so coerce to LIVE for the dropdown.
  let normalized = String(currentStatus || 'DRAFT').toUpperCase();
  if (normalized === 'INVITING') normalized = 'LIVE';
  const allowed = new Set((VALID_EVENT_TRANSITIONS[normalized] || ['DRAFT']).filter((s) => s !== 'INVITING'));
  if (select.tagName === 'SELECT') {
    Array.from(select.options).forEach((option) => {
      const value = String(option.value || '').toUpperCase();
      option.disabled = !allowed.has(value);
    });
  }
  select.value = allowed.has(normalized) ? normalized : 'DRAFT';

  const helper = $('ev-status-help');
  if (helper) {
    // The status guidance lives in the inline field hint next to the label;
    // a second copy here was redundant. Keep this node empty.
    helper.textContent = '';
  }
}

function fillEventForm(event = {}) {
  $('ev-id').value = event.eventSlug || event.eventId || '';
  $('ev-label').value = event.event_label || event.label || '';
  $('ev-date').value = event.date || '';
  $('ev-time').value = normalizeTimeInput(event.startTime || '');
  $('ev-city').value = event.city || '';
  if ($('ev-zip')) $('ev-zip').value = event.eventZipCode || '';
  if ($('ev-promotion-radius')) $('ev-promotion-radius').value = event.promotionRadiusMiles || '';
  $('ev-capacity').value = event.capacity || '';
  if ($('ev-show-rate')) $('ev-show-rate').value = Math.round(Number(event.expectedShowRate ?? 0.60) * 100);
  const isArchived = Boolean(event.archived) || String(event.event_status || '').toUpperCase() === 'ARCHIVED';
  if ($('ev-status')) {
    $('ev-status').value = event.event_status || 'DRAFT';
    setEventStatusOptions(event.event_status || 'DRAFT');
    $('ev-status').disabled = isArchived;
  }
  const saveActiveBtn = $('event-save-active-btn');
  if (saveActiveBtn) saveActiveBtn.disabled = isArchived;
  const saveBtn = $('event-save-btn');
  if (saveBtn) saveBtn.disabled = isArchived;
  $('ev-timezone').value = event.event_timezone || 'America/New_York';
  $('ev-venue').value = event.venue || '';
  $('ev-address').value = event.address || '';
  $('ev-dresscode').value = event.dresscode || '';
  $('ev-reveal-venue').checked = !!event.revealVenue;
  // Field collapse: description is the single source; fall back to legacy jadeNotes.
  const eventIntel = event.description || event.jadeNotes || event.jade_notes || '';
  if ($('ev-jade-notes')) $('ev-jade-notes').value = eventIntel;
  if ($('ev-allow-plus-ones')) $('ev-allow-plus-ones').checked = !!event.allowPlusOnes;
  if ($('ev-ticket-url')) $('ev-ticket-url').value = event.ticketUrl || '';
  if ($('ev-section-info')) $('ev-section-info').value = event.sectionInfo || '';
  if ($('ev-parking-info')) $('ev-parking-info').value = event.parkingInfo || event.parking_info || '';
  if ($('ev-end-time')) $('ev-end-time').value = normalizeTimeInput(event.endTime || '');
  if (event.event_type) {
    $('ev-type').value = event.event_type;
    updateVibeTags();
    if (event.vibe_tag) $('ev-vibe-tag').value = event.vibe_tag;
  } else {
    $('ev-type').value = '';
    updateVibeTags();
  }
  if ($('ev-remind-day-before-time')) $('ev-remind-day-before-time').value = normalizeTimeInput(event.day_before_send_time || '18:00') || '18:00';
  if ($('ev-remind-day-of-time')) $('ev-remind-day-of-time').value = normalizeTimeInput(event.day_of_send_time || '11:00') || '11:00';
  const timing = event.reminderTiming || REMINDER_MODES.MANUAL;
  $('ev-remind-day-before').checked = timing.includes(REMINDER_MODES.DAY_BEFORE) || timing === REMINDER_MODES.BOTH;
  $('ev-remind-day-of').checked = timing.includes(REMINDER_MODES.DAY_OF) || timing === REMINDER_MODES.BOTH;
  const savedDayBeforeTemplate = (event.day_before_template || event.reminder_template || '').trim();
  const savedDayOfTemplate = (event.day_of_template || event.reminder_template || '').trim();
  const hasSavedTemplates = Boolean(event.invite_template || savedDayBeforeTemplate || savedDayOfTemplate);
  $('jade-invite-preview').value = event.invite_template || '';
  $('jade-day-before-preview').value = savedDayBeforeTemplate;
  $('jade-day-of-preview').value = savedDayOfTemplate;
  const lockStatus = $('template-lock-status');
  if (hasSavedTemplates) {
    setHidden('jade-preview-panel', false);
    if (lockStatus) {
      lockStatus.textContent = '✓ Templates locked';
      lockStatus.classList.remove('is-muted');
      lockStatus.classList.add('is-success');
    }
  } else {
    setHidden('jade-preview-panel', true);
    if (lockStatus) {
      lockStatus.textContent = '';
      lockStatus.classList.remove('is-muted', 'is-success');
    }
  }
}

function createEventListCard(event = {}) {
  const slug = eventSlug(event);
  const card = createNode('div', { className: `event-list-card${event.active ? ' is-active' : ''}` });
  const meta = createNode('div', { className: 'event-list-meta' });
  appendChildren(
    meta,
    createNode('div', { className: 'event-list-title', text: displayEventTitle(event) }),
    createNode('div', { className: 'event-list-sub', text: eventSubline(event) }),
    createNode('div', { className: 'event-list-tags' }),
  );
  const tags = meta.querySelector('.event-list-tags');
  const status = String(event.event_status || 'DRAFT').toUpperCase();
  const displayStatus = event.archived || status === 'ARCHIVED' ? 'ARCHIVED' : (EVENT_STATUS_LABELS[status] ? status : 'DRAFT');
  tags.appendChild(createNode('span', {
    className: `event-pill ${displayStatus === 'LIVE' ? 'is-live' : displayStatus === 'ARCHIVED' ? 'is-archived' : ''}`.trim(),
    text: EVENT_STATUS_LABELS[displayStatus] || displayStatus,
  }));
  const actions = createNode('div', { className: 'event-list-actions' });
  const edit = createNode('button', { className: 'mini-btn', text: 'Edit', attrs: { type: 'button' } });
  edit.addEventListener('click', () => {
    state.currentEvent = event;
    fillEventForm(event);
    setEventEditorOpen(true);
    eventFormMode = 'edit';
    eventFormSourceSlug = slug;
    showToast(`Loaded ${displayEventTitle(event)}`, 'success');
  });
  const canSetActive = !event.active && !event.archived && status !== 'ARCHIVED';
  const active = createNode('button', {
    className: 'mini-btn',
    text: event.active ? 'Live' : 'Make Live',
    attrs: {
      type: 'button',
      disabled: !canSetActive,
      title: canSetActive ? 'Save this event as Live' : 'This event is already Live or Archived',
    },
  });
  active.addEventListener('click', () => makeEventLive(event));
  const dup = createNode('button', { className: 'mini-btn', text: 'Duplicate', attrs: { type: 'button' } });
  dup.addEventListener('click', () => duplicateEventBySlug(slug));
  const archive = createNode('button', { className: 'mini-btn', text: 'Archive', attrs: { type: 'button', disabled: event.archived || event.event_status === 'ARCHIVED' } });
  archive.addEventListener('click', () => archiveEventBySlug(slug));
  const del = createNode('button', { className: 'mini-btn danger', text: 'Delete', attrs: { type: 'button' } });
  del.addEventListener('click', () => hardDeleteEventBySlug(slug));
  appendChildren(actions, edit, active, dup, archive, del);
  appendChildren(card, meta, actions);
  return card;
}

async function loadEventsList() {
  const host = clearNode('event-list-container');
  if (!host) return;
  try {
    const data = await apiJson(ROUTES.ADMIN_EVENTS);
    const events = Array.isArray(data.events) ? data.events : [];
    if (!events.length) {
      host.appendChild(createNode('div', { className: 'analytics-empty', text: 'No saved events yet. Create one below.' }));
      return;
    }
    events.forEach((event) => host.appendChild(createEventListCard(event)));
  } catch (error) {
    host.appendChild(createNode('div', { className: 'analytics-empty', text: `Could not load events: ${error.message}` }));
  }
}

async function makeEventLive(event = {}) {
  const slug = eventSlug(event);
  if (!slug) return;
  try {
    const payload = { ...event, eventSlug: slug, eventId: slug, event_status: 'LIVE', setActive: true };
    const data = await saveEventRecord(payload);
    showToast('Event is live', 'success');
    if (data.event) state.currentEvent = data.event;
    await loadCurrentEvent();
  } catch (error) {
    showToast(`Make live failed: ${error.message}`, 'error');
  }
}

async function archiveEventBySlug(slug) {
  const body = createNode('div', { className: 'send-confirm-body' });
  appendChildren(
    body,
    createNode('p', { className: 'modal-copy send-confirm-copy', text: 'This archives the event and removes it from active event operations. Event history is kept.' }),
    createNode('p', { className: 'send-confirm-warning', text: 'Use Hard Delete only for test events, mistakes, or events that will not happen.' }),
  );
  const cancel = createNode('button', { className: 'btn-secondary', text: 'Keep Event', attrs: { type: 'button' } });
  cancel.addEventListener('click', closeModal);
  const confirm = createNode('button', { className: 'btn-primary-muted', text: 'Archive Event', attrs: { type: 'button' } });
  confirm.addEventListener('click', async () => {
    confirm.disabled = true;
    confirm.textContent = 'Archiving...';
    try {
      await apiJson(ROUTES.ADMIN_EVENTS_ARCHIVE, { method: 'POST', body: { eventSlug: slug } });
      closeModal();
      showToast('Event archived', 'success');
      if (eventSlug(state.currentEvent || {}) === slug) startNewEvent();
      await loadEventsList();
    } catch (error) {
      confirm.disabled = false;
      confirm.textContent = 'Archive Event';
      showToast(`Archive failed: ${error.message}`, 'error');
    }
  });
  openModal({ kicker: 'Event Control', title: 'Archive event?', body, actions: [cancel, confirm] });
}

async function hardDeleteEventBySlug(slug) {
  const body = createNode('div', { className: 'send-confirm-body danger-confirm-body' });
  appendChildren(
    body,
    createNode('p', { className: 'modal-copy send-confirm-copy', text: 'This permanently deletes the event record and removes event-scoped invite/check-in rows. Use this only for test events or events that will not happen.' }),
    createNode('p', { className: 'send-confirm-warning', text: 'This cannot be undone.' }),
    createNode('label', { className: 'danger-confirm-label', text: `Type ${slug} to hard delete this event.` }),
  );
  const input = createNode('input', { className: 'danger-confirm-input', attrs: { type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: slug } });
  body.appendChild(input);
  const cancel = createNode('button', { className: 'btn-secondary', text: 'Keep Event', attrs: { type: 'button' } });
  cancel.addEventListener('click', closeModal);
  const confirm = createNode('button', { className: 'btn-primary-muted danger', text: 'Hard Delete Event', attrs: { type: 'button', disabled: true } });
  input.addEventListener('input', () => { confirm.disabled = input.value.trim() !== slug; });
  confirm.addEventListener('click', async () => {
    if (input.value.trim() !== slug) return;
    confirm.disabled = true;
    confirm.textContent = 'Deleting...';
    try {
      await apiJson(`${ROUTES.ADMIN_EVENTS}?eventSlug=${encodeURIComponent(slug)}&hardDelete=true`, { method: 'DELETE', body: { eventSlug: slug, confirmSlug: slug, hardDelete: true } });
      closeModal();
      showToast('Event hard deleted', 'success');
      if (eventSlug(state.currentEvent || {}) === slug) startNewEvent();
      await loadEventsList();
    } catch (error) {
      confirm.disabled = false;
      confirm.textContent = 'Hard Delete Event';
      showToast(`Delete failed: ${error.message}`, 'error');
    }
  });
  openModal({ kicker: 'Danger Zone', title: 'Hard delete event?', body, actions: [cancel, confirm] });
}

function duplicateEventBySlug(slug) {
  const body = createNode('div', { className: 'single-invite-grid' });
  const field = createNode('label', { className: 'member-detail-row' });
  appendChildren(
    field,
    createNode('span', { className: 'member-detail-label', text: 'New Event Slug' }),
    createNode('input', { className: 'modal-input', attrs: { id: 'duplicate-event-slug', value: `${slug}-copy`, type: 'text' } }),
  );
  body.appendChild(field);
  body.appendChild(createNode('p', { className: 'modal-copy', text: 'The draft copies the date, venue, address, event details, ticket link, and configured show-rate estimate. Review these before making it live.' }));
  const cancel = createNode('button', { className: 'btn-secondary', text: 'Cancel', attrs: { type: 'button' } });
  cancel.addEventListener('click', closeModal);
  const confirm = createNode('button', { className: 'btn-primary-muted', text: 'Duplicate', attrs: { type: 'button' } });
  confirm.addEventListener('click', async () => {
    const newSlug = $('duplicate-event-slug')?.value?.trim();
    if (!newSlug) return showToast('New slug required', 'error');
    confirm.disabled = true;
    try {
      const data = await apiJson(`${ROUTES.ADMIN_EVENTS}/duplicate?eventSlug=${encodeURIComponent(slug)}`, { method: 'POST', body: { newEventSlug: newSlug } });
      closeModal();
      showToast('Event duplicated as draft', 'success');
      if (data.event) {
        state.currentEvent = data.event;
        fillEventForm(data.event);
        eventFormMode = 'edit';
        eventFormSourceSlug = eventSlug(data.event);
      }
      await loadEventsList();
    } catch (error) {
      confirm.disabled = false;
      showToast(`Duplicate failed: ${error.message}`, 'error');
    }
  });
  openModal({ kicker: 'Event Control', title: 'Duplicate event', body, actions: [cancel, confirm] });
}

function renderEventCurrent(fields = []) {
  const grid = clearNode('event-current-grid');
  if (!grid) return;
  const fieldMap = Object.fromEntries(fields.map((field) => [field.label, field.val || '—']));
  const title = state.currentEvent?.event_label || fieldMap.Event || 'Current Event';
  const status = String(state.currentEvent?.event_status || 'DRAFT').toUpperCase();
  const subtitle = [fieldMap.Date, fieldMap.Start, fieldMap.Venue]
    .filter((value) => value && value !== '—')
    .join(' · ') || 'No Live event loaded';

  const hero = createNode('div', { className: 'event-active-card' });
  const copy = createNode('div', { className: 'event-active-copy' });
  appendChildren(
    copy,
    createNode('div', { className: 'event-command-kicker', text: status }),
    createNode('div', { className: 'event-command-title', text: title }),
    createNode('div', { className: 'event-command-subtitle', text: subtitle }),
  );

  const stats = createNode('div', { className: 'event-active-stats' });
  [
    ['Capacity', fieldMap.Capacity || '—'],
    ['Reminders', fieldMap.Reminders || 'manual'],
  ].forEach(([label, value]) => {
    const item = createNode('div', { className: 'event-command-stat' });
    appendChildren(item, createNode('span', { text: label }), createNode('strong', { text: value || '—' }));
    stats.appendChild(item);
  });

  appendChildren(hero, copy, stats);
  grid.appendChild(hero);
}

export async function loadCurrentEvent() {
  try {
    const data = await apiJson(ROUTES.ADMIN_EVENT);
    const event = data.ok && data.event ? data.event : null;
    if (event && Object.keys(event).length) {
      state.currentEvent = event;
      rememberEvent(event);
      fillEventForm(event);
      eventFormMode = 'edit';
      eventFormSourceSlug = eventSlug(event);
      renderEventCurrent([
        { label: 'Event', val: event.eventSlug || event.eventId },
        { label: 'Status', val: event.event_status || 'DRAFT' },
        { label: 'Date', val: formatDateForDisplay(event.date) },
        { label: 'Start', val: formatTimeForDisplay(event.startTime) },
        { label: 'Venue', val: event.venue ? `${event.venue}${event.revealVenue ? ' ✓ revealed' : ' — hidden'}` : '' },
        { label: 'Capacity', val: event.capacity ? `${event.capacity}` : '' },
        { label: 'City', val: event.city || '' },
        { label: 'Time Zone', val: timezoneLabel(event.event_timezone) },
        { label: 'Reminders', val: event.reminderTiming || 'manual' },
        { label: 'Dress', val: event.dresscode || '' },
      ]);
      $('event-current-display')?.classList.remove('visible');
    } else {
      state.currentEvent = null;
      renderEventCurrent([]);
      $('event-current-display')?.classList.remove('visible');
      setEventStatusOptions('DRAFT');
    }
    await loadEventsList();
  } catch (error) {
    showToast(`Event load failed: ${error.message}`, 'error');
    await loadEventsList();
  }
}

function clearEventForm() {
  ['ev-id','ev-label','ev-date','ev-time','ev-city','ev-zip','ev-promotion-radius','ev-capacity','ev-show-rate','ev-venue','ev-address','ev-dresscode','ev-jade-notes','ev-ticket-url','ev-section-info','ev-parking-info','ev-end-time'].forEach((id) => {
    const el = $(id);
    if (el) el.value = '';
  });
  if ($('ev-status')) { $('ev-status').value = 'DRAFT'; $('ev-status').disabled = false; }
  setEventStatusOptions('DRAFT');
  if ($('event-save-btn')) $('event-save-btn').disabled = false;
  if ($('event-save-active-btn')) $('event-save-active-btn').disabled = false;
  if ($('ev-timezone')) $('ev-timezone').value = 'America/New_York';
  if ($('ev-reveal-venue')) $('ev-reveal-venue').checked = false;
  if ($('ev-allow-plus-ones')) $('ev-allow-plus-ones').checked = false;
  if ($('ev-remind-day-before')) $('ev-remind-day-before').checked = false;
  if ($('ev-remind-day-of')) $('ev-remind-day-of').checked = false;
  if ($('ev-remind-day-before-time')) $('ev-remind-day-before-time').value = '18:00';
  if ($('ev-remind-day-of-time')) $('ev-remind-day-of-time').value = '11:00';
  if ($('ev-type')) $('ev-type').value = '';
  updateVibeTags();
  ['jade-invite-preview','jade-day-before-preview','jade-day-of-preview'].forEach((id) => { if ($(id)) $(id).value = ''; });
  setHidden('jade-preview-panel', true);
  $('event-current-display')?.classList.remove('visible');
  eventFormMode = 'new';
  eventFormSourceSlug = '';
  setEventEditorOpen(true);
  showToast('New event form ready. Save creates a separate event record.', 'success');
}


export function startNewEvent() {
  clearEventForm();
}

export function duplicateCurrentEvent() {
  const ev = state.currentEvent;
  const slug = eventSlug(ev || {});
  if (!slug) {
    showToast('Load an event before duplicating', 'error');
    return;
  }
  duplicateEventBySlug(slug);
}

export function deleteCurrentEvent() {
  const slug = eventSlug(state.currentEvent || {});
  if (!slug) {
    showToast('No event selected to delete', 'error');
    return;
  }
  hardDeleteEventBySlug(slug);
}


async function saveEventRecord(body) {
  try {
    return await apiJson(ROUTES.ADMIN_EVENTS, {
      method: 'POST',
      body,
    });
  } catch (error) {
    // Some live API Gateway stages still expose the legacy /admin/event POST
    // before the newer /admin/events POST method is applied. 403/404 here is
    // usually a route/method deployment gap, not bad form data.
    if (error?.status !== 403 && error?.status !== 404) throw error;
    return apiJson(ROUTES.ADMIN_EVENT, {
      method: 'POST',
      body,
    });
  }
}

export async function saveEvent({ setActive = false } = {}) {
  const dayBeforeTemplate = $('jade-day-before-preview').value.trim();
  const dayOfTemplate = $('jade-day-of-preview').value.trim();
  const legacyReminderTemplate = dayBeforeTemplate === dayOfTemplate
    ? dayBeforeTemplate
    : (dayBeforeTemplate || dayOfTemplate || '');

  const body = {
    eventSlug: $('ev-id').value.trim(),
    event_label: $('ev-label').value.trim(),
    date: $('ev-date').value.trim(),
    startTime: $('ev-time').value.trim(),
    day_before_send_time: ($('ev-remind-day-before-time')?.value || '18:00'),
    day_of_send_time: ($('ev-remind-day-of-time')?.value || '11:00'),
    city: ($('ev-city')?.value || '').trim(),
    eventZipCode: ($('ev-zip')?.value || '').trim(),
    promotionRadiusMiles: ($('ev-promotion-radius')?.value || '').trim(),
    capacity: parseInt($('ev-capacity').value, 10) || 0,
    expectedShowRate: (parseInt($('ev-show-rate')?.value || '60', 10) || 60) / 100,
    event_status: setActive ? 'LIVE' : String(($('ev-status')?.value || state.currentEvent?.event_status || 'DRAFT')).toUpperCase(),
    event_timezone: $('ev-timezone').value || 'America/New_York',
    venue: $('ev-venue').value.trim(),
    address: $('ev-address').value.trim(),
    dresscode: $('ev-dresscode').value.trim(),
    revealVenue: $('ev-reveal-venue').checked,
    event_type: $('ev-type').value,
    vibe_tag: $('ev-vibe-tag').value,
    // Field collapse: the single Event Intelligence box (ev-jade-notes) is the source.
    // Save it to description only; jadeNotes is retired.
    description: ($('ev-jade-notes')?.value || '').trim(),
    invite_template: $('jade-invite-preview').value.trim(),
    reminder_template: legacyReminderTemplate,
    day_before_template: dayBeforeTemplate,
    day_of_template: dayOfTemplate,
    reminderTiming: (() => {
      const dayBefore = $('ev-remind-day-before').checked;
      const dayOf = $('ev-remind-day-of').checked;
      if (dayBefore && dayOf) return REMINDER_MODES.BOTH;
      if (dayBefore) return REMINDER_MODES.DAY_BEFORE;
      if (dayOf) return REMINDER_MODES.DAY_OF;
      return REMINDER_MODES.MANUAL;
    })(),
    allowPlusOnes: $('ev-allow-plus-ones').checked,
    ticketUrl: ($('ev-ticket-url')?.value || '').trim(),
    sectionInfo: ($('ev-section-info')?.value || '').trim(),
    parkingInfo: ($('ev-parking-info')?.value || '').trim(),
    endTime: ($('ev-end-time')?.value || '').trim(),
  };

  if (body.expectedShowRate < 0.1 || body.expectedShowRate > 1) {
    showToast('Expected show rate must be 10–100%', 'error');
    return;
  }

  if (body.event_status === 'LIVE') {
    if (!/^\d{5}$/.test(body.eventZipCode)) {
      showToast('A valid 5-digit Event ZIP is required before going Live', 'error');
      return;
    }
    const radius = Number(body.promotionRadiusMiles);
    if (!Number.isFinite(radius) || radius <= 0) {
      showToast('Promotion Radius must be greater than 0 before going Live', 'error');
      return;
    }
  }

  if (!body.date) {
    showToast('Date is required', 'error');
    return;
  }

  if (!body.startTime) {
    showToast('Start time is required', 'error');
    return;
  }

  if (!isFiveMinuteAligned(body.day_before_send_time) || !isFiveMinuteAligned(body.day_of_send_time)) {
    showToast('Reminder send times must use 5-minute increments', 'error');
    return;
  }

  if (!EVENT_STATUS_LABELS[body.event_status]) {
    showToast('Invalid event status', 'error');
    return;
  }

  try {
    await saveEventRecord({ ...body, setActive });
    showToast(setActive ? 'Event saved and made live' : (eventFormMode === 'new' ? 'Draft event created' : 'Event saved'), 'success');
    setEventEditorOpen(false);
    clearEventForm();
    setEventEditorOpen(false);
    await loadCurrentEvent();
  } catch (error) {
    showToast(`Save failed: ${error.message}`, 'error');
  }
}

export function saveEventAndSetActive() {
  saveEvent({ setActive: true });
}
