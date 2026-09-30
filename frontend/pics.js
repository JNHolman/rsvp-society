// ── CONFIG ──
// Update CLOUDFRONT_BASE after terraform apply creates the distribution.
// Run: terraform output pics_cloudfront_url
// Then replace the placeholder below.
const CLOUDFRONT_BASE = 'https://d31o74npegx00h.cloudfront.net';

// Event folders — add a new entry here each month after uploading photos.
// Format: { id: 'folder-name-in-s3', label: 'Display Name' }
const EVENT_FOLDERS = [
  // { id: '2026-03-march', label: 'March 2026' },
  // { id: '2026-04-april', label: 'April 2026' },
];

// Photo filenames per folder — update after uploading to S3.
// Format: { folder: 'folder-id', files: ['photo1.jpg', 'photo2.jpg', ...] }
const PHOTO_MANIFEST = [
  // { folder: '2026-03-march', files: ['001.jpg', '002.jpg', '003.jpg'] },
];

// ── State ──
let currentFolder = null;
let lightboxPhotos = [];
let lightboxIndex = 0;

// ── Init ──
function init() {
  const nav = document.getElementById('folder-nav');

  if (!EVENT_FOLDERS.length) return; // No folders yet — empty state stays

  EVENT_FOLDERS.forEach((folder, i) => {
    const btn = document.createElement('button');
    btn.className = 'folder-btn' + (i === 0 ? ' active' : '');
    btn.textContent = folder.label;
    btn.onclick = () => loadFolder(folder.id, btn);
    nav.appendChild(btn);
  });

  // Load first folder
  loadFolder(EVENT_FOLDERS[0].id, nav.querySelector('.folder-btn'));
}

function loadFolder(folderId, btn) {
  currentFolder = folderId;

  document.querySelectorAll('.folder-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');

  const manifest = PHOTO_MANIFEST.find(m => m.folder === folderId);
  const files = manifest ? manifest.files : [];
  const container = document.getElementById('gallery-container');
  container.textContent = '';

  if (!files.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-gallery';
    empty.textContent = 'No photos yet.';
    const sub = document.createElement('div');
    sub.className = 'empty-sub';
    sub.textContent = 'Photos from this event coming soon.';
    empty.appendChild(sub);
    container.appendChild(empty);
    return;
  }

  lightboxPhotos = files.map(f => `${CLOUDFRONT_BASE}/${folderId}/${f}`);

  const grid = document.createElement('div');
  grid.className = 'gallery-grid';

  files.forEach((f, i) => {
    const item = document.createElement('div');
    item.className = 'photo-item';
    item.addEventListener('click', () => openLightbox(i));

    const img = document.createElement('img');
    img.src = `${CLOUDFRONT_BASE}/${folderId}/${f}`;
    img.alt = 'RSVP Society event photo';
    img.loading = 'lazy';
    img.addEventListener('load', () => img.classList.add('loaded'));

    item.appendChild(img);
    grid.appendChild(item);
  });

  container.appendChild(grid);
}

// ── Lightbox ──
function openLightbox(index) {
  lightboxIndex = index;
  document.getElementById('lightbox-img').src = lightboxPhotos[index];
  document.getElementById('lightbox').classList.add('open');
}

function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
  document.getElementById('lightbox-img').src = '';
}

function lightboxNav(dir) {
  lightboxIndex = (lightboxIndex + dir + lightboxPhotos.length) % lightboxPhotos.length;
  document.getElementById('lightbox-img').src = lightboxPhotos[lightboxIndex];
}

document.getElementById('lightbox').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeLightbox();
});
document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
document.getElementById('lightbox-prev').addEventListener('click', () => lightboxNav(-1));
document.getElementById('lightbox-next').addEventListener('click', () => lightboxNav(1));

document.addEventListener('keydown', e => {
  if (!document.getElementById('lightbox').classList.contains('open')) return;
  if (e.key === 'Escape') closeLightbox();
  if (e.key === 'ArrowRight') lightboxNav(1);
  if (e.key === 'ArrowLeft') lightboxNav(-1);
});

init();
