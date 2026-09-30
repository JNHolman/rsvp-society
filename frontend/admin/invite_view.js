import { apiJson } from './api.js';
import { ROUTES } from './constants.js';
import { state } from './state.js';
import { $, appendChildren, clearNode, createNode, openModal, closeModal } from './ui.js';

export function createPreviewEmpty(message) {
  return createNode('div', { className: 'analytics-empty analytics-empty-padded', text: message });
}

export function createPreviewStat(value, label, extraClass = '') {
  const stat = createNode('div', { className: 'preview-stat' });
  appendChildren(
    stat,
    createNode('div', { className: `preview-stat-num ${extraClass}`.trim(), text: value }),
    createNode('div', { className: 'preview-stat-label', text: label }),
  );
  return stat;
}

export function createInviteEventSummary(event, capacity) {
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

  // Delivery stats from last blast — only show if a blast has been sent
  if (event.lastBlastSmsSent) {
    const delivered = Number(event.deliveredCount || 0);
    const sent = Number(event.lastBlastSmsSent || 0);
    const failed = Number(event.lastBlastFailed || 0);
    const pending = Math.max(0, sent - delivered - failed);
    const wave = Number(event.lastBlastWave || 0);

    const statsValue = createNode('div', { className: 'invite-event-item-value' });
    statsValue.appendChild(createNode('span', { className: 'delivery-success', text: `${delivered} delivered` }));
    if (pending > 0) {
      statsValue.appendChild(document.createTextNode(` · ${pending} pending`));
    }
    if (failed > 0) {
      statsValue.appendChild(createNode('span', { className: 'delivery-failed', text: ` · ${failed} failed` }));
    }
    addItem(`WAVE ${wave} SMS`, statsValue);
  }

  card.appendChild(grid);
  return card;
}

function waveMetric(value, label, tone = '') {
  const item = createNode('div', { className: `wave-metric ${tone}`.trim() });
  appendChildren(
    item,
    createNode('div', { className: 'wave-metric-value', text: value ?? '—' }),
    createNode('div', { className: 'wave-metric-label', text: label }),
  );
  return item;
}

function normalizeWaveCommand(summary = {}, fallback = {}) {
  const command = summary.commandCenter || summary || {};
  const capacity = Number(command.capacity ?? fallback.capacity ?? $('inv-capacity')?.value ?? 0) || 0;
  const confirmed = Number(command.confirmed ?? command.confirmedCount ?? 0) || 0;
  const plusOneRisk = Number(command.plusOneRisk ?? 0) || 0;
  const nextWaveNumber = Number(command.nextWaveNumber ?? summary.waveNumber ?? fallback.nextWaveNumber ?? 1) || 1;
  return {
    capacity,
    nextWaveNumber,
    wave1Sent: Number(command.wave1Sent ?? fallback.wave1Sent ?? 0) || 0,
    alreadyInvited: Number(command.alreadyInvited ?? summary.alreadyInvitedExcluded ?? fallback.alreadyInvited ?? 0) || 0,
    remainingEligible: Number(command.remainingEligible ?? command.notYetInvited ?? summary.poolRemaining ?? fallback.remainingEligible ?? 0) || 0,
    confirmed,
    attended: Number(command.attended ?? summary.attendedCount ?? fallback.attended ?? 0) || 0,
    plusOneRisk,
    safeCapacityRemaining: Number(command.safeCapacityRemaining ?? Math.max(0, capacity - confirmed - plusOneRisk)) || 0,
    recommendedNextWaveSize: Number(command.recommendedNextWaveSize ?? summary.waveSize ?? fallback.recommendedNextWaveSize ?? 0) || 0,
    eligibleInPreview: Number(command.eligibleInPreview ?? summary.totalInvites ?? fallback.eligibleInPreview ?? 0) || 0,
    formalWavesComplete: Boolean(command.formalWavesComplete ?? fallback.formalWavesComplete ?? nextWaveNumber > 3),
    estimated: Boolean(fallback.estimated && !summary.commandCenter),
  };
}

export function renderWaveCommandCenter(summary = {}, fallback = {}) {
  const host = clearNode('wave-command-center');
  if (!host) return;
  const data = normalizeWaveCommand(summary, fallback);
  host.classList.add('is-visible');
  const header = createNode('div', { className: 'wave-command-header' });
  appendChildren(
    header,
    createNode('div', { className: 'wave-command-kicker', text: data.estimated ? 'Wave Command · Estimated' : 'Wave Command · Preview Locked' }),
    createNode('div', { className: 'wave-command-title', text: data.formalWavesComplete ? 'Manual / Resend Only' : `Next Wave ${data.nextWaveNumber}` }),
    createNode('div', { className: 'wave-command-copy', text: data.formalWavesComplete ? 'Wave 1–3 already exist. Do not create Wave 4; use Manual/Resend for corrections or late one-off invites.' : (data.estimated ? 'Run Preview Next Wave to lock the exact audience and wave number.' : 'This preview has a server-side lock. Send can only use this wave and this audience or a selected subset.') }),
  );
  const grid = createNode('div', { className: 'wave-command-grid' });
  [
    waveMetric(data.wave1Sent, 'Wave 1 Sent'),
    waveMetric(data.alreadyInvited, 'Already Invited'),
    waveMetric(data.remainingEligible, 'Not Yet Invited'),
    waveMetric(data.confirmed, 'Confirmed', 'is-gold'),
    waveMetric(data.attended, 'Attended', 'is-green'),
    waveMetric(data.plusOneRisk, '+1 Risk', data.plusOneRisk ? 'is-warning' : ''),
    waveMetric(data.safeCapacityRemaining, 'Safe Capacity Left', data.safeCapacityRemaining <= 0 ? 'is-danger' : 'is-green'),
    waveMetric(data.recommendedNextWaveSize || data.eligibleInPreview, 'Recommended Send', 'is-gold'),
  ].forEach((node) => grid.appendChild(node));
  host.appendChild(header);
  host.appendChild(grid);
  const command = summary.commandCenter || summary || {};
  const gap = summary.gapAnalysis || command.gapAnalysis || null;
  const why = createNode('div', { className: 'wave-command-why' });
  if (gap) {
    why.textContent = `Why: capacity ${data.capacity}, ${Number(gap.currentConfirmed) || 0} confirmed headcount, target ${Number(gap.targetConfirmed) || 0} at ${Number(gap.assumedShowRate) || 60}% expected show rate, ${Number(gap.assumedConfirmRate) || 30}% invite-to-headcount rate. Recommended send: ${data.recommendedNextWaveSize}.`;
  } else if (data.nextWaveNumber === 1) {
    why.textContent = summary.coldStartTier2Fallback
      ? 'Why: this is a cold-start market with no Tier 1 members yet, so Wave 1 uses Tier 2 to avoid an empty first send. Later waves still expand by RSVP and +1 headcount.'
      : 'Why: Wave 1 targets Tier 1 members. Later waves expand to Tier 2 and then Tier 3 only if the RSVP and +1 headcount leaves a gap.';
  } else {
    why.textContent = `Why: recommendation is based on current confirmations, remaining safe capacity, +1 exposure, and already-invited members.`;
  }
  host.appendChild(why);
}

export async function loadConfirmedUpdateCount(eventId) {
  const node = $('confirmed-update-count');
  if (!node) return;
  if (!eventId) {
    node.textContent = 'Confirmed guests: —';
    return;
  }
  node.textContent = 'Confirmed guests: loading...';
  try {
    const data = await apiJson(`${ROUTES.ADMIN_EVENT_ANALYTICS}?eventId=${encodeURIComponent(eventId)}`);
    const totals = data.analytics?.totals || {};
    const confirmedMembers = Number(totals.members_confirmed ?? totals.confirmed ?? 0) || 0;
    const plusOnes = Number(totals.plus_one_confirmed ?? 0) || 0;
    node.textContent = `Confirmed members: ${confirmedMembers}${plusOnes ? ` · +1 expected: ${plusOnes}` : ''}`;
  } catch {
    node.textContent = 'Confirmed guests: unavailable';
  }
}

export async function loadInitialWaveCommandCenter(eventId, capacity, event = {}) {
  const nextWaveNumber = Number(event.lastBlastWave || 0) + 1 || 1;
  const fallback = {
    capacity,
    nextWaveNumber,
    wave1Sent: Number(event.lastBlastWave || 0) >= 1 ? Number(event.lastBlastSmsSent || 0) : 0,
    alreadyInvited: Number(event.lastBlastSmsSent || 0) || 0,
    recommendedNextWaveSize: capacity && nextWaveNumber === 1 ? Math.ceil(Number(capacity) / 0.8) : 0,
    estimated: true,
  };
  try {
    if (!eventId) throw new Error('eventId missing');
    const data = await apiJson(`${ROUTES.ADMIN_EVENT_ANALYTICS}?eventId=${encodeURIComponent(eventId)}`);
    const totals = data.analytics?.totals || {};
    const byWave = data.analytics?.by_wave || {};
    const waveNumbers = Object.keys(byWave).map((v) => Number(v)).filter((v) => Number.isFinite(v) && v > 0);
    const maxWave = waveNumbers.length ? Math.max(...waveNumbers) : 0;
    renderWaveCommandCenter({}, {
      ...fallback,
      nextWaveNumber: maxWave >= 3 ? 4 : (maxWave + 1 || fallback.nextWaveNumber),
      formalWavesComplete: maxWave >= 3,
      wave1Sent: Number(byWave[1]?.invited || byWave['1']?.invited || fallback.wave1Sent || 0),
      alreadyInvited: Number(totals.invited || 0),
      remainingEligible: 0,
      confirmed: Number(totals.confirmed || 0),
      attended: Number(totals.attended || 0),
      plusOneRisk: Number(totals.plus_one_confirmed || 0),
      safeCapacityRemaining: Math.max(0, Number(capacity || 0) - Number(totals.confirmed || 0) - Number(totals.plus_one_confirmed || 0)),
      recommendedNextWaveSize: 0,
      estimated: true,
    });
  } catch {
    renderWaveCommandCenter({}, fallback);
  }
}

export function currentEventLabel() {
  const ev = state.currentEvent || {};
  return ev.label || ev.title || ev.eventName || ev.eventSlug || ev.eventId || $('inv-event-id')?.value?.trim() || 'Current Event';
}

export function buildSummaryCard(rows = []) {
  const card = createNode('div', { className: 'send-summary-grid' });
  rows.forEach(([label, value, extraClass = '']) => {
    const item = createNode('div', { className: `send-summary-item ${extraClass}`.trim() });
    appendChildren(
      item,
      createNode('span', { className: 'member-detail-label', text: label }),
      createNode('span', { className: 'member-detail-value', text: value ?? '—' }),
    );
    card.appendChild(item);
  });
  return card;
}

export function selectedRecipientSample(rows = []) {
  const sample = rows.slice(0, 5).map((row) => {
    const name = row.querySelector('.member-name')?.textContent?.trim() || '';
    const phone = row.dataset.phone || '';
    return [name, phone].filter(Boolean).join(' · ');
  }).filter(Boolean);
  if (!sample.length) return null;
  const wrap = createNode('div', { className: 'recipient-sample' });
  wrap.appendChild(createNode('div', { className: 'member-intel-title', text: 'Recipient sample' }));
  sample.forEach((line) => wrap.appendChild(createNode('div', { className: 'recipient-sample-row', text: line })));
  if (rows.length > sample.length) wrap.appendChild(createNode('div', { className: 'recipient-sample-more', text: `+${rows.length - sample.length} more` }));
  return wrap;
}

export function openJobStatusModal(jobId, fallback = {}) {
  if (!jobId) return;
  const body = createNode('div', { className: 'job-status-body' });
  const statusLine = createNode('div', { className: 'job-status-line', text: 'Queued. Waiting for backend job status…' });
  const detailGrid = buildSummaryCard([
    ['Job ID', jobId],
    ['Mode', fallback.mode || 'Invite job'],
    ['Recipients', fallback.count || '—'],
    ['Event', fallback.event || currentEventLabel()],
  ]);
  const breakdown = createNode('div', { className: 'job-breakdown' });
  appendChildren(body, statusLine, detailGrid, breakdown);

  const close = createNode('button', { className: 'btn-secondary', text: 'Close', attrs: { type: 'button' } });
  close.addEventListener('click', closeModal);
  const refresh = createNode('button', { className: 'btn-primary-muted', text: 'Refresh', attrs: { type: 'button' } });

  async function poll() {
    refresh.disabled = true;
    refresh.textContent = 'Checking...';
    try {
      const data = await apiJson(`${ROUTES.ADMIN_INVITE_STATUS}?jobId=${encodeURIComponent(jobId)}`);
      const status = data.status || 'UNKNOWN';
      statusLine.textContent = `Status: ${status}`;
      clearNode(breakdown);
      const rows = [
        ['Invites Written', data.invitesWritten ?? '—'],
        ['SMS Sent', data.smsSent ?? '—'],
        ['Failed', data.failed ?? '—', Number(data.failed || 0) > 0 ? 'is-danger' : ''],
      ];
      if (data.autoWaveStatus === 'SCHEDULED') {
        rows.push(['Next Wave', `Wave ${data.autoWaveNumber} in ${data.autoWaveAt} hours`]);
      } else if (data.autoWaveStatus === 'SKIPPED') {
        rows.push(['Next Wave', 'Not scheduled; handle manually if another wave is needed']);
      }
      breakdown.appendChild(buildSummaryCard(rows));
      if (data.error) breakdown.appendChild(createNode('p', { className: 'send-confirm-warning', text: data.error }));
      if (data.autoWaveError) breakdown.appendChild(createNode('p', { className: 'send-confirm-warning', text: 'The next wave could not be scheduled. Handle it manually if another wave is needed.' }));
      if (!['COMPLETE', 'FAILED'].includes(status)) window.setTimeout(poll, 4000);
    } catch (error) {
      statusLine.textContent = 'Texts were queued. Status check is temporarily unavailable. Refresh this panel or Analytics in a moment.';
    } finally {
      refresh.disabled = false;
      refresh.textContent = 'Refresh';
    }
  }

  refresh.addEventListener('click', poll);
  openModal({ kicker: 'Invite Job', title: 'Job status', body, actions: [close, refresh] });
  poll();
}
