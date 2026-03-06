import { apiJson } from './api.js';
import { rememberEvent, state } from './state.js';
import { REMINDER_MODES, ROUTES, VIBE_TAGS } from './constants.js';
import { $, appendChildren, clearNode, createNode, setHidden, showToast } from './ui.js';

const TIMEZONE_LABELS = Object.freeze({
  'America/New_York': 'Eastern Time (ET)',
  'America/Chicago': 'Central Time (CT)',
  'America/Denver': 'Mountain Time (MT)',
  'America/Los_Angeles': 'Pacific Time (PT)',
  'America/Phoenix': 'Arizona Time (MST)',
  'America/Anchorage': 'Alaska Time (AKT)',
  'Pacific/Honolulu': 'Hawaii Time (HT)',
});

function timezoneLabel(value) {
  return TIMEZONE_LABELS[value] || value || 'America/New_York';
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
  const time = $('ev-time').value.trim();
  const vibe = $('ev-vibe-tag').value.trim();
  const address = $('ev-address').value.trim();
  const venue = $('ev-venue').value.trim();
  const revealVenue = $('ev-reveal-venue').checked;

  if (!date) {
    showToast('Add a date first', 'error');
    return;
  }

  const closings = ['Tap in.', 'Lmk.', 'We on?', 'You sliding?', 'Still on?', 'Pull up.'];
  const inviteClosing = Math.random() < 0.4 ? closings[Math.floor(Math.random() * closings.length)] : '';
  const dayBeforeClosing = Math.random() < 0.4 ? closings[Math.floor(Math.random() * closings.length)] : '';
  const dayOfClosing = Math.random() < 0.4 ? closings[Math.floor(Math.random() * closings.length)] : '';

  const inviteParts = ['{name}.'];
  if (label) inviteParts.push(`${label}.`);
  inviteParts.push(`${date}.`);
  if (vibe) inviteParts.push(`${vibe}.`);
  if (time) inviteParts.push(`${time}.`);
  if (revealVenue && venue) inviteParts.push(`${venue}.`);
  if (revealVenue && address) inviteParts.push(`${address}.`);
  if (inviteClosing) inviteParts.push(inviteClosing);

  const dayBeforeParts = ['{name}.', 'Tomorrow.'];
  if (label) dayBeforeParts.push(`${label}.`);
  if (time) dayBeforeParts.push(`Doors at ${time}.`);
  if (revealVenue && venue) dayBeforeParts.push(`${venue}.`);
  if (dayBeforeClosing) dayBeforeParts.push(dayBeforeClosing);

  const dayOfParts = ['{name}.', 'Tonight.'];
  if (label) dayOfParts.push(`${label}.`);
  if (time) dayOfParts.push(`Doors at ${time}.`);
  if (revealVenue && venue) dayOfParts.push(`${venue}.`);
  if (revealVenue && address) dayOfParts.push(`${address}.`);
  if (dayOfClosing) dayOfParts.push(dayOfClosing);

  $('jade-invite-preview').value = inviteParts.join(' ');
  $('jade-day-before-preview').value = dayBeforeParts.join(' ');
  $('jade-day-of-preview').value = dayOfParts.join(' ');

  setHidden('jade-preview-panel', false);
  const lockStatus = $('template-lock-status');
  lockStatus.textContent = 'Edit above, then Save Event to lock in.';
  lockStatus.classList.remove('is-success');
  lockStatus.classList.add('is-muted');
  $('jade-preview-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderEventCurrent(fields = []) {
  const grid = clearNode('event-current-grid');
  if (!grid) return;

  fields.forEach((field) => {
    const item = createNode('div', { className: 'event-current-item' });
    appendChildren(
      item,
      createNode('label', { text: field.label }),
      createNode('span', { text: field.val || '—' }),
    );
    grid.appendChild(item);
  });
}

export async function loadCurrentEvent() {
  try {
    const data = await apiJson(ROUTES.ADMIN_EVENT);
    if (!(data.ok && data.event)) return;

    const event = data.event;
    state.currentEvent = event;
    rememberEvent(event);

    $('ev-id').value = event.eventSlug || event.eventId || '';
    $('ev-label').value = event.event_label || '';
    $('ev-date').value = event.date || '';
    $('ev-time').value = event.startTime || '';
    $('ev-city').value = event.city || '';
    $('ev-capacity').value = event.capacity || '';
    $('ev-timezone').value = event.event_timezone || 'America/New_York';
    $('ev-venue').value = event.venue || '';
    $('ev-address').value = event.address || '';
    $('ev-dresscode').value = event.dresscode || '';
    $('ev-reveal-venue').checked = !!event.revealVenue;
    $('ev-description').value = event.description || '';

    if (event.event_type) {
      $('ev-type').value = event.event_type;
      updateVibeTags();
      if (event.vibe_tag) $('ev-vibe-tag').value = event.vibe_tag;
    } else {
      updateVibeTags();
    }

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
      lockStatus.textContent = '✓ Templates locked';
      lockStatus.classList.remove('is-muted');
      lockStatus.classList.add('is-success');
    } else {
      setHidden('jade-preview-panel', true);
      lockStatus.textContent = '';
      lockStatus.classList.remove('is-muted', 'is-success');
    }

    renderEventCurrent([
      { label: 'Event', val: event.eventSlug || event.eventId },
      { label: 'Date', val: event.date },
      { label: 'Venue', val: event.venue ? `${event.venue}${event.revealVenue ? ' ✓ revealed' : ' — hidden'}` : '—' },
      { label: 'Address', val: event.address },
      { label: 'Dresscode', val: event.dresscode },
      { label: 'Capacity', val: event.capacity },
      { label: 'Time Zone', val: timezoneLabel(event.event_timezone || 'America/New_York') },
      { label: 'Vibe', val: event.vibe_tag },
      { label: 'Updated', val: (event.updatedAt || '').replace('T', ' ').slice(0, 16) },
    ]);

    $('event-current-display').classList.add('visible');
  } catch (error) {
    console.error('loadCurrentEvent:', error);
  }
}

export async function saveEvent() {
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
    city: $('ev-city').value.trim(),
    capacity: parseInt($('ev-capacity').value, 10) || 0,
    event_timezone: $('ev-timezone').value || 'America/New_York',
    venue: $('ev-venue').value.trim(),
    address: $('ev-address').value.trim(),
    dresscode: $('ev-dresscode').value.trim(),
    revealVenue: $('ev-reveal-venue').checked,
    event_type: $('ev-type').value,
    vibe_tag: $('ev-vibe-tag').value,
    description: $('ev-description').value.trim(),
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
  };

  if (!body.date) {
    showToast('Date is required', 'error');
    return;
  }

  try {
    await apiJson(ROUTES.ADMIN_EVENT, {
      method: 'POST',
      body,
    });
    showToast('Event saved — Jade is updated', 'success');
    await loadCurrentEvent();
  } catch (error) {
    showToast(`Save failed: ${error.message}`, 'error');
  }
}
