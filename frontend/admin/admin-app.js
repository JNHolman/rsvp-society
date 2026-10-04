import { syncReminderTimingUi } from './event.js';
import { apiJson } from './api.js';
import { ROUTES } from './constants.js';
import { loadAnalyticsTab, loadEventAnalytics } from './analytics.js';
import { deleteCurrentEvent, draftWithJade, duplicateCurrentEvent, loadCurrentEvent, previewJadeMessages, saveEvent, saveEventAndSetActive, startNewEvent, updateVibeTags } from './event.js';
import { closeImport, confirmImport, handleDrop, handleFileSelect, openImport, refreshImportPreview } from './import.js';
import {
  clearPreviewSelection,
  loadEventIntoInviteForm,
  removeFromPreview,
  renderPreview,
  runPreview,
  sendConfirmedUpdate,
  prefillVenueReveal,
  sendInvites,
  togglePreviewSelection,
  updatePreviewCount,
} from './invite.js';
import {
  deleteMember,
  filterMembers,
  refreshMembersWithServerFilters,
  goPage,
  loadMembers,
  loadStats,
  openMemberDetail,
  setStatus,
  updateGender,
  updateTier,
} from './members.js';
import { resetSessionState, state } from './state.js';
import { $, reportError, setHidden } from './ui.js';

async function doLogin() {
  const token = $('token-input').value.trim();
  const errorNode = $('login-error');
  if (!token) return;

  const button = $('login-btn');
  const previousToken = state.adminToken;
  button.textContent = 'Checking...';
  errorNode.textContent = '';

  try {
    state.adminToken = token;
    const initialPendingPayload = await apiJson(`${ROUTES.ADMIN_MEMBERS}?status=PENDING&limit=${state.pageSize || 50}&includeTotal=1`);

    state.activeTab = 'overview';
    state.currentStatus = 'PENDING';
    sessionStorage.setItem('rsvp_admin_token', token);
    sessionStorage.setItem('rsvp_token_exp', String(Date.now() + (8 * 60 * 60 * 1000)));

    setHidden('login-screen', true);
    setHidden('app', false);
    button.textContent = 'Enter';

    await loadMembers('PENDING', { initialPayload: initialPendingPayload });
    await loadStats({ skipStatuses: ['PENDING'] });
    await refreshOverview();
  } catch (error) {
    state.adminToken = previousToken;
    if (error?.status === 401 || error?.status === 403) {
      errorNode.textContent = 'Invalid token.';
    } else if (error?.status) {
      errorNode.textContent = `Error ${error.status}. Check API URL.`;
    } else {
      errorNode.textContent = 'Network error — check API URL in settings.';
    }
    button.textContent = 'Enter';
    reportError('Login failed', error, { toast: false, fallback: 'Unable to start admin session.' });
  }
}

function doLogout() {
  resetSessionState();
  sessionStorage.removeItem('rsvp_admin_token');
  sessionStorage.removeItem('rsvp_token_exp');
  setHidden('app', true);
  setHidden('login-screen', false);
  $('token-input').value = '';
  $('login-error').textContent = '';
}

function formatOverviewEvent(event = {}) {
  const name = event.label || event.eventLabel || event.name || event.eventSlug || event.eventId || 'No active event';
  const details = [event.date, event.startTime, event.venue].filter(Boolean).join(' · ');
  return { name, details: details || 'Create or publish an event to begin operations.' };
}

async function refreshOverview() {
  const pending = document.getElementById('overview-pending');
  const approved = document.getElementById('overview-approved');
  if (pending) pending.textContent = Number.isFinite(state.memberTotals?.PENDING) ? String(state.memberTotals.PENDING) : '—';
  if (approved) approved.textContent = Number.isFinite(state.memberTotals?.APPROVED) ? String(state.memberTotals.APPROVED) : '—';
  try {
    await loadCurrentEvent();
    const formatted = formatOverviewEvent(state.currentEvent || {});
    const name = document.getElementById('overview-event-name');
    const meta = document.getElementById('overview-event-meta');
    if (name) name.textContent = formatted.name;
    if (meta) meta.textContent = formatted.details;
  } catch {
    const name = document.getElementById('overview-event-name');
    const meta = document.getElementById('overview-event-meta');
    if (name) name.textContent = 'Event unavailable';
    if (meta) meta.textContent = 'Open Event to retry.';
  }
}

function setTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll('.tab-btn').forEach((button) => button.classList.remove('active'));
  const active = document.getElementById(`tab-${tab}`);
  if (active) active.classList.add('active');
}

async function showSection(section) {
  state.activeTab = section;
  setHidden('overview-section', section !== 'overview');
  setHidden('members-section', section !== 'members');
  setHidden('invite-section', section !== 'invite');
  setHidden('event-section', section !== 'event');
  setHidden('analytics-section', section !== 'analytics');

  if (section === 'overview') await refreshOverview();
  if (section === 'event') await loadCurrentEvent();
  if (section === 'invite') await loadEventIntoInviteForm();
  if (section === 'analytics') await loadAnalyticsTab();
}

async function goToSection(section) {
  setTab(section);
  if (window.scrollY > 0) window.scrollTo({ top: 0, behavior: 'auto' });
  await showSection(section);
}

function bindShellEvents() {
  $('login-btn')?.addEventListener('click', doLogin);
  $('logout-btn')?.addEventListener('click', doLogout);
  $('tab-overview')?.addEventListener('click', () => goToSection('overview'));
  $('tab-members')?.addEventListener('click', () => goToSection('members'));
  $('overview-event-btn')?.addEventListener('click', () => goToSection('event'));
  $('overview-invite-btn')?.addEventListener('click', () => goToSection('invite'));
  $('overview-pending-card')?.addEventListener('click', async () => { await goToSection('members'); await loadMembers('PENDING'); });
  $('overview-approved-card')?.addEventListener('click', async () => { await goToSection('members'); await loadMembers('APPROVED'); });
  $('overview-event-card')?.addEventListener('click', () => goToSection('event'));
  $('overview-invite-card')?.addEventListener('click', () => goToSection('invite'));
  $('overview-analytics-card')?.addEventListener('click', () => goToSection('analytics'));
  $('tab-event')?.addEventListener('click', () => goToSection('event'));
  $('tab-invite')?.addEventListener('click', () => goToSection('invite'));
  $('tab-analytics')?.addEventListener('click', () => goToSection('analytics'));
  $('filter-PENDING')?.addEventListener('click', () => loadMembers('PENDING'));
  $('filter-APPROVED')?.addEventListener('click', () => loadMembers('APPROVED'));
  $('filter-DENIED')?.addEventListener('click', () => loadMembers('DENIED'));
  $('import-open-btn')?.addEventListener('click', openImport);
  $('import-close-btn')?.addEventListener('click', closeImport);
  $('import-cancel-btn')?.addEventListener('click', closeImport);
  $('import-confirm-btn')?.addEventListener('click', confirmImport);
  $('import-sms-consent')?.addEventListener('change', refreshImportPreview);
  $('member-search')?.addEventListener('input', () => refreshMembersWithServerFilters());
  $('preview-run-btn')?.addEventListener('click', runPreview);
  $('send-btn')?.addEventListener('click', sendInvites);
  $('confirmed-update-send-btn')?.addEventListener('click', sendConfirmedUpdate);
  $('reveal-venue-btn')?.addEventListener('click', prefillVenueReveal);
  $('invite-back-btn')?.addEventListener('click', () => goToSection('event'));
  $('ev-type')?.addEventListener('change', updateVibeTags);
  $('event-new-btn')?.addEventListener('click', startNewEvent);
  $('event-duplicate-btn')?.addEventListener('click', duplicateCurrentEvent);
  $('event-hard-delete-btn')?.addEventListener('click', deleteCurrentEvent);
  $('event-save-btn')?.addEventListener('click', () => saveEvent({ setActive: false }));
  $('event-save-active-btn')?.addEventListener('click', saveEventAndSetActive);
  $('event-preview-btn')?.addEventListener('click', previewJadeMessages);
  $('event-draft-jade-btn')?.addEventListener('click', draftWithJade);
  $('event-next-btn')?.addEventListener('click', () => goToSection('invite'));
$('ev-venue-mode')?.addEventListener('change', syncReminderTimingUi);
  $('analytics-event-select')?.addEventListener('change', (event) => loadEventAnalytics(event.target.value));
  $('analytics-refresh-btn')?.addEventListener('click', () => loadAnalyticsTab());
  $('import-modal')?.addEventListener('click', (event) => {
    if (event.target === event.currentTarget) closeImport();
  });
  $('csv-file-input')?.addEventListener('change', handleFileSelect);
  $('import-drop')?.addEventListener('click', () => $('csv-file-input')?.click());
  $('import-drop')?.addEventListener('dragover', (event) => {
    event.preventDefault();
    event.currentTarget.classList.add('is-drag-active');
  });
  $('import-drop')?.addEventListener('dragleave', (event) => {
    event.currentTarget.classList.remove('is-drag-active');
  });
  $('import-drop')?.addEventListener('drop', (event) => {
    event.currentTarget.classList.remove('is-drag-active');
    handleDrop(event);
  });
}

function bindDelegatedEvents() {
  document.addEventListener('click', (event) => {
    const previewRemove = event.target.closest('.preview-remove-btn');
    if (previewRemove) {
      removeFromPreview(previewRemove.dataset.phone || '');
      return;
    }

    const clearSelected = event.target.closest('.preview-clear-selected-btn');
    if (clearSelected) {
      clearPreviewSelection();
      return;
    }

    const memberRow = event.target.closest('.member-row[data-phone]');
    if (memberRow && !event.target.closest('button, a, input, select, textarea, label')) {
      openMemberDetail(memberRow.dataset.phone || '');
      return;
    }

    const memberStatus = event.target.closest('.member-status-btn');
    if (memberStatus) {
      setStatus(memberStatus.dataset.phone || '', memberStatus.dataset.status || 'PENDING');
      return;
    }

    const memberDelete = event.target.closest('.member-delete-btn');
    if (memberDelete) {
      deleteMember(memberDelete.dataset.phone || '');
      return;
    }

    const pageButton = event.target.closest('.page-btn[data-page]');
    if (pageButton) {
      goPage(pageButton.dataset.page || '1');
    }
  });


  document.addEventListener('dblclick', (event) => {
    const nameOpen = event.target.closest('.member-name-open');
    if (!nameOpen) return;
    openMemberDetail(nameOpen.dataset.phone || '');
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const nameOpen = event.target.closest('.member-name-open');
    if (!nameOpen) return;
    event.preventDefault();
    openMemberDetail(nameOpen.dataset.phone || '');
  });

  document.addEventListener('change', (event) => {
    const previewCheckbox = event.target.closest('.preview-select-checkbox');
    if (previewCheckbox) {
      togglePreviewSelection(previewCheckbox.dataset.phone || '', previewCheckbox.checked);
      return;
    }


    const genderSelect = event.target.closest('.member-gender-select');
    if (genderSelect) {
      updateGender(genderSelect.dataset.phone || '', genderSelect.value);
      return;
    }

    const tierSelect = event.target.closest('.member-tier-select');
    if (tierSelect) {
      updateTier(tierSelect.dataset.phone || '', tierSelect.value);
    }
  });
}

function bindDomEvents() {
  $('token-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') doLogin();
  });
  bindShellEvents();
  bindDelegatedEvents();
}

async function tryAutoLogin() {
  const savedToken = sessionStorage.getItem('rsvp_admin_token');
  const expiresAt = parseInt(sessionStorage.getItem('rsvp_token_exp') || '0', 10);

  if (savedToken && Date.now() < expiresAt) {
    $('token-input').value = savedToken;
    await doLogin();
    return;
  }

  sessionStorage.removeItem('rsvp_admin_token');
  sessionStorage.removeItem('rsvp_token_exp');
}

function bootstrap() {
  bindDomEvents();
  tryAutoLogin();
}

bootstrap();
