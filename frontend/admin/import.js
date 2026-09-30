import { apiJson } from './api.js';
import { ROUTES } from './constants.js';
import { state } from './state.js';
import { $, appendChildren, clearNode, createNode, setHidden, showToast, textStrong } from './ui.js';
import { loadMembers, loadStats } from './members.js';

function parseCsvText(text) {
  const rows = [];
  let row = [];
  let cell = '';
  let inQuotes = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];

    if (char === '"') {
      if (inQuotes && next === '"') {
        cell += '"';
        index += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }

    if (char === ',' && !inQuotes) {
      row.push(cell);
      cell = '';
      continue;
    }

    if ((char === '\n' || char === '\r') && !inQuotes) {
      if (char === '\r' && next === '\n') index += 1;
      row.push(cell);
      const normalizedRow = row.map((value) => value.trim());
      if (normalizedRow.some((value) => value !== '')) rows.push(normalizedRow);
      row = [];
      cell = '';
      continue;
    }

    cell += char;
  }

  if (cell.length || row.length) {
    row.push(cell);
    const normalizedRow = row.map((value) => value.trim());
    if (normalizedRow.some((value) => value !== '')) rows.push(normalizedRow);
  }

  return rows;
}

function getColumnIndex(headers, names) {
  const normalizedHeaders = headers.map((header) => String(header || '').toLowerCase().trim());
  const normalizedNames = names.map((name) => String(name || '').toLowerCase().trim());

  // Prefer an exact header before accepting broader vendor-specific labels such
  // as "Primary Phone Number". This prevents the generic "name" alias from
  // stealing a dedicated "Last Name" column.
  for (const name of normalizedNames) {
    const index = normalizedHeaders.findIndex((header) => header === name);
    if (index !== -1) return index;
  }

  for (const name of normalizedNames) {
    const index = normalizedHeaders.findIndex((header) => {
      if (!header.includes(name)) return false;
      if (name === 'name' && /(?:^|\s)(?:last\s*name|lastname|surname)(?:$|\s)/.test(header)) return false;
      return true;
    });
    if (index !== -1) return index;
  }
  return -1;
}

const CSV_IMPORT_SOURCE = 'csv';

function labelImportSource() {
  return 'CSV';
}

function effectiveImportSource() {
  return CSV_IMPORT_SOURCE;
}

function importConsentConfirmed() {
  return Boolean($('import-sms-consent')?.checked);
}

function setImportConfirmEnabled(isEnabled) {
  const button = $('import-confirm-btn');
  if (!button) return;
  button.disabled = !isEnabled;
}

function showImportPreview(builder) {
  const preview = $('import-preview');
  const previewText = clearNode('import-preview-text');
  if (!preview || !previewText) return;
  preview.classList.add('is-visible');
  if (typeof builder === 'function') builder(previewText);
}

function renderImportPreview() {
  const skipped = state.importSkipped || 0;
  const noLocation = state.importLocationSkipped || 0;
  if (!state.importRows.length) {
    showImportPreview((previewText) => {
      previewText.appendChild(createNode('span', {
        className: 'panel-summary-error',
        text: `No importable rows. ${skipped} skipped (missing phone); ${noLocation} excluded (missing or invalid ZIP/location).`,
      }));
    });
    return;
  }

  const source = labelImportSource();
  showImportPreview((previewText) => {
    appendChildren(
      previewText,
      textStrong(`${state.importRows.length} member${state.importRows.length !== 1 ? 's' : ''}`, 'text-strong'),
      document.createTextNode(' ready to import as '),
      textStrong('Approved', 'text-strong'),
      createNode('br'),
      document.createTextNode('Import source: '),
      textStrong(source, 'text-gold-strong'),
      createNode('br'),
      document.createTextNode('SMS consent: '),
      textStrong(importConsentConfirmed() ? 'Confirmed for this file' : 'Not confirmed — texts disabled', 'text-gold-strong'),
    );

    if (skipped) {
      appendChildren(
        previewText,
        createNode('br'),
        createNode('span', {
          className: 'panel-summary-skipped',
          text: `${skipped} row${skipped !== 1 ? 's' : ''} skipped (no phone)`,
        }),
      );
    }
    if (noLocation) {
      appendChildren(
        previewText,
        createNode('br'),
        createNode('span', {
          className: 'panel-summary-skipped',
          text: `${noLocation} excluded: no location (a valid 5-digit ZIP is required)`,
        }),
      );
    }

  });
}

export function openImport() {
  if (state.importInProgress) return;
  setHidden('import-modal', false);
  state.importRows = [];
  state.importSkipped = 0;
  state.importLocationSkipped = 0;
  state.importUniqueZipCount = 0;
  $('import-preview')?.classList.remove('is-visible');
  if ($('import-sms-consent')) $('import-sms-consent').checked = false;
  setImportConfirmEnabled(false);
  $('csv-file-input').value = '';
}

export function closeImport() {
  setHidden('import-modal', true);
}

export function refreshImportPreview() {
  if (state.importRows.length) renderImportPreview();
}

export function handleDrop(event) {
  event.preventDefault();
  $('import-drop').classList.remove('is-drag-active');
  const file = event.dataTransfer.files[0];
  if (file && file.name.toLowerCase().endsWith('.csv')) parseCSV(file);
}

export function handleFileSelect(event) {
  const file = event.target.files[0];
  if (file) parseCSV(file);
}

export function parseCSV(file) {
  if (state.importInProgress) return;
  const reader = new FileReader();

  reader.onload = (loadEvent) => {
    // A rejected replacement file must never leave the previous file sendable.
    state.importRows = [];
    state.importSkipped = 0;
    state.importLocationSkipped = 0;
    state.importUniqueZipCount = 0;
    setImportConfirmEnabled(false);
    const text = String(loadEvent.target.result || '');
    const rows = parseCsvText(text);
    if (!rows.length) {
      renderImportPreview();
      return;
    }

    const headers = (rows[0] || []).map((header) => header.replace(/["']/g, '').toLowerCase().trim());

    const phoneIndex = getColumnIndex(headers, ['phone', 'mobile', 'cell', 'number', 'tel']);
    const nameIndex = getColumnIndex(headers, ['first name', 'name', 'full name', 'attendee']);
    const lastNameIndex = getColumnIndex(headers, ['last name', 'lastname', 'surname']);
    const emailIndex = getColumnIndex(headers, ['email', 'e-mail', 'mail']);
    const instagramIndex = getColumnIndex(headers, ['instagram', 'ig', 'insta', '@']);
    const tagsIndex = getColumnIndex(headers, ['tags', 'tag', 'list']);
    const zipIndex = getColumnIndex(headers, ['zip code', 'zip', 'postal code', 'postal']);

    if (phoneIndex === -1) {
      renderImportPreview();
      showToast('No phone column found in CSV', 'error');
      return;
    }

    let skipped = 0;
    let noLocation = 0;

    rows.slice(1).forEach((cells) => {
      const phone = (cells[phoneIndex] || '').trim();
      if (!phone || phone.length < 7) {
        skipped += 1;
        return;
      }

      const rawZip = String(cells[zipIndex] || '').trim();
      const zipMatch = rawZip.match(/^(\d{5})(?:-?\d{4})?$/);
      if (!zipMatch) {
        noLocation += 1;
        return;
      }

      const row = { phone, zipCode: zipMatch[1] };
      if (nameIndex !== -1 && cells[nameIndex]) row.name = cells[nameIndex].trim();
      if (lastNameIndex !== -1 && cells[lastNameIndex]) row.lastName = cells[lastNameIndex].trim();
      if (emailIndex !== -1 && cells[emailIndex]) row.email = cells[emailIndex].trim();
      if (instagramIndex !== -1 && cells[instagramIndex]) row.instagram = cells[instagramIndex].trim().replace(/^@/, '');
      if (tagsIndex !== -1 && cells[tagsIndex]) row.tags = cells[tagsIndex].trim();

      state.importRows.push(row);
    });

    if (!state.importRows.length) {
      state.importSkipped = skipped;
      state.importLocationSkipped = noLocation;
      state.importUniqueZipCount = 0;
      renderImportPreview();
      setImportConfirmEnabled(false);
      return;
    }

    state.importSkipped = skipped;
    state.importLocationSkipped = noLocation;
    state.importUniqueZipCount = new Set(state.importRows.map((row) => row.zipCode)).size;
    renderImportPreview();
    setImportConfirmEnabled(
      state.importRows.length > 0,
    );
  };

  reader.readAsText(file);
}


export async function confirmImport() {
  if (!state.importRows.length || state.importInProgress) return;
  state.importInProgress = true;
  const button = $('import-confirm-btn');
  button.disabled = true;
  const source = effectiveImportSource();
  const consentConfirmed = importConsentConfirmed();
  const seen = new Set();
  let skipped = 0;
  const rows = state.importRows.filter((row) => {
    const digits = String(row.phone || '').replace(/\D/g, '');
    const phone = digits.length === 10 ? `1${digits}` : digits;
    if (seen.has(phone)) { skipped += 1; return false; }
    seen.add(phone);
    return true;
  });
  let imported = 0;
  let excluded = 0;
  let processed = 0;
  try {
    for (let offset = 0; offset < rows.length; offset += 25) {
      button.textContent = `Importing ${processed} of ${rows.length}...`;
      const batch = rows.slice(offset, offset + 25);
      const data = await apiJson(ROUTES.ADMIN_MEMBER_IMPORT, {
        method: 'POST', body: { members: batch, source, consentConfirmed },
      });
      imported += Number(data.imported || 0);
      skipped += Number(data.skipped || 0);
      excluded += Number(data.excludedNoLocation || 0);
      processed += batch.length;
    }
    state.importRows = [];
    showToast(`Imported ${imported} members${excluded ? `, ${excluded} excluded: no location` : ''}${skipped > excluded ? `, ${skipped - excluded} other rows skipped` : ''}`, skipped ? 'warning' : 'success');
    closeImport();
  } catch (error) {
    // A timeout can occur after writes. Do not automatically replay that batch.
    state.importRows = [];
    showToast(`Import stopped after ${processed} processed rows (${imported} imported). The last batch may have saved. Check members before uploading remaining rows. ${error.message}`, 'error');
  } finally {
    state.importInProgress = false;
    button.textContent = 'Import';
    button.disabled = true;
  }
  try {
    await loadMembers('APPROVED');
    await loadStats();
  } catch (error) {
    showToast(`Refresh members to see the import results: ${error.message}`, 'warning');
  }
}
