// ── Form ──
const smsOptInEl = document.getElementById('smsOptIn');
const submitBtn  = document.getElementById('submitBtn');
smsOptInEl.addEventListener('change', () => {
  submitBtn.disabled = !smsOptInEl.checked;
});

async function handleSubmit(e) {
  e.preventDefault();
  const phoneEl = document.getElementById('phoneInput');
  const firstNameEl = document.getElementById('firstNameInput');
  const lastNameEl  = document.getElementById('lastNameInput');
  const zipEl       = document.getElementById('zipInput');
  const label   = document.getElementById('submitLabel');
  const phone   = (phoneEl?.value || '').trim();
  const firstName = (firstNameEl?.value || '').trim();
  const lastName  = (lastNameEl?.value  || '').trim();
  const zipCode   = (zipEl?.value || '').trim();
  const name = [firstName, lastName].filter(Boolean).join(' ');
  if (!phone || !firstName || !lastName || !/^\d{5}$/.test(zipCode) || !smsOptInEl.checked) return;

  label.textContent = 'Adding you...';

  try {
    const res = await fetch('https://api.rsvpsociety.com/access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        phone,
        name,
        firstName,
        lastName,
        zipCode,
        source: 'web',
        smsOptIn: true
      })
    });
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch { data = { raw: text }; }

    if (!res.ok || data.ok !== true) {
      label.textContent = 'Something went wrong. Try again.';
      return;
    }

    phoneEl.disabled = true;
    firstNameEl.disabled = true;
    lastNameEl.disabled  = true;
    zipEl.disabled       = true;
    submitBtn.disabled = true;
    label.textContent = 'Locked in. Next room drops soon.';

  } catch (err) {
    label.textContent = 'Network error. Try again.';
  }
}

document.getElementById('accessForm').addEventListener('submit', handleSubmit);

// ── Smooth scroll ──
document.querySelectorAll('a[href^="#"]').forEach(a => {
  if (a.getAttribute('onclick')) return;
  a.addEventListener('click', e => {
    const target = document.querySelector(a.getAttribute('href'));
    if (target) { e.preventDefault(); target.scrollIntoView({ behavior: 'smooth' }); }
  });
});
