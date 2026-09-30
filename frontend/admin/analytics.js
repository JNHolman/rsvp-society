import { apiJson, fetchAllPages } from './api.js';
import { ROUTES } from './constants.js';
import { loadAttendance } from './attendance.js';
import { getKnownEvent, getKnownEvents, rememberEvent, state } from './state.js';
import { $, appendChildren, createNode, reportError, showToast } from './ui.js';

const EVENT_LIST_ENDPOINTS = [ROUTES.ADMIN_EVENTS];
const STRICT_ANALYTICS_STORAGE_KEY = 'rsvp_analytics_strict';

function parseBool(value) {
  if (typeof value === 'boolean') return value;
  if (value == null) return false;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (['true', '1', 'yes', 'y', 'on'].includes(normalized)) return true;
    if (['false', '0', 'no', 'n', 'off', ''].includes(normalized)) return false;
  }
  return Boolean(value);
}

let analyticsWarnings = [];

function pctClass(value) {
  const safe = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
  return `analytics-pct-${safe}`;
}

function isStrictAnalyticsMode() {
  const raw = localStorage.getItem(STRICT_ANALYTICS_STORAGE_KEY);
  if (raw === null) return true;
  return !['0', 'false', 'off', 'no'].includes(String(raw).trim().toLowerCase());
}

function resetAnalyticsWarnings() {
  analyticsWarnings = [];
  renderAnalyticsHealth();
}

function addAnalyticsWarning(message) {
  if (!message || analyticsWarnings.includes(message)) return;
  analyticsWarnings.push(message);
}

function renderAnalyticsHealth(errorMessage = '') {
  const node = $('analytics-health');
  if (!node) return;

  const messages = errorMessage ? [errorMessage] : analyticsWarnings;
  if (!messages.length) {
    node.hidden = true;
    node.className = 'analytics-health';
    node.replaceChildren();
    return;
  }

  node.hidden = false;
  node.className = `analytics-health ${errorMessage ? 'is-error' : 'is-warning'}`;
  const title = createNode('div', {
    className: 'analytics-health-title',
    text: errorMessage ? 'Analytics error' : 'Analytics warnings',
  });
  const list = createNode('ul', { className: 'analytics-health-list' });
  messages.forEach((message) => {
    list.appendChild(createNode('li', { className: 'analytics-health-item', text: message }));
  });
  node.replaceChildren(title, list);
}

function assertAnalyticsComplete() {
  if (!isStrictAnalyticsMode() || !analyticsWarnings.length) return;
  throw new Error('All-time analytics is partial in strict mode. Set localStorage.rsvp_analytics_strict = false to allow warning-based fallbacks.');
}

function normalizeEventList(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.events || payload?.items || payload?.history || payload?.data || [];
}

function getCurrentEventIdentifier() {
  return state.currentEvent?.eventSlug || state.currentEvent?.eventId || state.currentEvent?.slug || '';
}

function getMemberCount(data) {
  if (Number.isFinite(data?.total)) return data.total;
  return Array.isArray(data?.members) ? data.members.length : 0;
}

function getAttendedFlag(member = {}) {
  return parseBool(
    member.attended
    ?? member.didAttend
    ?? member.checkedIn
    ?? member.isAttended
    ?? member.attendance?.attended
  );
}

function getAttendanceCountFromMember(member = {}) {
  const candidates = [
    member.attendanceCount,
    member.attendedCount,
    member.totalAttendedEvents,
    member.attendance_count,
    member.analytics?.attendanceCount,
  ];

  for (const value of candidates) {
    if (Number.isFinite(Number(value))) return Number(value);
  }
  return null;
}

function setStatText(id, value) {
  const node = $(id);
  if (node) node.textContent = String(value);
}

function buildEventLabel(event = {}, fallback = '') {
  const slug = event.slug || event.eventSlug || event.eventId || fallback || 'current';
  const date = event.date || '';
  const venue = event.venue || '';
  return [slug, date, venue].filter(Boolean).join(' · ');
}

function dedupeMembersByPhone(members = []) {
  const seen = new Map();
  members.forEach((member) => {
    const phone = String(member?.phone || '').trim();
    if (!phone) return;
    if (!seen.has(phone)) {
      seen.set(phone, member);
      return;
    }
    seen.set(phone, { ...seen.get(phone), ...member });
  });
  return [...seen.values()];
}

async function fetchCurrentEventMeta() {
  try {
    const data = await apiJson(ROUTES.ADMIN_EVENT);
    if (data.event) {
      state.currentEvent = data.event;
      return rememberEvent(data.event) || getKnownEvent(getCurrentEventIdentifier());
    }
  } catch (error) {
    addAnalyticsWarning(`Current event metadata could not be refreshed; using cached event info where available. ${error?.message || ''}`.trim());
  }

  return getCurrentEventIdentifier() ? getKnownEvent(getCurrentEventIdentifier()) : null;
}

async function fetchEventListFromApi() {
  if (!EVENT_LIST_ENDPOINTS.length) return [];
  let lastError = null;
  for (const endpoint of EVENT_LIST_ENDPOINTS) {
    try {
      const { items } = await fetchAllPages(endpoint, normalizeEventList);
      const events = items.filter((event) => event?.slug || event?.eventSlug || event?.eventId);
      if (events.length) return events;
    } catch (error) {
      lastError = error;
    }
  }
  if (lastError) {
    addAnalyticsWarning(`Full event history endpoint was unavailable; all-time event counts may rely on cached known events. ${lastError?.message || ''}`.trim());
  }
  return [];
}

async function getTrackedEvents() {
  const tracked = [];
  const seen = new Set();

  const add = (event) => {
    const remembered = rememberEvent(event);
    const normalized = remembered || getKnownEvent(event?.slug || event?.eventId || event?.eventSlug || '');
    if (!normalized?.slug || seen.has(normalized.slug)) return;
    seen.add(normalized.slug);
    tracked.push(normalized);
  };

  const current = await fetchCurrentEventMeta();
  if (current) add(current);

  const apiEvents = await fetchEventListFromApi();
  apiEvents.forEach(add);
  getKnownEvents().forEach(add);

  return tracked;
}

async function resolveEventMeta(eventId, analyticsData) {
  const embeddedEvent = analyticsData?.event || analyticsData?.eventMeta || analyticsData?.metadata?.event || null;
  if (embeddedEvent) {
    const remembered = rememberEvent(embeddedEvent);
    if (remembered) return remembered;
  }

  const currentId = getCurrentEventIdentifier();
  if (!state.currentEvent || eventId === 'current' || (currentId && eventId === currentId)) {
    const current = await fetchCurrentEventMeta();
    if (current && (eventId === 'current' || current.slug === eventId || current.eventId === eventId)) {
      return current;
    }
  }

  return getKnownEvent(eventId) || {
    slug: eventId,
    eventId,
    label: eventId,
    date: '',
    venue: '',
    city: '',
    capacity: '',
  };
}

async function fetchMemberPools() {
  const statuses = ['APPROVED', 'PENDING', 'DENIED'];
  const responses = await Promise.all(statuses.map(async (status) => {
    try {
      const { pages, items } = await fetchAllPages(`${ROUTES.ADMIN_MEMBERS}?status=${encodeURIComponent(status)}`, (payload) => payload?.members || []);
      const first = pages[0] || {};
      const deduped = dedupeMembersByPhone(items);
      return {
        status,
        members: deduped,
        total: Math.max(getMemberCount(first), deduped.length),
      };
    } catch (error) {
      addAnalyticsWarning(`${status} members could not be fully loaded; all-time member totals may be partial. ${error?.message || ''}`.trim());
      if (error?.code === 'PAGINATION_LIMIT_REACHED') addAnalyticsWarning(error.message);
      return { status, members: [], total: 0 };
    }
  }));

  return {
    responses,
    members: responses.flatMap((response) => response.members),
    totals: responses.reduce((sum, response) => sum + response.total, 0),
  };
}

async function deriveAttendanceFromEvents(events) {
  if (!events.length) return { uniqueAttendees: 0, repeatAttendees: 0 };

  const attendanceCounts = new Map();

  await Promise.all(events.map(async (event) => {
    try {
      const { items } = await fetchAllPages(`${ROUTES.ADMIN_MEMBER_CONFIRMED}?eventId=${encodeURIComponent(event.slug)}`, (payload) => payload?.members || []);
      const perEventSeen = new Set();
      items.forEach((member) => {
        if (!getAttendedFlag(member)) return;
        const phone = String(member.phone || '').trim();
        if (!phone || perEventSeen.has(phone)) return;
        perEventSeen.add(phone);
        attendanceCounts.set(phone, (attendanceCounts.get(phone) || 0) + 1);
      });
    } catch (error) {
      addAnalyticsWarning(`Attendance history for ${event.slug} could not be fully loaded; repeat-attendance math may be partial. ${error?.message || ''}`.trim());
      if (error?.code === 'PAGINATION_LIMIT_REACHED') addAnalyticsWarning(error.message);
    }
  }));

  return {
    uniqueAttendees: attendanceCounts.size,
    repeatAttendees: [...attendanceCounts.values()].filter((count) => count > 1).length,
  };
}

function renderBodyState(message, compact = false) {
  const body = $('event-analytics-body');
  if (!body) return;
  body.replaceChildren(createNode('div', {
    className: `analytics-empty${compact ? ' analytics-empty-compact' : ''}`,
    text: message,
  }));
}

function createAnalyticsPanel(title) {
  const panel = createNode('div', { className: 'analytics-panel' });
  appendChildren(panel,
    createNode('div', { className: 'analytics-panel-title', text: title }),
  );
  return panel;
}

function createSummaryStat(value, label, tone = '') {
  const stat = createNode('div', { className: 'a-stat' });
  const number = createNode('div', { className: `a-stat-num${tone ? ` ${tone}` : ''}`, text: value });
  const labelNode = createNode('div', { className: 'a-stat-label', text: label });
  return appendChildren(stat, number, labelNode);
}

function createFunnelRow(label, value, rate, fillPct, fillTone = '') {
  const row = createNode('div', { className: 'funnel-row' });
  const fill = createNode('div', { className: `funnel-bar-fill ${pctClass(fillPct)}${fillTone ? ` ${fillTone}` : ''}`.trim() });
  const wrap = createNode('div', { className: 'funnel-bar-wrap' });
  wrap.appendChild(fill);
  return appendChildren(row,
    createNode('div', { className: 'funnel-label', text: label }),
    wrap,
    createNode('div', { className: 'funnel-value', text: value }),
    createNode('div', { className: 'funnel-rate', text: rate || '' }),
  );
}

function createRateCard(value, label, toneClass) {
  const card = createNode('div', { className: 'analytics-rate-card' });
  return appendChildren(card,
    createNode('div', { className: `analytics-rate-value ${toneClass}`.trim(), text: value }),
    createNode('div', { className: 'analytics-rate-label', text: label }),
  );
}

function createSplitCard(title, rows) {
  const card = createNode('div', { className: 'analytics-split-card' });
  appendChildren(card, createNode('div', { className: 'analytics-split-title', text: title }));
  rows.forEach(([label, value]) => {
    const row = createNode('div', { className: 'analytics-stat-row' });
    appendChildren(row,
      createNode('span', { className: 'analytics-split-label', text: label }),
      createNode('span', { className: 'analytics-stat-value', text: value }),
    );
    card.appendChild(row);
  });
  return card;
}

function createTable(headers, rowNodes, emptyLabel = 'No data available') {
  const table = createNode('table', { className: 'analytics-tier-table' });
  const thead = createNode('thead');
  const headRow = createNode('tr');
  headers.forEach((header, index) => {
    headRow.appendChild(createNode('th', { text: header, attrs: index === 0 ? {} : {} }));
  });
  thead.appendChild(headRow);

  const tbody = createNode('tbody');
  if (rowNodes.length) {
    rowNodes.forEach((row) => tbody.appendChild(row));
  } else {
    const row = createNode('tr');
    const cell = createNode('td', { className: 'analytics-empty-cell', text: emptyLabel, attrs: { colspan: String(headers.length) } });
    row.appendChild(cell);
    tbody.appendChild(row);
  }

  appendChildren(table, thead, tbody);
  return table;
}

function createTierRow(tier, tierData) {
  const tierInvited = tierData.invited || 0;
  const tierConfirmed = tierData.confirmed || 0;
  const tierAttended = tierData.attended || 0;
  const confirmPct = tierInvited > 0 ? Math.round((tierConfirmed / tierInvited) * 100) : 0;
  const showPct = tierConfirmed > 0 ? Math.round((tierAttended / tierConfirmed) * 100) : 0;

  const row = createNode('tr');
  row.appendChild(createNode('td', { text: `Tier ${tier}` }));
  row.appendChild(createNode('td', { text: tierInvited }));

  const confirmedCell = createNode('td');
  appendChildren(confirmedCell,
    createNode('span', { text: tierConfirmed }),
    createNode('span', { className: 'analytics-inline-pct', text: `${confirmPct}%` }),
  );
  row.appendChild(confirmedCell);

  const attendedCell = createNode('td');
  appendChildren(attendedCell,
    createNode('span', { text: tierAttended }),
    createNode('span', { className: 'analytics-inline-pct', text: `${showPct}%` }),
  );
  row.appendChild(attendedCell);
  return row;
}

function createWaveRow(wave, waveData) {
  const waveInvited = Number(waveData.invited || 0);
  const waveConfirmed = Number(waveData.confirmed || 0);
  const waveExpected = Number(waveData.expected_headcount || waveData.expectedHeadcount || waveConfirmed);
  const waveCheckedIn = Number(waveData.checked_in_headcount || waveData.checkedInHeadcount || waveData.attended || 0);
  const waveNoShow = Number(waveData.no_show_headcount || waveData.noShowHeadcount || waveData.no_show || 0);
  const waveRate = waveInvited > 0 ? Math.round((waveConfirmed / waveInvited) * 100) : 0;
  const showRate = waveExpected > 0 ? Math.round((waveCheckedIn / waveExpected) * 100) : 0;
  const ghostRate = waveExpected > 0 ? Math.round((waveNoShow / waveExpected) * 100) : 0;
  const label = waveData.label || (wave === 'manual' ? 'Manual / Resend' : `Wave ${wave}`);

  const row = createNode('tr');
  [
    label,
    waveInvited,
    waveConfirmed,
    `${waveRate}%`,
    waveCheckedIn,
    `${showRate}%`,
    waveNoShow,
    `${ghostRate}%`,
  ].forEach((value) => row.appendChild(createNode('td', { text: value })));
  return row;
}

function renderEventAnalyticsView({
  eventId,
  meta,
  totals,
  byGender,
  byTier,
  byWave,
  confirmRatePct,
  showRatePct,
  ghostRatePct,
  noResponseRatePct,
  attendedFunnelPct,
}) {
  const body = $('event-analytics-body');
  if (!body) return;

  const venue = meta.venue || '—';
  const city = meta.city || '—';
  const date = meta.date || '—';
  const capacity = meta.capacity || '—';
  const eventName = meta.slug || eventId;

  const invitedMen = (byGender.M || {}).invited || 0;
  const invitedWomen = (byGender.F || {}).invited || 0;
  const invitedOther = (byGender.O || {}).invited || 0;
  const attendedMen = (byGender.M || {}).attended || 0;
  const attendedWomen = (byGender.F || {}).attended || 0;
  const attendedOther = (byGender.O || {}).attended || 0;

  const totalInvitedGender = invitedMen + invitedWomen + invitedOther;
  const totalAttendedGender = attendedMen + attendedWomen + attendedOther;
  const invitedWomenPct = totalInvitedGender > 0 ? Math.round((invitedWomen / totalInvitedGender) * 100) : 0;
  const invitedMenPct = totalInvitedGender > 0 ? Math.round((invitedMen / totalInvitedGender) * 100) : 0;
  const attendedWomenPct = totalAttendedGender > 0 ? Math.round((attendedWomen / totalAttendedGender) * 100) : 0;
  const attendedMenPct = totalAttendedGender > 0 ? Math.round((attendedMen / totalAttendedGender) * 100) : 0;

  const root = document.createDocumentFragment();

  const header = createNode('div', { className: 'analytics-event-header' });
  const metaParts = [date];
  if (city !== '—') metaParts.push(city);
  if (venue !== '—') metaParts.push(venue);
  if (capacity !== '—') metaParts.push(`Cap ${capacity}`);
  appendChildren(header,
    createNode('div', { className: 'analytics-event-title', text: eventName }),
    createNode('div', { className: 'analytics-event-meta', text: metaParts.filter(Boolean).join(' · ') }),
  );
  root.appendChild(header);

  root.appendChild(createNode('div', { className: 'analytics-block-label', text: 'Event Summary' }));

  const summaryGrid = createNode('div', { className: 'analytics-grid' });
  [
    createSummaryStat(totals.invited || 0, 'Members Invited'),
    createSummaryStat(totals.confirmed || 0, 'Members Confirmed', 'gold'),
    createSummaryStat(totals.expected_headcount || totals.confirmed || 0, 'Expected Headcount', 'gold'),
    createSummaryStat(totals.checked_in_headcount || totals.attended || 0, 'Checked In', 'green'),
    createSummaryStat(totals.plus_one_confirmed || 0, '+1 Confirmed', 'dim'),
    createSummaryStat(totals.plus_one_attended || 0, '+1 Attended', 'green'),
    createSummaryStat(totals.plus_one_no_show || 0, '+1 No-Show', 'dim'),
    createSummaryStat(totals.no_show_headcount || totals.no_show || 0, 'No-Show Headcount', 'dim'),
    createSummaryStat(totals.no_response || 0, 'No Response', 'dim'),
    createSummaryStat(totals.declined || 0, 'Declined', 'dim'),
  ].forEach((card) => summaryGrid.appendChild(card));
  root.appendChild(summaryGrid);

  const firstGrid = createNode('div', { className: 'analytics-grid-2' });

  const funnelPanel = createAnalyticsPanel('Conversion Funnel');
  const funnel = createNode('div', { className: 'analytics-funnel' });
  [
    createFunnelRow('Members Invited', totals.invited || 0, '', 100),
    createFunnelRow('Members Confirmed', totals.confirmed || 0, `${confirmRatePct}%`, confirmRatePct),
    createFunnelRow('Expected Headcount', totals.expected_headcount || totals.confirmed || 0, '', confirmRatePct),
    createFunnelRow('Checked In Headcount', totals.checked_in_headcount || totals.attended || 0, showRatePct == null ? 'Pending' : `${showRatePct}%`, attendedFunnelPct, 'green-fill'),
    createFunnelRow('No-Show Headcount', totals.no_show_headcount || totals.no_show || 0, ghostRatePct == null ? 'Pending' : `${ghostRatePct}%`, ghostRatePct, 'red-fill'),
  ].forEach((row) => funnel.appendChild(row));
  funnelPanel.appendChild(funnel);

  const ratesPanel = createAnalyticsPanel('Rates');
  const rateGrid = createNode('div', { className: 'analytics-rate-grid' });
  [
    createRateCard(`${confirmRatePct}%`, 'Confirm Rate', 'analytics-rate-value-gold'),
    createRateCard(showRatePct == null ? 'Pending' : `${showRatePct}%`, 'Show Rate', 'analytics-rate-value-green'),
    createRateCard(`${noResponseRatePct}%`, 'No Response', 'analytics-rate-value-dim'),
    createRateCard(ghostRatePct == null ? 'Pending' : `${ghostRatePct}%`, 'Ghost Rate', 'analytics-rate-value-red'),
  ].forEach((card) => rateGrid.appendChild(card));
  ratesPanel.appendChild(rateGrid);

  appendChildren(firstGrid, funnelPanel, ratesPanel);
  root.appendChild(firstGrid);

  const secondGrid = createNode('div', { className: 'analytics-grid-2 analytics-grid-2-spaced' });
  const genderPanel = createAnalyticsPanel('Gender Mix');
  const splitGrid = createNode('div', { className: 'analytics-split-grid' });
  splitGrid.appendChild(createSplitCard('Invited', [
    ['Women', `${invitedWomen} · ${invitedWomenPct}%`],
    ['Men', `${invitedMen} · ${invitedMenPct}%`],
    ['Other', invitedOther],
  ]));
  splitGrid.appendChild(createSplitCard('Attended', [
    ['Women', `${attendedWomen} · ${attendedWomenPct}%`],
    ['Men', `${attendedMen} · ${attendedMenPct}%`],
    ['Other', attendedOther],
  ]));
  genderPanel.appendChild(splitGrid);

  const tierPanel = createAnalyticsPanel('By Tier');
  const tierRows = [1, 2, 3]
    .map((tier) => createTierRow(tier, byTier[tier] || byTier[String(tier)] || {}));
  tierPanel.appendChild(createTable(['Tier', 'Invited', 'Confirmed', 'Attended'], tierRows, 'No tier data'));

  appendChildren(secondGrid, genderPanel, tierPanel);
  root.appendChild(secondGrid);

  const wavePanel = createAnalyticsPanel('By Wave');
  const waveOrder = ['1', '2', '3', 'manual'];
  const waveRows = waveOrder
    .filter((wave) => byWave[wave])
    .map((wave) => createWaveRow(wave, byWave[wave] || {}));
  wavePanel.appendChild(createTable(['Wave', 'Invited', 'Confirmed', 'Confirm Rate', 'Checked In', 'Show Rate', 'No-Show', 'Ghost Rate'], waveRows, 'No wave data'));
  // By Wave (8 cols) and Attendance Check (interactive mark buttons) each need the
  // full width — sharing a half-width 2-col grid cell overflowed the panel.
  root.appendChild(wavePanel);

  const attendancePanel = createAnalyticsPanel('Attendance Check');
  const subHeader = createNode('div', { className: 'att-sub-header' });
  appendChildren(subHeader,
    createNode('input', {
      attrs: {
        type: 'text',
        id: 'att-event-id',
        value: eventId,
        placeholder: 'event slug',
      },
    }),
    createNode('button', { className: 'filter-btn attendance-load-btn', text: 'Load Attendance', attrs: { type: 'button' } }),
  );
  const attendanceContainer = createNode('div', { attrs: { id: 'attendance-container' } });
  attendanceContainer.appendChild(createNode('div', { className: 'analytics-empty analytics-empty-compact', text: 'Load confirmed list' }));
  appendChildren(attendancePanel, subHeader, attendanceContainer);

  root.appendChild(attendancePanel);

  body.replaceChildren(root);
}

export async function loadAnalyticsTab() {
  const refreshButton = $('analytics-refresh-btn');
  const refreshStamp = $('analytics-refresh-stamp');
  if (refreshButton) { refreshButton.disabled = true; refreshButton.textContent = 'Refreshing...'; }
  // Fetch tracked events once and share the result — avoids two parallel
  // /admin/event requests on every tab open (FE-L2). Refresh also reloads the
  // currently selected event instead of only refreshing the selector cards.
  const selectedBeforeRefresh = $('analytics-event-select')?.value || '';
  let sharedEvents = null;
  try {
    sharedEvents = await getTrackedEvents();
  } catch (_) {
    // each child will handle its own error if events is null
  }

  await Promise.all([
    loadEventSelector(sharedEvents),
    loadAllTimeStats(sharedEvents),
  ]);

  const selectedAfterRefresh = $('analytics-event-select')?.value || selectedBeforeRefresh;
  if (selectedAfterRefresh) {
    await loadEventAnalytics(selectedAfterRefresh);
  }
  if (refreshStamp) refreshStamp.textContent = `Last refreshed ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
  if (refreshButton) { refreshButton.disabled = false; refreshButton.textContent = 'Refresh'; }
}

export async function loadAllTimeStats(prefetchedEvents = null) {
  resetAnalyticsWarnings();
  try {
    const [events, memberPool] = await Promise.all([
      prefetchedEvents !== null ? Promise.resolve(prefetchedEvents) : getTrackedEvents(),
      fetchMemberPools(),
    ]);

    const membersWithCounts = dedupeMembersByPhone(memberPool.members)
      .filter((member) => getAttendanceCountFromMember(member) !== null);

    let uniqueAttendees = 0;
    let repeatAttendees = 0;

    if (membersWithCounts.length) {
      uniqueAttendees = membersWithCounts.filter((member) => (getAttendanceCountFromMember(member) || 0) > 0).length;
      repeatAttendees = membersWithCounts.filter((member) => (getAttendanceCountFromMember(member) || 0) > 1).length;
    } else {
      const derived = await deriveAttendanceFromEvents(events);
      uniqueAttendees = derived.uniqueAttendees || 0;
      repeatAttendees = derived.repeatAttendees || 0;
    }

    assertAnalyticsComplete();

    setStatText('at-members', memberPool.totals || 0);
    setStatText('at-events', events.length || 0);
    setStatText('at-attendees', uniqueAttendees);
    setStatText('at-repeat', repeatAttendees);
    renderAnalyticsHealth();
  } catch (error) {
    const message = reportError('All-time analytics failed', error, {
      fallback: 'Check backend responses and retry.',
    });
    setStatText('at-members', '—');
    setStatText('at-events', '—');
    setStatText('at-attendees', '—');
    setStatText('at-repeat', '—');
    renderAnalyticsHealth(message);
  }
}

export async function loadEventSelector(prefetchedEvents = null) {
  const select = $('analytics-event-select');
  if (!select) return;

  try {
    const trackedEvents = prefetchedEvents !== null ? prefetchedEvents : await getTrackedEvents();
    const selectedValue = select.value;

    select.replaceChildren(createNode('option', { text: '— select event —', attrs: { value: '' } }));
    trackedEvents.forEach((event) => {
      const option = document.createElement('option');
      option.value = event.slug;
      option.textContent = buildEventLabel(event, event.slug);
      select.appendChild(option);
    });

    if (selectedValue && trackedEvents.some((event) => event.slug === selectedValue)) {
      select.value = selectedValue;
    }

    setStatText('at-events', trackedEvents.length || 0);
  } catch (error) {
    reportError('Analytics event list failed', error, {
      fallback: 'Unable to refresh event list.',
    });
  }
}

export async function loadEventAnalytics(eventId) {
  const select = $('analytics-event-select');
  if (select && eventId && select.value !== eventId) select.value = eventId;

  if (!eventId) {
    renderBodyState('Select an event above');
    $('analytics-subtitle').textContent = 'Select an event';
    return;
  }

  renderBodyState('Loading...');
  $('analytics-subtitle').textContent = eventId;

  try {
    const analyticsData = await apiJson(`${ROUTES.ADMIN_EVENT_ANALYTICS}?eventId=${encodeURIComponent(eventId)}`);
    const analytics = analyticsData.analytics || {};
    const meta = await resolveEventMeta(eventId, analyticsData);
    const totals = analytics.totals || {};
    const byGender = analytics.by_gender || {};
    const byTier = analytics.by_tier || {};
    const byWave = analytics.by_wave || {};

    const rates = analytics.rates || {};
    const confirmRatePct = Math.round(Number(rates.confirm_rate ?? ((totals.invited > 0 ? (totals.confirmed / totals.invited) * 100 : 0))) || 0);
    const showRatePct = analytics.attendanceSettled === false ? null : Math.round(Number(rates.show_rate ?? ((totals.expected_headcount > 0 ? ((totals.checked_in_headcount || totals.attended || 0) / totals.expected_headcount) * 100 : 0))) || 0);
    const noResponseRatePct = Math.round(Number(rates.no_response_rate ?? ((totals.invited > 0 ? ((totals.no_response || 0) / totals.invited) * 100 : 0))) || 0);
    const ghostRatePct = analytics.attendanceSettled === false ? null : Math.round(Number(rates.no_show_rate ?? ((totals.expected_headcount > 0 ? ((totals.no_show_headcount || totals.no_show || 0) / totals.expected_headcount) * 100 : 0))) || 0);

    const funnelMax = Math.max(totals.invited || 0, 1);
    const attendedFunnelPct = Math.round(((totals.checked_in_headcount || totals.attended || 0) / funnelMax) * 100);

    $('analytics-subtitle').textContent = buildEventLabel(meta, eventId);
    renderEventAnalyticsView({
      eventId,
      meta,
      totals,
      byGender,
      byTier,
      byWave,
      confirmRatePct,
      showRatePct,
      ghostRatePct,
      noResponseRatePct,
      attendedFunnelPct,
    });

    // Attendance is loaded on demand via the "Load Attendance" button —
    // not auto-fired here to keep the button meaningful and avoid extra API calls
  } catch (error) {
    const message = reportError('Event analytics failed', error, {
      fallback: 'Unable to load event analytics.',
    });
    renderBodyState(`Failed to load — ${message}`);
  }
}

window.addEventListener('rsvp:attendance-updated', async (event) => {
  const selected = $('analytics-event-select')?.value || event.detail?.eventId || '';
  if (selected) await loadEventAnalytics(selected);
});
