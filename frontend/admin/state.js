import { API_DEFAULT } from './constants.js';
const KNOWN_EVENTS_KEY = 'rsvp_known_events';
const MAX_KNOWN_EVENTS = 50;

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

export const state = {
  apiBase: (localStorage.getItem('rsvp_api_base') || API_DEFAULT).replace(/\/+$/, ''),
  adminToken: '',
  activeTab: 'members',
  currentStatus: 'PENDING',
  currentPage: 1,
  pageSize: 50,
  currentEvent: null,
  memberList: [],
  filteredMembers: [],
  memberTotals: {
    PENDING: 0,
    APPROVED: 0,
    DENIED: 0,
  },
  importRows: [],
  importDetectedSource: 'csv',
  importSkipped: 0,
  preview: {
    lastPreview: null,
    members: [],
    removedPhones: new Set(),
    activeMarketFilter: 'All',
  },
};

export function resetPreviewState() {
  state.preview.lastPreview = null;
  state.preview.members = [];
  state.preview.removedPhones = new Set();
  state.preview.activeMarketFilter = 'All';
}

export function resetSessionState() {
  state.adminToken = '';
  state.currentEvent = null;
  state.memberList = [];
  state.filteredMembers = [];
  state.memberTotals = {
    PENDING: 0,
    APPROVED: 0,
    DENIED: 0,
  };
  state.importRows = [];
  state.importDetectedSource = 'csv';
  state.importSkipped = 0;
  state.currentStatus = 'PENDING';
  state.currentPage = 1;
  state.activeTab = 'members';
  resetPreviewState();
}

export function setMembers(members = []) {
  state.memberList = members;
  state.filteredMembers = members;
}

export function normalizeEventRecord(event = {}) {
  const slug = event.slug || event.eventSlug || event.eventId || '';
  const date = event.date || '';
  const label = event.label || (slug
    ? `${slug}${date ? ` — ${date}` : ''}`
    : (date ? `Current Event — ${date}` : 'Current Event'));

  return {
    slug,
    label,
    date,
    venue: event.venue || '',
    city: event.city || '',
    capacity: event.capacity || '',
    eventId: event.eventId || slug,
    eventSlug: event.eventSlug || slug,
    revealVenue: parseBool(event.revealVenue),
    updatedAt: event.updatedAt || '',
  };
}

export function getKnownEvents() {
  try {
    const parsed = JSON.parse(localStorage.getItem(KNOWN_EVENTS_KEY) || '[]');
    return Array.isArray(parsed)
      ? parsed
          .map((entry) => normalizeEventRecord(entry))
          .filter((entry) => entry.slug)
      : [];
  } catch {
    return [];
  }
}

export function rememberEvent(event = {}) {
  const normalized = normalizeEventRecord(event);
  if (!normalized.slug) return null;

  const existing = getKnownEvents();
  const prior = existing.find((entry) => entry.slug === normalized.slug) || {};
  const merged = normalizeEventRecord({
    ...prior,
    ...normalized,
    label: normalized.label || prior.label,
  });

  const next = [merged, ...existing.filter((entry) => entry.slug !== merged.slug)].slice(0, MAX_KNOWN_EVENTS);
  try {
    localStorage.setItem(KNOWN_EVENTS_KEY, JSON.stringify(next));
  } catch {
    // ignore storage issues
  }

  return merged;
}

export function getKnownEvent(eventId = '') {
  const lookup = String(eventId || '').trim();
  if (!lookup) return null;

  if (state.currentEvent) {
    const current = normalizeEventRecord(state.currentEvent);
    if (lookup === 'current' || current.slug === lookup || current.eventId === lookup) {
      return current;
    }
  }

  return getKnownEvents().find((entry) => (
    entry.slug === lookup || entry.eventId === lookup || entry.eventSlug === lookup
  )) || null;
}
