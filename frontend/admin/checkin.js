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
    allMembers = (data.members || []).sort((a, b) => displayName(a).toLowerCase().localeCompare(displayName(b).toLowerCase()));
    checkedIn = new Set(allMembers.filter((m) => m.checkedIn).map((m) => m.phone));
    updateCounter();
    renderAlphaBar();
    renderGuestList(allMembers);
  } catch (err) {
    $('guest-list').innerHTML = '<div class="state-msg">Failed to load — check connection</div>';
  }
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
  const letters = new Set(allMembers.map((m) => alphaKey(m)));
  const bar = $('alpha-bar');
  bar.innerHTML = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').map((l) =>
    `<button class="alpha-btn ${letters.has(l) ? 'has-members' : ''}" onclick="jumpTo('${l}')">${l}</button>`
  ).join('');
}

function jumpTo(letter) {
  const el = document.getElementById(`group-${letter}`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderGuestList(members) {
  const list = $('guest-list');
  if (!members.length) {
    list.innerHTML = '<div class="state-msg">No guests found</div>';
    return;
  }

  const groups = {};
  members.forEach((m) => {
    const letter = alphaKey(m);
    if (!groups[letter]) groups[letter] = [];
    groups[letter].push(m);
  });

  list.innerHTML = Object.keys(groups).sort().map((letter) => `
    <div id="group-${letter}">
      <div class="alpha-header">${letter}</div>
      ${groups[letter].map((m) => renderRow(m)).join('')}
    </div>
  `).join('');
}

function renderRow(m) {
  const alreadyIn = checkedIn.has(m.phone);
  const safe = m.phone.replace(/\D/g, '');
  const guestName = displayName(m);
  return `
    <div class="guest-row ${alreadyIn ? 'checked-in' : ''}" id="row-${safe}">
      <div class="guest-info">
        <div class="guest-name">${escHtml(displayName(m))}</div>
        <div class="guest-meta">${m.phone}</div>
      </div>
      <button
        class="checkin-btn ${alreadyIn ? 'done' : ''}"
        id="btn-${safe}"
        onclick="checkIn('${m.phone}', ${JSON.stringify(guestName)})"
        ${alreadyIn ? 'disabled' : ''}
      >${alreadyIn ? '✓ In' : 'Check In'}</button>
    </div>`;
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
    showToast('Failed — try again', 'error');
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

$('token-input')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') doLogin();
});

window.addEventListener('DOMContentLoaded', () => {
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

window.doLogin = doLogin;
window.doLogout = doLogout;
window.onSearch = onSearch;
window.clearSearch = clearSearch;
window.jumpTo = jumpTo;
window.checkIn = checkIn;
