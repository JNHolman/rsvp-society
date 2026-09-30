import { apiFetch, apiJson } from './api.js';
import { ROUTES } from './constants.js';
import { state } from './state.js';

let allMembers = [];
let checkedIn = new Set();
let checkedInPlusOnes = new Set();
let currentEventId = 'current';

function syncAdminToken(token = '') {
  state.adminToken = String(token || '').trim();
}

function withEventId(path, eventId) {
  const url = new URL(path, 'https://placeholder.local');
  url.searchParams.set('eventId', eventId || 'current');
  return `${url.pathname}${url.search}`;
}

function $(id) {
  return document.getElementById(id);
}

function node(tag, options = {}) {
  const el = document.createElement(tag);
  if (options.className) el.className = options.className;
  if (options.text !== undefined && options.text !== null) el.textContent = String(options.text);
  if (options.id) el.id = options.id;
  if (options.dataset) {
    Object.entries(options.dataset).forEach(([key, value]) => {
      if (value !== undefined && value !== null) el.dataset[key] = String(value);
    });
  }
  if (options.attrs) {
    Object.entries(options.attrs).forEach(([key, value]) => {
      if (value === false || value === null || value === undefined) return;
      if (value === true) el.setAttribute(key, '');
      else el.setAttribute(key, String(value));
    });
  }
  return el;
}

function displayName(member = {}) {
  const first = String(member.name || '').trim();
  const last = String(member.lastName || '').trim();
  return [first, last].filter(Boolean).join(' ') || first || '—';
}

function alphaKey(member = {}) {
  return displayName(member).charAt(0).toUpperCase() || '#';
}

function showToast(msg, type = '') {
  const t = $('toast');
  t.textContent = msg;
  t.className = `toast show ${type}`.trim();
  setTimeout(() => { t.className = 'toast'; }, 2500);
}

function errorMessage(error, fallback = 'try again') {
  return error?.data?.reason
    || error?.data?.error
    || error?.data?.message
    || error?.message
    || fallback;
}

async function requestJson(path, options = {}, config = {}) {
  syncAdminToken(state.adminToken);
  return apiJson(path, options, config);
}

async function validateToken(token) {
  syncAdminToken(token);
  return apiFetch(ROUTES.ADMIN_EVENT, {}, { auth: true });
}

async function doLogin() {
  const val = $('token-input').value.trim();
  const errEl = $('login-error');
  if (!val) return;

  const btn = $('login-btn');
  btn.textContent = 'Loading...';
  errEl.textContent = '';

  try {
    const res = await validateToken(val);
    if (res.status === 401 || res.status === 403) {
      errEl.textContent = 'Invalid token.';
      btn.textContent = 'Enter';
      return;
    }
    if (!res.ok) {
      errEl.textContent = `Error ${res.status}.`;
      btn.textContent = 'Enter';
      return;
    }

    state.adminToken = val;
    sessionStorage.setItem('rsvp_checkin_token', val);
    sessionStorage.setItem('rsvp_checkin_token_exp', String(Date.now() + 8 * 60 * 60 * 1000));
    $('login-screen').style.display = 'none';
    if ($('app')) {
      $('app').hidden = false;
      $('app').removeAttribute('hidden');
      $('app').style.display = 'flex';
    }
    btn.textContent = 'Enter';
    await loadEvent();
    await loadGuests();
  } catch (err) {
    errEl.textContent = 'Network error.';
    btn.textContent = 'Enter';
  }
}

function doLogout() {
  syncAdminToken('');
  sessionStorage.removeItem('rsvp_checkin_token');
  sessionStorage.removeItem('rsvp_checkin_token_exp');
  allMembers = [];
  checkedIn = new Set();
  if ($('app')) {
    $('app').style.display = 'none';
    $('app').hidden = true;
    $('app').setAttribute('hidden', '');
  }
  $('login-screen').style.display = 'flex';
  $('token-input').value = '';
}

async function loadEvent() {
  try {
    const data = await requestJson(ROUTES.ADMIN_EVENT);
    if (data.ok && data.event && data.event.date) {
      const ev = data.event;
      currentEventId = ev.eventSlug || ev.eventId || 'current';
      $('banner-name').textContent = currentEventId || '—';
      $('banner-date').textContent = ev.date || '—';
      $('banner-venue').textContent = ev.venue || '—';
      $('banner-time').textContent = ev.startTime || '—';
      $('event-banner').style.display = 'block';
    }
  } catch (e) {
    console.error('loadEvent:', e);
  }
}

async function loadGuests() {
  try {
    const data = await requestJson(withEventId(ROUTES.ADMIN_MEMBER_CONFIRMED, currentEventId));
    allMembers = (data.members || []).sort((a, b) => displayName(a).toLowerCase().localeCompare(displayName(b).toLowerCase()));
    checkedIn = new Set(allMembers.filter((m) => m.checkedIn).map((m) => m.phone));
    checkedInPlusOnes = new Set(allMembers.filter((m) => m.plusOneCheckedIn).map((m) => m.phone));
    updateCounter();
    renderAlphaBar();
    renderGuestList(allMembers);
  } catch (err) {
    const msg = errorMessage(err, 'check connection');
    $('guest-list').replaceChildren(node('div', { className: 'state-msg', text: `Failed to load — ${msg}` }));
    showToast(`Guest load failed — ${msg}`, 'error');
  }
}

function updateCounter() {
  const plusOneTotal = allMembers.filter((m) => m.plusOneName).length;
  const total = allMembers.length + plusOneTotal;
  const inCount = checkedIn.size + checkedInPlusOnes.size;
  $('counter-in').textContent = inCount;
  $('counter-total').textContent = total;
  const pct = total > 0 ? Math.round((inCount / total) * 100) : 0;
  $('counter-pct').textContent = total > 0 ? `${pct}%` : '';
  $('progress-bar').style.width = `${pct}%`;
}

function renderAlphaBar() {
  const letters = new Set(allMembers.map((m) => alphaKey(m)));
  const bar = $('alpha-bar');
  const buttons = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').map((l) => {
    const btn = node('button', {
      className: `alpha-btn ${letters.has(l) ? 'has-members' : ''}`.trim(),
      text: l,
      dataset: { letter: l },
      attrs: { type: 'button' },
    });
    return btn;
  });
  bar.replaceChildren(...buttons);
}

function jumpTo(letter) {
  const el = document.getElementById(`group-${letter}`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderGuestList(members) {
  const list = $('guest-list');
  if (!members.length) {
    list.replaceChildren(node('div', { className: 'state-msg', text: 'No guests found' }));
    return;
  }

  const groups = {};
  members.forEach((m) => {
    const letter = alphaKey(m);
    if (!groups[letter]) groups[letter] = [];
    groups[letter].push(m);
  });

  const children = Object.keys(groups).sort().map((letter) => {
    const group = node('div', { id: `group-${letter}` });
    group.appendChild(node('div', { className: 'alpha-header', text: letter }));
    groups[letter].forEach((m) => group.appendChild(renderRow(m)));
    return group;
  });
  list.replaceChildren(...children);
}

function renderPlusOne(m) {
  if (!m.plusOneName) return null;
  const safe = String(m.phone || '').replace(/\D/g, '');
  const alreadyIn = checkedInPlusOnes.has(m.phone);
  const row = node('div', {
    className: `plus-one-row ${alreadyIn ? 'checked-in-plus-one' : ''}`.trim(),
    id: `plusone-row-${safe}`,
  });
  const name = node('span', { className: 'plus-one-name', text: `+1: ${m.plusOneName}` });
  if (m.plusOneIsMember) {
    // no marker needed
  } else {
    name.appendChild(node('span', { className: 'plus-one-star', text: ' ✦', attrs: { title: 'Not a member' } }));
  }
  const btn = node('button', {
    className: `plusone-checkin-btn ${alreadyIn ? 'done' : ''}`.trim(),
    id: `plusone-btn-${safe}`,
    text: alreadyIn ? '✓ +1 In' : 'Check +1',
    dataset: { sponsorPhone: m.phone, name: m.plusOneName },
    attrs: { type: 'button', disabled: alreadyIn },
  });
  row.appendChild(name);
  row.appendChild(btn);
  return row;
}

function renderRow(m) {
  const alreadyIn = checkedIn.has(m.phone);
  const safe = String(m.phone || '').replace(/\D/g, '');
  const row = node('div', {
    className: `guest-row ${alreadyIn ? 'checked-in' : ''}`.trim(),
    id: `row-${safe}`,
  });
  const info = node('div', { className: 'guest-info' });
  info.appendChild(node('div', { className: 'guest-name', text: displayName(m) }));
  const plus = renderPlusOne(m);
  if (plus) info.appendChild(plus);
  info.appendChild(node('div', { className: 'guest-meta', text: m.phone || '' }));
  const btn = node('button', {
    className: `checkin-btn ${alreadyIn ? 'done' : ''}`.trim(),
    id: `btn-${safe}`,
    text: alreadyIn ? '✓ In' : 'Check In',
    dataset: { phone: m.phone, name: displayName(m) },
    attrs: { type: 'button', disabled: alreadyIn },
  });
  row.appendChild(info);
  row.appendChild(btn);
  return row;
}
function onSearch(val) {
  $('clear-btn').classList.toggle('visible', val.length > 0);
  const alphaBar = $('alpha-bar');
  if (!val.trim()) {
    alphaBar.style.display = 'flex';
    renderGuestList(allMembers);
    return;
  }
  alphaBar.style.display = 'none';
  const q = val.toLowerCase();
  renderGuestList(allMembers.filter((m) =>
    displayName(m).toLowerCase().includes(q)
    || (m.lastName || '').toLowerCase().includes(q)
    || (m.phone || '').includes(q)
    || (m.plusOneName || '').toLowerCase().includes(q)
  ));
}

function clearSearch() {
  const input = $('search-input');
  input.value = '';
  $('clear-btn').classList.remove('visible');
  $('alpha-bar').style.display = 'flex';
  renderGuestList(allMembers);
  input.focus();
}

async function checkIn(phone, name) {
  const safe = phone.replace(/\D/g, '');
  const btn = document.getElementById(`btn-${safe}`);
  const row = document.getElementById(`row-${safe}`);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }

  try {
    await requestJson(ROUTES.ADMIN_MEMBER_ATTENDANCE, {
      method: 'POST',
      body: { phone, attended: true, eventId: currentEventId },
    });

    checkedIn.add(phone);
    if (btn) { btn.textContent = '✓ In'; btn.classList.add('done'); }
    if (row) row.classList.add('checked-in');
    updateCounter();
    showToast(`${name || phone} checked in`, 'success');
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = 'Check In'; }
    showToast(`Failed — ${errorMessage(err)}`, 'error');
  }
}


async function checkInPlusOne(sponsorPhone, name) {
  const safe = sponsorPhone.replace(/\D/g, '');
  const btn = document.getElementById(`plusone-btn-${safe}`);
  const row = document.getElementById(`plusone-row-${safe}`);
  if (btn) { btn.disabled = true; btn.textContent = '...'; }

  try {
    await requestJson(ROUTES.ADMIN_MEMBER_ATTENDANCE, {
      method: 'POST',
      body: { phone: sponsorPhone, sponsorPhone, attended: true, eventId: currentEventId, guestType: 'plus_one' },
    });

    checkedInPlusOnes.add(sponsorPhone);
    if (btn) { btn.textContent = '✓ +1 In'; btn.classList.add('done'); }
    if (row) row.classList.add('checked-in-plus-one');
    updateCounter();
    showToast(`${name || 'Plus one'} checked in`, 'success');
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = 'Check +1'; }
    showToast(`+1 failed — ${errorMessage(err)}`, 'error');
  }
}

function escHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

window.addEventListener('DOMContentLoaded', () => {
  $('token-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doLogin();
  });
  $('login-btn')?.addEventListener('click', doLogin);
  $('signout-btn')?.addEventListener('click', doLogout);
  $('refresh-btn')?.addEventListener('click', async () => {
    const btn = $('refresh-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing...'; }
    try {
      await loadEvent();
      await loadGuests();
      showToast('Guest list refreshed', 'success');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
    }
  });
  $('close-event-btn')?.addEventListener('click', async () => {
    const total = parseInt($('counter-total')?.textContent || '0', 10);
    const checked = parseInt($('counter-in')?.textContent || '0', 10);
    const pending = Math.max(0, total - checked);
    const ok = window.confirm(
      `Close this event?\n\nThis finalizes attendance and adjusts tiers. ` +
      `${pending} confirmed guest${pending === 1 ? '' : 's'} not checked in will be ` +
      `marked no-show. This can't be undone.`
    );
    if (!ok) return;
    const btn = $('close-event-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Closing...'; }
    try {
      let cursor = '';
      let members = 0;
      let plusOnes = 0;
      let rows = 0;
      let res;
      do {
        res = await requestJson(ROUTES.ADMIN_EVENTS_FINALIZE, {
          method: 'POST', body: { eventSlug: currentEventId, cursor },
        }, { auth: true });
        if (!res?.ok) throw new Error(res?.error || 'finalization failed');
        members += Number(res.memberNoShows || 0);
        plusOnes += Number(res.plusOneNoShows || 0);
        rows += Number(res.rowsScanned || 0);
        cursor = res.cursor || '';
        if (!res.done && btn) btn.textContent = `Closing... ${rows} checked`;
      } while (!res.done && !res.alreadyFinalized);
      if (res?.alreadyFinalized) {
        showToast('Event was already closed', 'success');
      } else {
        showToast(`Closed. ${members} member and ${plusOnes} +1 no-shows recorded`, 'success');
      }
    } catch (err) {
      showToast('Could not close event', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Close Event'; }
    }
  });
  $('search-input')?.addEventListener('input', (e) => onSearch(e.target.value));
  $('clear-btn')?.addEventListener('click', clearSearch);

  // Delegated listener for check-in buttons — avoids onclick injection issues
  document.getElementById('guest-list')?.addEventListener('click', (e) => {
    const plusBtn = e.target.closest('.plusone-checkin-btn:not(.done):not([disabled])');
    if (plusBtn) {
      checkInPlusOne(plusBtn.dataset.sponsorPhone || '', plusBtn.dataset.name || '');
      return;
    }
    const btn = e.target.closest('.checkin-btn:not(.done):not([disabled])');
    if (btn) checkIn(btn.dataset.phone || '', btn.dataset.name || '');
  });

  // Delegated listener for alpha-bar letter buttons
  $('alpha-bar')?.addEventListener('click', (e) => {
    const btn = e.target.closest('.alpha-btn[data-letter]');
    if (btn) jumpTo(btn.dataset.letter);
  });

  const saved = sessionStorage.getItem('rsvp_checkin_token') || sessionStorage.getItem('rsvp_admin_token');
  const exp = parseInt(sessionStorage.getItem('rsvp_checkin_token_exp') || sessionStorage.getItem('rsvp_token_exp') || '0', 10);
  if (saved && Date.now() < exp) {
    $('token-input').value = saved;
    doLogin();
  } else if (saved) {
    sessionStorage.removeItem('rsvp_checkin_token');
    sessionStorage.removeItem('rsvp_checkin_token_exp');
  }
});
