// drawer.js — contact profile drawer + pending banner
// Loaded as type="module" src — no inline scripts, CSP-safe

import { state } from './state.js';
import { setStatus, deleteMember } from './members.js';

// HTML escape — prevent XSS from member-supplied data injected into innerHTML
function esc(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── DOM refs (resolved after DOMContentLoaded since modules are deferred) ──
const overlay  = () => document.getElementById('drawer-overlay');
const drawer   = () => document.getElementById('contact-drawer');
const bodyEl   = () => document.getElementById('drawer-body');
const titleEl  = () => document.getElementById('drawer-name');

// ── Open drawer with member data ──────────────────────────────────────────
export function openDrawer(phone) {
  const member = state.memberList.find(
    m => (m.phone || '').replace(/\D/g, '') === phone.replace(/\D/g, '')
  );
  if (!member) return;

  const fullName = [member.name || '', member.lastName || '']
    .filter(Boolean).join(' ') || '—';

  const joinDate = member.createdAt
    ? new Date(member.createdAt).toLocaleDateString('en-US',
        { month: 'long', day: 'numeric', year: 'numeric' })
    : '—';

  const statusBadge =
    `<span class="status-badge ${member.status || 'PENDING'}">${member.status || 'PENDING'}</span>`;

  const tierLabel = {
    '0': 'No tier assigned',
    '1': 'Tier 1 — Inner Circle',
    '2': 'Tier 2 — Regular',
    '3': 'Tier 3 — General',
  };
  const tier = tierLabel[String(member.tier ?? member.tierOverride ?? '0')]
    || 'No tier assigned';

  const genderMap = { M: 'Male', F: 'Female', NB: 'Non-binary' };
  const gender = genderMap[member.gender] || member.gender || '—';

  const optIn = member.smsOptIn
    ? '<span style="color:var(--green)">● Opted in</span>'
    : '<span style="color:rgba(248,244,236,0.28)">○ No opt-in on record</span>';

  const isPending = (member.status || state.currentStatus) === 'PENDING';
  const actions = isPending
    ? `<button class="drawer-btn approve"  data-action="approve" data-phone="${esc(phone)}">Approve</button>
       <button class="drawer-btn deny"     data-action="deny"    data-phone="${esc(phone)}">Deny</button>`
    : `<button class="drawer-btn neutral"  data-action="pending" data-phone="${esc(phone)}">Move to Pending</button>`;

  const t = titleEl();
  if (t) t.textContent = fullName;

  const b = bodyEl();
  if (b) b.innerHTML = `
    <div class="drawer-section">
      <div class="drawer-section-label">Contact</div>
      <div class="drawer-field">
        <span class="drawer-field-label">Phone</span>
        <span class="drawer-field-value phone">${esc(phone)}</span>
      </div>
      <div class="drawer-field">
        <span class="drawer-field-label">Status</span>
        <span class="drawer-field-value">${statusBadge}</span>
      </div>
      <div class="drawer-field">
        <span class="drawer-field-label">SMS Opt-In</span>
        <span class="drawer-field-value" style="font-size:12px">${optIn}</span>
      </div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">Profile</div>
      <div class="drawer-field">
        <span class="drawer-field-label">Gender</span>
        <span class="drawer-field-value">${esc(gender)}</span>
      </div>
      <div class="drawer-field">
        <span class="drawer-field-label">Tier</span>
        <span class="drawer-field-value">${esc(tier)}</span>
      </div>
      <div class="drawer-field">
        <span class="drawer-field-label">Joined</span>
        <span class="drawer-field-value">${esc(joinDate)}</span>
      </div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">Actions</div>
      <div class="drawer-actions">${actions}</div>
      <div style="margin-top:10px">
        <button class="drawer-btn"
          style="border-color:rgba(192,71,74,0.2);color:rgba(192,71,74,0.55);width:100%"
          data-action="delete" data-phone="${esc(phone)}">Delete Member</button>
      </div>
    </div>`;

  overlay().classList.add('open');
  drawer().classList.add('open');
}

// ── Close drawer ──────────────────────────────────────────────────────────
export function closeDrawer() {
  overlay().classList.remove('open');
  drawer().classList.remove('open');
}

// ── Delegated handler for drawer action buttons (CSP-safe, no inline handlers) ──
async function _handleDrawerAction(action, phone) {
  if      (action === 'approve') { await setStatus(phone, 'APPROVED'); closeDrawer(); }
  else if (action === 'deny')    { await setStatus(phone, 'DENIED');   closeDrawer(); }
  else if (action === 'pending') { await setStatus(phone, 'PENDING');  closeDrawer(); }
  else if (action === 'delete')  { await deleteMember(phone);          closeDrawer(); }
}

// ── Wire up events after DOM is parsed (modules are deferred) ─────────────
document.addEventListener('DOMContentLoaded', () => {
  // Close on overlay click
  const ov = overlay();
  if (ov) ov.addEventListener('click', closeDrawer);

  // Close button
  const btn = document.getElementById('drawer-close-btn');
  if (btn) btn.addEventListener('click', closeDrawer);

  // Delegated click on member name → open drawer
  const container = document.getElementById('members-container');
  if (container) {
    container.addEventListener('click', (e) => {
      const nameEl = e.target.closest('.member-name');
      if (!nameEl) return;
      const row = nameEl.closest('tr');
      if (!row) return;
      const phoneTd = row.querySelector('.member-phone');
      if (!phoneTd) return;
      const phone = phoneTd.textContent.trim();
      if (phone && phone !== '—') openDrawer(phone);
    });
  }

  // Drawer action button delegation — CSP-safe, no inline onclick
  const drawerEl = drawer();
  if (drawerEl) {
    drawerEl.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      const phone  = btn.dataset.phone;
      if (action && phone) await _handleDrawerAction(action, phone);
    });
  }

  // Pending banner Review button — bind here, not inline (CSP: script-src 'self')
  const bannerBtn = document.getElementById('pending-banner-btn');
  if (bannerBtn) {
    bannerBtn.addEventListener('click', () => {
      const filterBtn = document.getElementById('filter-PENDING');
      if (filterBtn) filterBtn.click();
    });
  }

  // Pending banner — observe stat-pending for changes
  const statPending = document.getElementById('stat-pending');
  const banner      = document.getElementById('pending-banner');
  const bannerCount = document.getElementById('pending-banner-count');

  if (statPending && banner) {
    const update = () => {
      const count = parseInt(statPending.textContent, 10);
      if (count > 0) {
        banner.style.display = 'flex';
        if (bannerCount) bannerCount.textContent = count;
      } else {
        banner.style.display = 'none';
      }
    };
    new MutationObserver(update).observe(
      statPending,
      { childList: true, subtree: true, characterData: true }
    );
  }
});
