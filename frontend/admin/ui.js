export function $(id) {
  return document.getElementById(id);
}

export function setHidden(target, hidden = true) {
  const element = typeof target === 'string' ? $(target) : target;
  if (!element) return;
  element.hidden = Boolean(hidden);
}

export function clearNode(target) {
  const element = typeof target === 'string' ? $(target) : target;
  if (!element) return null;
  element.replaceChildren();
  return element;
}

export function showToast(message, type = '') {
  const toast = $('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast show ${type}`.trim();
  window.setTimeout(() => {
    toast.className = 'toast';
  }, 3000);
}

export function createNode(tag, options = {}) {
  const element = document.createElement(tag);
  const {
    className = '',
    text = null,
    attrs = {},
    dataset = {},
  } = options;

  if (className) element.className = className;
  if (text !== null && text !== undefined) element.textContent = String(text);

  Object.entries(attrs).forEach(([key, value]) => {
    if (value === false || value === null || value === undefined) return;
    if (value === true) {
      element.setAttribute(key, '');
      return;
    }
    element.setAttribute(key, String(value));
  });

  Object.entries(dataset).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    element.dataset[key] = String(value);
  });

  return element;
}

export function appendChildren(parent, ...children) {
  children.flat().filter(Boolean).forEach((child) => parent.appendChild(child));
  return parent;
}

export function emptyState(message, className = 'empty-state') {
  return createNode('div', { className, text: message });
}

export function loadingState(message = 'Loading...') {
  return createNode('div', { className: 'loading-state', text: message });
}

export function textStrong(text, className = '') {
  return createNode('strong', { className, text });
}


export function formatErrorMessage(error, fallback = 'Unexpected error') {
  if (error?.message) return String(error.message);
  if (typeof error === 'string' && error.trim()) return error.trim();
  return fallback;
}

export function reportError(context, error, options = {}) {
  const { toast = true, fallback = 'Unexpected error', type = 'error' } = options;
  const message = formatErrorMessage(error, fallback);
  console.error(`${context}:`, error);
  if (toast) showToast(`${context}: ${message}`, type);
  return message;
}

export function escHtml(value = '') {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function sanitizePhoneId(phone = '') {
  const normalized = String(phone || '').trim();
  const digits = normalized.replace(/\D/g, '');
  return digits || normalized.replace(/[^a-zA-Z0-9_-]/g, '_');
}

export function setActiveButton(selector, activeId) {
  document.querySelectorAll(selector).forEach((element) => element.classList.remove('active'));
  const active = document.getElementById(activeId);
  if (active) active.classList.add('active');
}


export function openModal({ title = '', kicker = '', body = null, actions = [] } = {}) {
  closeModal();
  const overlay = createNode('div', { className: 'modal-overlay premium-modal-overlay', attrs: { id: 'runtime-modal' } });
  const panel = createNode('div', { className: 'modal-panel premium-modal-panel' });
  const header = createNode('div', { className: 'modal-header' });
  const headingWrap = createNode('div');
  if (kicker) headingWrap.appendChild(createNode('p', { className: 'modal-kicker', text: kicker }));
  if (title) headingWrap.appendChild(createNode('h3', { className: 'modal-title', text: title }));
  const close = createNode('button', { className: 'modal-close-btn', text: '×', attrs: { type: 'button', 'aria-label': 'Close modal' } });
  close.addEventListener('click', closeModal);
  appendChildren(header, headingWrap, close);
  panel.appendChild(header);
  if (body) panel.appendChild(body);
  if (actions.length) {
    const row = createNode('div', { className: 'modal-actions' });
    actions.forEach((action) => row.appendChild(action));
    panel.appendChild(row);
  }
  overlay.appendChild(panel);
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) closeModal();
  });
  const onKey = (event) => { if (event.key === 'Escape') closeModal(); };
  document.addEventListener('keydown', onKey, { once: true });
  overlay._rsvpModalKeyHandler = onKey;
  document.body.appendChild(overlay);
  return overlay;
}

export function closeModal() {
  const existing = document.getElementById('runtime-modal');
  if (existing) {
    if (existing._rsvpModalKeyHandler) document.removeEventListener('keydown', existing._rsvpModalKeyHandler);
    existing.remove();
  }
}
