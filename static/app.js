'use strict';
const $ = selector => document.querySelector(selector);
const token = $('meta[name="photo-token"]').content;
const photos = new Map();
const selected = new Set();
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
  $('#samples').disabled = value;
  $('#files').disabled = value;
  render();
}
function remember(photo) { photos.set(photo.id, photo); }
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
function link(label, href) { const a = el('a', 'text-button', label); a.href = href; return a; }
function renderPhoto(photo) {
  const r = photo.report;
  const card = el('article', 'photo-card'); card.dataset.id = photo.id;
  const top = el('div', 'photo-top');
  const checkbox = el('input', 'photo-select'); checkbox.type = 'checkbox';
  checkbox.setAttribute('aria-label', `Select ${photo.name}`);
  checkbox.checked = selected.has(photo.id);
  checkbox.disabled = busy || !(r.evidence || []).length;
  checkbox.addEventListener('change', () => { checkbox.checked ? selected.add(photo.id) : selected.delete(photo.id); render(); });
  top.append(checkbox);
  if (r.status !== 'error') {
    const image = el('img', 'thumbnail'); image.src = `/api/photos/${photo.id}/preview`;
    image.alt = `Preview of ${photo.name}`; image.loading = 'lazy'; top.append(image);
  } else top.append(el('div', 'thumbnail'));
  const info = el('div', 'photo-info'); info.append(el('h3', 'photo-title', photo.name));
  info.append(el('p', 'photo-meta', r.width ? `${r.width.toLocaleString()} × ${r.height.toLocaleString()} · ${(r.size / 1e6).toFixed(2)} MB · ${r.color_profile || 'No embedded color profile'}` : 'Could not inspect this file'));
  info.append(el('span', `status-pill status-${r.status}`, r.label));
  info.append(el('p', 'photo-description', describe(r)));
  const actions = el('div', 'photo-actions');
  if ((r.evidence || []).length && !photo.cleanup) {
    const button = el('button', 'secondary compact', 'Create cleaned copy'); button.disabled = busy;
    button.addEventListener('click', () => cleanPhotos([photo.id])); actions.append(button);
  }
  if (photo.cleanup) actions.append(link('Download cleaned JPEG ↓', `/api/photos/${photo.id}/download`));
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
    verified.append(el('p', '', `After cleanup: ${photo.cleanup.report.label}. ${photo.cleanup.bytes_removed.toLocaleString()} bytes removed.`));
    if (photo.cleanup.report.credential_present) verified.append(el('p', '', 'A credential without explicit AI evidence remains.'));
    card.append(verified);
  }
  if (r.status !== 'error') {
    const details = el('details');
    details.append(el('summary', '', 'View all metadata & credential details'));
    details.append(el('pre', '', JSON.stringify({signature_verification: r.signature_verification, original_sha256: r.sha256, metadata: r.metadata, credentials: r.credentials, after_cleanup: photo.cleanup?.report || undefined}, null, 2)));
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
    const status = photo.report.status;
    if (['edited', 'generated'].includes(status)) counts.ai++;
    else counts[status]++;
    if (photo.cleanup) counts.cleaned++;
  }
  $('#summary').replaceChildren();
  for (const [count, label] of [[counts.ai, 'with AI evidence'], [counts.none, 'with no AI indicators found'], [counts.credentials, 'with other credentials'], [counts.error, counts.error === 1 ? 'inspection error' : 'inspection errors'], [counts.cleaned, counts.cleaned === 1 ? 'verified cleaned copy' : 'verified cleaned copies']]) {
    if (count) $('#summary').append(el('span', '', `${count} ${label}`));
  }
  const cards = [...photos.values()].map(renderPhoto);
  $('#photos').replaceChildren(...cards);
  for (const card of cards) if (opened.has(card.dataset.id)) card.querySelector('details')?.setAttribute('open', '');
  const eligible = [...photos.values()].filter(p => p.report.evidence?.length);
  $('#select-all').disabled = busy || !eligible.length;
  $('#select-all').checked = !!eligible.length && eligible.every(p => selected.has(p.id));
  $('#select-all').indeterminate = selected.size > 0 && !$('#select-all').checked;
  $('#clean-selected').disabled = busy || ![...selected].some(id => !photos.get(id)?.cleanup);
  $('#zip').disabled = busy || !selected.size || ![...selected].every(id => photos.get(id)?.cleanup);
}
async function importFiles(files) {
  if (busy) return;
  $('#errors').replaceChildren();
  let completed = 0;
  setBusy(true, `Inspecting ${files.length} photo(s)…`);
  try {
    for (const file of files) {
      $('#activity').textContent = `Inspecting ${++completed} of ${files.length}: ${file.name}`;
      try {
        if (!/\.jpe?g$/i.test(file.name)) throw new Error('Only JPG and JPEG exports are supported.');
        if (file.size > 64 * 1024 * 1024) throw new Error('Each JPEG must be 64 MB or smaller.');
        const body = new FormData(); body.append('photo', file);
        remember(await api('/api/photos', {method: 'POST', body})); render();
      } catch (e) { error(`${file.name}: ${e.message}`); }
    }
  } finally { setBusy(false, 'Inspection complete. Your originals are unchanged.'); $('#files').value = ''; }
}
async function cleanPhotos(ids) {
  if (busy) return;
  $('#errors').replaceChildren();
  setBusy(true, 'Creating separate copies and checking image data and metadata…');
  let count = 0;
  try {
    // Sequential requests keep memory bounded and show progress in large batches.
    for (const id of ids) {
      const data = await api('/api/clean', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: [id]})});
      for (const result of data.results) {
        if (result.photo) { remember(result.photo); count++; }
        else if (result.status === 'error') error(`${photos.get(result.id)?.name || 'Photo'}: ${result.error}`);
      }
      render();
    }
  } catch (e) { error(e.message); }
  finally { setBusy(false, `${count} verified cleaned ${count === 1 ? 'copy' : 'copies'} ready to download. Originals unchanged.`); }
}
$('#choose').addEventListener('click', () => $('#files').click());
$('#files').addEventListener('change', event => importFiles([...event.target.files]));
$('#samples').addEventListener('click', async () => {
  $('#errors').replaceChildren(); setBusy(true, 'Inspecting JPEGs in the app folder…');
  try {
    const data = await api('/api/samples', {method: 'POST'});
    data.photos.forEach(remember); data.errors.forEach(item => error(`${item.name}: ${item.error}`));
    setBusy(false, data.photos.length ? 'Inspection complete. Your originals are unchanged.' : 'No JPEGs found in the configured input folder.');
  } catch (e) { error(e.message); setBusy(false); }
});
$('#select-all').addEventListener('change', event => {
  selected.clear();
  if (event.target.checked) for (const p of photos.values()) if (p.report.evidence?.length) selected.add(p.id);
  render();
});
$('#clean-selected').addEventListener('click', () => cleanPhotos([...selected].filter(id => !photos.get(id).cleanup)));
$('#zip').addEventListener('click', async () => {
  setBusy(true, 'Preparing cleaned photos for download…');
  try {
    const response = await fetch('/api/download.zip', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Photo-Token': token}, body: JSON.stringify({ids: [...selected]})});
    if (!response.ok) throw new Error((await response.json()).error);
    const url = URL.createObjectURL(await response.blob());
    const a = document.createElement('a'); a.href = url; a.download = 'cleaned-photos.zip'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    setBusy(false, 'ZIP download ready.');
  } catch (e) { error(e.message); setBusy(false); }
});
for (const eventName of ['dragover', 'drop']) document.addEventListener(eventName, event => event.preventDefault());
$('#dropzone').addEventListener('dragover', () => { if (!busy) $('#dropzone').classList.add('dragging'); });
$('#dropzone').addEventListener('dragleave', () => $('#dropzone').classList.remove('dragging'));
$('#dropzone').addEventListener('drop', event => { $('#dropzone').classList.remove('dragging'); importFiles([...event.dataTransfer.files]); });
api('/api/photos').then(data => { data.photos.forEach(remember); render(); }).catch(e => error(e.message));
