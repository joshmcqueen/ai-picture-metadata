'use strict';
const $ = selector => document.querySelector(selector);
const token = $('meta[name="photo-token"]').content;
const photos = new Map();
const selected = new Set();
const pageSize = 25;
let page = 0;
let busy = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function error(message) { $('#errors').append(el('div', 'error-message', message)); }
async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers: {'X-Photo-Token': token, ...options.headers}});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || 'The local app could not process this request.');
  }
  return response.json();
}
function setBusy(value, message = '') {
  busy = value;
  $('#activity').textContent = message;
  $('#choose').disabled = value;
  $('#choose-folder').disabled = value;
  $('#samples').disabled = value;
  render();
}
function remember(photo) { photos.set(photo.id, photo); }
function photoPath(photo) { return photo?.relative_path || photo?.name || 'Photo'; }
function describe(report) {
  const app = (report.software || []).map(s => `${s.name || 'Editing app'} ${s.version || ''}`.trim()).join(', ');
  switch (report.status) {
    case 'edited': return `${app ? app + ' records' : 'The metadata records'} generative-AI editing. This does not mean the whole photograph was generated.`;
    case 'generated': return 'The metadata includes a declaration of AI generation. This describes the embedded record, not an independent assessment of the photograph.';
    case 'credentials': return 'An embedded provenance record is present, with no explicit generative-AI evidence identified. Credential presence alone does not establish AI use.';
    case 'error': return report.error;
    default: return 'No explicit AI indicators were found in the embedded metadata. This is not proof that AI was never used.';
  }
}
function canClean(photo) {
  return photo.editable && !photo.cleanup && photo.report.evidence?.length > 0;
}
function renderPhoto(photo) {
  const r = photo.cleanup?.report || photo.report;
  const card = el('article', 'photo-card'); card.dataset.id = photo.id;
  const top = el('div', 'photo-top');
  const checkbox = el('input', 'photo-select'); checkbox.type = 'checkbox';
  checkbox.setAttribute('aria-label', `Select ${photoPath(photo)}`);
  checkbox.checked = selected.has(photo.id);
  checkbox.disabled = busy || !canClean(photo);
  checkbox.addEventListener('change', () => { checkbox.checked ? selected.add(photo.id) : selected.delete(photo.id); render(); });
  top.append(checkbox);
  if (r.status !== 'error') {
    const image = el('img', 'thumbnail'); image.src = `/api/photos/${photo.id}/preview`;
    image.alt = `Preview of ${photo.name}`; image.loading = 'lazy'; image.decoding = 'async'; top.append(image);
  } else top.append(el('div', 'thumbnail'));
  const info = el('div', 'photo-info'); info.append(el('h3', 'photo-title', photo.name));
  if (photo.relative_path?.includes('/')) {
    info.append(el('p', 'photo-meta photo-path', photo.relative_path.slice(0, photo.relative_path.lastIndexOf('/'))));
  }
  info.append(el('p', 'photo-meta', r.width ? `${r.width.toLocaleString()} × ${r.height.toLocaleString()} · ${(r.size / 1e6).toFixed(2)} MB · ${r.color_profile || 'No embedded color profile'}` : 'Could not inspect this file'));
  info.append(el('span', `status-pill status-${r.status}`, r.label));
  info.append(el('p', 'photo-description', describe(r)));
  const actions = el('div', 'photo-actions');
  if (canClean(photo)) {
    const button = el('button', 'secondary compact', 'Clean original'); button.disabled = busy;
    button.addEventListener('click', () => cleanPhotos([photo.id])); actions.append(button);
  } else if (!photo.editable) {
    actions.append(el('p', 'photo-meta', 'Inspection only. Use the Mac picker to open the original for cleanup.'));
  }
  info.append(actions); top.append(info); card.append(top);
  if ((r.evidence || []).length) {
    const evidence = el('div', 'evidence'); evidence.append(el('h4', '', 'What the metadata says'));
    const list = el('ul');
    for (const item of r.evidence) {
      const li = el('li'); li.append(el('strong', '', `${item.source} · ${item.field}: `), document.createTextNode(String(item.value))); list.append(li);
    }
    evidence.append(list); card.append(evidence);
  }
  if (photo.cleanup) {
    const verified = el('div', 'verification');
    verified.append(el('p', '', '✓ ' + photo.cleanup.verification));
    verified.append(el('p', '', `${photo.cleanup.bytes_removed.toLocaleString()} bytes removed from the original.`));
    if (photo.cleanup.report.credential_present) verified.append(el('p', '', 'A credential without explicit AI evidence remains.'));
    card.append(verified);
  }
  if (r.status !== 'error') {
    const details = el('details');
    details.append(el('summary', '', 'View all metadata & credential details'));
    details.addEventListener('toggle', () => {
      if (details.open && !details.querySelector('pre')) {
        details.append(el('pre', '', JSON.stringify({signature_verification: r.signature_verification, original_sha256: photo.report.sha256, metadata: r.metadata, credentials: r.credentials, before_cleanup: photo.cleanup ? photo.report : undefined}, null, 2)));
      }
    });
    card.append(details);
  }
  return card;
}
function render() {
  const opened = new Set([...document.querySelectorAll('.photo-card details[open]')].map(d => d.closest('.photo-card').dataset.id));
  $('#results').hidden = photos.size === 0;
  $('#count').textContent = `(${photos.size})`;
  const counts = {ai: 0, none: 0, credentials: 0, error: 0, cleaned: 0};
  for (const photo of photos.values()) {
    const status = (photo.cleanup?.report || photo.report).status;
    if (['edited', 'generated'].includes(status)) counts.ai++;
    else counts[status]++;
    if (photo.cleanup) counts.cleaned++;
  }
  $('#summary').replaceChildren();
  for (const [count, label] of [[counts.ai, 'with AI evidence'], [counts.none, 'with no AI indicators found'], [counts.credentials, 'with other credentials'], [counts.error, counts.error === 1 ? 'inspection error' : 'inspection errors'], [counts.cleaned, 'cleaned in place']]) {
    if (count) $('#summary').append(el('span', '', `${count} ${label}`));
  }
  const pageCount = Math.max(1, Math.ceil(photos.size / pageSize));
  page = Math.min(page, pageCount - 1);
  const cards = [...photos.values()].slice(page * pageSize, (page + 1) * pageSize).map(renderPhoto);
  $('#photos').replaceChildren(...cards);
  for (const card of cards) if (opened.has(card.dataset.id)) card.querySelector('details')?.setAttribute('open', '');
  const eligible = [...photos.values()].filter(canClean);
  $('#select-all').disabled = busy || !eligible.length;
  $('#select-all').checked = !!eligible.length && eligible.every(p => selected.has(p.id));
  $('#select-all').indeterminate = selected.size > 0 && !$('#select-all').checked;
  $('#clean-selected').disabled = busy || !selected.size;
  $('#pages').hidden = pageCount === 1;
  $('#page-number').textContent = `Page ${page + 1} of ${pageCount}`;
  $('#previous-page').disabled = busy || page === 0;
  $('#next-page').disabled = busy || page === pageCount - 1;
}
function isJpeg(name) { return /\.jpe?g$/i.test(name) && !name.startsWith('._'); }
async function importFiles(scan) {
  if (busy) return;
  $('#errors').replaceChildren();
  setBusy(true, 'Looking for JPEGs…');
  let inspected = 0, skipped = 0, failed = 0;
  try {
    const found = await scan((path, e) => {
      failed++;
      error(`${path}: ${e.message || 'Could not read this item.'}`);
    });
    const files = found.filter(({file}) => isJpeg(file.name));
    skipped = found.length - files.length;
    for (const [index, {file, path}] of files.entries()) {
      $('#activity').textContent = `Inspecting ${index + 1} of ${files.length}: ${path}`;
      try {
        if (file.size > 64 * 1024 * 1024) throw new Error('Each JPEG must be 64 MiB or smaller.');
        const body = new FormData();
        body.append('photo', file);
        if (path !== file.name) body.append('relative_path', path);
        const photo = await api('/api/photos', {method: 'POST', body});
        remember(photo);
        if (photo.report.status === 'error') failed++;
        else inspected++;
      } catch (e) { failed++; error(`${path}: ${e.message}`); }
    }
  } catch (e) { failed++; error(e.message); }
  finally {
    const message = inspected || failed ? `${inspected} ${inspected === 1 ? 'JPEG' : 'JPEGs'} inspected.` : 'No JPEGs found.';
    const skippedMessage = skipped ? ` ${skipped} unsupported ${skipped === 1 ? 'file' : 'files'} skipped.` : '';
    setBusy(false, message + (failed ? ` ${failed} failed.` : '') + skippedMessage);
  }
}
async function readDropEntries(roots, looseFiles, onError) {
  async function visit(entry, parent) {
    const path = parent + entry.name;
    try {
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        looseFiles.push({file, path});
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        // A reader can return several batches for one directory.
        while (true) {
          const entries = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!entries.length) break;
          for (const child of entries) await visit(child, path + '/');
        }
      }
    } catch (e) { onError(path, e); }
  }
  for (const root of roots) await visit(root, '');
  return looseFiles;
}
async function cleanPhotos(ids) {
  if (busy) return;
  $('#errors').replaceChildren();
  setBusy(true, 'Checking image data and metadata before saving…');
  let count = 0;
  try {
    // Sequential requests keep memory bounded and show progress in large batches.
    for (const [index, id] of ids.entries()) {
      $('#activity').textContent = `Cleaning ${index + 1} of ${ids.length}: ${photoPath(photos.get(id))}`;
      const data = await api('/api/clean', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: [id]})});
      for (const result of data.results) {
        if (result.photo) { remember(result.photo); selected.delete(result.id); count++; }
        else if (result.status === 'error') error(`${photoPath(photos.get(result.id))}: ${result.error}`);
      }
    }
  } catch (e) { error(e.message); }
  finally { setBusy(false, `${count} ${count === 1 ? 'original' : 'originals'} cleaned in place.`); }
}
async function openOriginals(kind) {
  if (busy) return;
  $('#errors').replaceChildren();
  setBusy(true, kind ? 'Choose your originals in the Mac picker…' : 'Inspecting JPEGs in the configured folder…');
  try {
    const data = kind
      ? await api('/api/choose', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind})})
      : await api('/api/samples', {method: 'POST'});
    for (const photo of data.photos) {
      remember(photo);
      if (!canClean(photo)) selected.delete(photo.id);
    }
    data.errors.forEach(item => error(`${item.name}: ${item.error}`));
    setBusy(false, data.cancelled ? '' : data.photos.length || data.errors.length ? 'Inspection finished.' : 'No JPEGs found.');
  } catch (e) { error(e.message); setBusy(false); }
}
$('#choose').addEventListener('click', () => openOriginals('files'));
$('#choose-folder').addEventListener('click', () => openOriginals('folders'));
$('#samples').addEventListener('click', () => openOriginals());
$('#select-all').addEventListener('change', event => {
  selected.clear();
  if (event.target.checked) for (const p of photos.values()) if (canClean(p)) selected.add(p.id);
  render();
});
$('#clean-selected').addEventListener('click', () => cleanPhotos([...selected]));
for (const [selector, step] of [['#previous-page', -1], ['#next-page', 1]]) {
  $(selector).addEventListener('click', () => { page += step; render(); $('#results').scrollIntoView(); });
}
for (const eventName of ['dragover', 'drop']) document.addEventListener(eventName, event => event.preventDefault());
$('#dropzone').addEventListener('dragover', () => { if (!busy) $('#dropzone').classList.add('dragging'); });
$('#dropzone').addEventListener('dragleave', () => $('#dropzone').classList.remove('dragging'));
$('#dropzone').addEventListener('drop', event => {
  $('#dropzone').classList.remove('dragging');
  if (busy) return;
  const roots = [], files = [];
  for (const item of event.dataTransfer.items || []) {
    if (item.kind !== 'file') continue;
    const entry = item.webkitGetAsEntry?.();
    if (entry) roots.push(entry);
    else {
      const file = item.getAsFile();
      if (file) files.push({file, path: file.name});
    }
  }
  if (!roots.length && !files.length) {
    for (const file of event.dataTransfer.files) files.push({file, path: file.name});
  }
  importFiles(onError => readDropEntries(roots, files, onError));
});
api('/api/photos').then(data => { data.photos.forEach(remember); render(); }).catch(e => error(e.message));
