const API_DEFAULT = 'https://api.rsvpsociety.com';

const ROUTES = Object.freeze({
  ADMIN_MEMBERS: '/admin/members',
  ADMIN_MEMBER_STATUS: '/admin/members/status',
  ADMIN_MEMBER_GENDER: '/admin/members/gender',
  ADMIN_MEMBER_TIER: '/admin/members/tier',
  ADMIN_MEMBER_PROFILE: '/admin/members/profile',
  ADMIN_MEMBER_ATTENDANCE: '/admin/members/attendance',
  ADMIN_MEMBER_IMPORT: '/admin/members/import',
  ADMIN_MEMBER_CONFIRMED: '/admin/members/confirmed',
  ADMIN_MEMBER_HISTORY: '/admin/members/history',
  ADMIN_EVENT: '/admin/event',
  ADMIN_EVENTS: '/admin/events',
  ADMIN_EVENTS_ARCHIVE: '/admin/events/archive',
  ADMIN_EVENTS_FINALIZE: '/admin/events/finalize-attendance',
  ADMIN_EVENT_ANALYTICS: '/admin/event/analytics',
  ADMIN_EVENT_DRAFT_MESSAGE: '/admin/event/draft-message',
  ADMIN_INVITE_PREVIEW: '/admin/invite/preview',
  ADMIN_INVITE_SEND: '/admin/invite/send',
  ADMIN_INVITE_STATUS: '/admin/invite/status',
  ADMIN_INVITE_REMINDER: '/admin/invite/reminder',
});

const REMINDER_MODES = Object.freeze({
  MANUAL: 'manual',
  DAY_BEFORE: 'day_before',
  DAY_OF: 'day_of',
  BOTH: 'both',
});

const VIBE_TAGS = {
  swim:     ['suits + shots','poolside r&b','sunset + vibes','day party energy','cabanas + cocktails','towels + tequila'],
  day:      ['day drinks + r&b','patio + sunlight','brunchy vibes','outside early','grown day party'],
  rooftop:  ['rooftop + r&b','city views','cocktails + slow jams','night air vibes','late night rooftop'],
  elevated: ['special night','live moment','dress code matters','quiet luxury'],
  bowling:  ['lanes + drinks','bowling + r&b','link + bowl'],
  karaoke:  ['r&b karaoke','mic + r&b','late night karaoke','sing your heart out','shots + choruses','r&b classics','90s r&b night'],
  regular:  ['just vibes','keep it chill','no chaos'],
};

export { VIBE_TAGS, API_DEFAULT, ROUTES, REMINDER_MODES };
