export function closeDrawer() {
  const existing = document.getElementById('runtime-drawer');
  if (!existing) return;
  if (existing._rsvpDrawerKeyHandler) {
    document.removeEventListener('keydown', existing._rsvpDrawerKeyHandler);
  }
  existing.remove();
}
