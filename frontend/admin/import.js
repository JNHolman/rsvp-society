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
  for (const name of names) {
    const index = headers.findIndex((header) => header.includes(name));
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
  if (!state.importRows.length) {
    showImportPreview((previewText) => {
      previewText.appendChild(createNode('span', {
        className: 'panel-summary-error',
        text: `No valid rows found. ${skipped} row${skipped !== 1 ? 's' : ''} skipped (missing phone).`,
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
  });
}

export function openImport() {
  setHidden('import-modal', false);
  state.importRows = [];
  state.importSkipped = 0;
  $('import-preview')?.classList.remove('is-visible');
  setImportConfirmEnabled(false);
  $('csv-file-input').value = '';
}

export function closeImport() {
  setHidden('import-modal', true);
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
  const reader = new FileReader();

  reader.onload = (loadEvent) => {
    const text = String(loadEvent.target.result || '');
    const rows = parseCsvText(text);
    if (!rows.length) return;

    const headers = (rows[0] || []).map((header) => header.replace(/["']/g, '').toLowerCase().trim());

    const phoneIndex = getColumnIndex(headers, ['phone', 'mobile', 'cell', 'number', 'tel']);
    const nameIndex = getColumnIndex(headers, ['first name', 'name', 'full name', 'attendee']);
    const lastNameIndex = getColumnIndex(headers, ['last name', 'lastname', 'surname']);
    const emailIndex = getColumnIndex(headers, ['email', 'e-mail', 'mail']);
    const instagramIndex = getColumnIndex(headers, ['instagram', 'ig', 'insta', '@']);
    const tagsIndex = getColumnIndex(headers, ['tags', 'tag', 'list']);

    if (phoneIndex === -1) {
      showToast('No phone column found in CSV', 'error');
      return;
    }

    state.importRows = [];
    state.importSkipped = 0;
    let skipped = 0;

    rows.slice(1).forEach((cells) => {
      const phone = (cells[phoneIndex] || '').trim();
      if (!phone || phone.length < 7) {
        skipped += 1;
        return;
      }

      const row = { phone, smsOptIn: true };
      if (nameIndex !== -1 && cells[nameIndex]) row.name = cells[nameIndex].trim();
      if (lastNameIndex !== -1 && cells[lastNameIndex]) row.lastName = cells[lastNameIndex].trim();
      if (emailIndex !== -1 && cells[emailIndex]) row.email = cells[emailIndex].trim();
      if (instagramIndex !== -1 && cells[instagramIndex]) row.instagram = cells[instagramIndex].trim().replace(/^@/, '');
      if (tagsIndex !== -1 && cells[tagsIndex]) row.tags = cells[tagsIndex].trim();

      state.importRows.push(row);
    });

    if (!state.importRows.length) {
      state.importSkipped = skipped;
      renderImportPreview();
      setImportConfirmEnabled(false);
      return;
    }

    state.importSkipped = skipped;
    renderImportPreview();
    setImportConfirmEnabled(true);
  };

  reader.readAsText(file);
}


export async function confirmImport() {
  if (!state.importRows.length) return;

  const button = $('import-confirm-btn');
  button.textContent = 'Importing...';
  button.disabled = true;

  try {
    const data = await apiJson(ROUTES.ADMIN_MEMBER_IMPORT, {
      method: 'POST',
      body: { members: state.importRows, source: effectiveImportSource() },
    });

    const message = `Imported ${data.imported} member${data.imported !== 1 ? 's' : ''}${data.skipped ? `, ${data.skipped} skipped` : ''}`;
    showToast(message, data.skipped ? 'warning' : 'success');
    closeImport();
    await loadMembers('APPROVED');
    await loadStats();
  } catch (error) {
    showToast(`Import failed: ${error.message}`, 'error');
    button.textContent = 'Import';
    button.disabled = false;
  }
}
