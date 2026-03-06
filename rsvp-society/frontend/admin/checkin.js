import { apiFetch, apiJson } from './api.js';
import { ROUTES } from './constants.js';
import { state } from './state.js';

let allMembers = [];
let checkedIn = new Set();
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

function showToast(msg, type = '') {
  const t = $('toast');
  t.textContent = msg;
  t.className = `toast show ${type}`.trim();
  setTimeout(() => { t.className = 'toast'; }, 2500);
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
    $('app').style.display = 'flex';
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
  $('app').style.display = 'none';
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
    allMembers = (data.members || []).sort((a, b) => {
      const aKey = (a.lastName || a.name || '').toLowerCase();
      const bKey = (b.lastName || b.name || '').toLowerCase();
      return aKey.localeCompare(bKey);
    });
    checkedIn = new Set(allMembers.filter((m) => m.checkedIn).map((m) => m.phone));
    updateCounter();
    renderAlphaBar();
    renderGuestList(allMembers);
  } catch (err) {
    renderStateMessage($('guest-list'), 'Failed to load — check connection');
  }
}


function renderStateMessage(container, message) {
  container.replaceChildren();
  const msg = document.createElement('div');
  msg.className = 'state-msg';
  msg.textContent = message;
  container.appendChild(msg);
}

function updateCounter() {
  const total = allMembers.length;
  const inCount = checkedIn.size;
  $('counter-in').textContent = inCount;
  $('counter-total').textContent = total;
  const pct = total > 0 ? Math.round((inCount / total) * 100) : 0;
  $('counter-pct').textContent = total > 0 ? `${pct}%` : '';
  $('progress-bar').style.width = `${pct}%`;
}

function renderAlphaBar() {
  const letters = new Set(allMembers.map((m) => (m.lastName || m.name || '#')[0].toUpperCase()));
  const bar = $('alpha-bar');
  bar.replaceChildren();
  'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').forEach((l) => {
    const btn = document.createElement('button');
    btn.className = `alpha-btn ${letters.has(l) ? 'has-members' : ''}`.trim();
    btn.dataset.jump = l;
    btn.textContent = l;
    bar.appendChild(btn);
  });
}

function jumpTo(letter) {
  const el = document.getElementById(`group-${letter}`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderGuestList(members) {
  const list = $('guest-list');
  if (!members.length) {
    renderStateMessage(list, 'No guests found');
    return;
  }

  const groups = {};
  members.forEach((m) => {
    const sortName = m.lastName || m.name || '#';
    const letter = sortName[0].toUpperCase();
    if (!groups[letter]) groups[letter] = [];
    groups[letter].push(m);
  });

  list.replaceChildren();
  Object.keys(groups).sort().forEach((letter) => {
    const group = document.createElement('div');
    group.id = `group-${letter}`;

    const header = document.createElement('div');
    header.className = 'alpha-header';
    header.textContent = letter;
    group.appendChild(header);

    groups[letter].forEach((member) => {
      group.appendChild(renderRow(member));
    });

    list.appendChild(group);
  });
}

function displayName(m) {
  const first = (m.name || '').trim();
  const last  = (m.lastName || '').trim();
  if (first && last) return `${first} ${last}`;
  return first || last || '—';
}

function renderRow(m) {
  const alreadyIn = checkedIn.has(m.phone);
  const safe = m.phone.replace(/\D/g, '');

  const row = document.createElement('div');
  row.className = `guest-row ${alreadyIn ? 'checked-in' : ''}`.trim();
  row.id = `row-${safe}`;

  const info = document.createElement('div');
  info.className = 'guest-info';

  const nameEl = document.createElement('div');
  nameEl.className = 'guest-name';
  nameEl.textContent = displayName(m);

  const metaEl = document.createElement('div');
  metaEl.className = 'guest-meta';
  metaEl.textContent = m.phone || '—';

  info.appendChild(nameEl);
  info.appendChild(metaEl);

  const button = document.createElement('button');
  button.className = `checkin-btn ${alreadyIn ? 'done' : ''}`.trim();
  button.id = `btn-${safe}`;
  button.dataset.phone = m.phone || '';
  button.dataset.name = displayName(m);
  button.textContent = alreadyIn ? '✓ In' : 'Check In';
  if (alreadyIn) button.disabled = true;

  row.appendChild(info);
  row.appendChild(button);
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
    (m.name || '').toLowerCase().includes(q) ||
    (m.lastName || '').toLowerCase().includes(q) ||
    (m.phone || '').includes(q)
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
    showToast(`${name || phone} — checked in`, 'success');
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = 'Check In'; }
    showToast('Failed — try again', 'error');
  }
}

function bindStaticUi() {
  $('login-btn')?.addEventListener('click', () => { void doLogin(); });
  $('search-input')?.addEventListener('input', (e) => onSearch(e.target.value));
  $('clear-btn')?.addEventListener('click', clearSearch);
  document.querySelector('.signout-btn')?.addEventListener('click', doLogout);
}

$('token-input')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') doLogin();
});

window.addEventListener('DOMContentLoaded', () => {
  bindStaticUi();
  const saved = sessionStorage.getItem('rsvp_checkin_token');
  const exp = parseInt(sessionStorage.getItem('rsvp_checkin_token_exp') || '0', 10);
  if (saved && Date.now() < exp) {
    $('token-input').value = saved;
    doLogin();
  } else if (saved) {
    sessionStorage.removeItem('rsvp_checkin_token');
    sessionStorage.removeItem('rsvp_checkin_token_exp');
  }
});

// Delegated event listeners — no inline onclick anywhere
document.addEventListener('click', (e) => {
  // Alpha bar jump
  const jumpBtn = e.target.closest('[data-jump]');
  if (jumpBtn) {
    jumpTo(jumpBtn.dataset.jump);
    return;
  }
  // Check-in button
  const ciBtn = e.target.closest('.checkin-btn[data-phone]');
  if (ciBtn && !ciBtn.disabled) {
    checkIn(ciBtn.dataset.phone, ciBtn.dataset.name);
    return;
  }
});

