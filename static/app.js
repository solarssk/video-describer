'use strict';

let eventSource = null;
let totalFiles = 0;
let reconnectTimer = null;
let activelyProcessing = false;

// ── Single-tab guard ──────────────────────────────────────
const TAB_ID = Math.random().toString(36).slice(2);
const TAB_KEY = 'vd_active_tab';

function claimTab() {
  localStorage.setItem(TAB_KEY, TAB_ID);
}
function releaseTab() {
  if (localStorage.getItem(TAB_KEY) === TAB_ID) localStorage.removeItem(TAB_KEY);
}
function checkSingleTab() {
  const active = localStorage.getItem(TAB_KEY);
  if (active && active !== TAB_ID) {
    const banner = document.getElementById('multi-tab-banner');
    if (banner) banner.style.display = 'flex';
  }
}
window.addEventListener('beforeunload', releaseTab);

// ── i18n ──────────────────────────────────────────────────
let I18N = {};                  // current dictionary
let CURRENT_LANG = 'en';
const SUPPORTED_LANGS = ['en', 'pl'];

function detectInitialLang() {
  const saved = localStorage.getItem('ui_language');
  if (saved && SUPPORTED_LANGS.includes(saved)) return saved;
  const nav = (navigator.language || 'en').toLowerCase().slice(0, 2);
  return SUPPORTED_LANGS.includes(nav) ? nav : 'en';
}

function t(key, vars) {
  // dot-notation lookup with optional {{var}} interpolation
  const parts = key.split('.');
  let val = I18N;
  for (const p of parts) {
    if (val && typeof val === 'object' && p in val) val = val[p];
    else return key;  // missing key — return the key so it's noticeable
  }
  if (typeof val !== 'string') return key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      val = val.replace(new RegExp(`{{${k}}}`, 'g'), String(v));
    }
  }
  return val;
}

async function loadI18n(lang) {
  const res = await fetch(`/static/i18n/${lang}.json`);
  if (!res.ok) throw new Error(`Failed to load i18n for ${lang}`);
  return res.json();
}

function applyTranslations() {
  // textContent
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  // innerHTML (for strings that contain markup like <b>, <code>)
  document.querySelectorAll('[data-i18n-html]').forEach(el => {
    const vars = el.dataset.i18nVars ? JSON.parse(el.dataset.i18nVars) : undefined;
    el.innerHTML = t(el.dataset.i18nHtml, vars);
  });
  // attributes: data-i18n-attr-<name>="key" → sets that attribute
  document.querySelectorAll('*').forEach(el => {
    for (const attr of el.attributes) {
      if (attr.name.startsWith('data-i18n-attr-')) {
        const targetAttr = attr.name.slice('data-i18n-attr-'.length);
        el.setAttribute(targetAttr, t(attr.value));
      }
    }
  });
}

async function setLang(lang) {
  if (!SUPPORTED_LANGS.includes(lang)) lang = 'en';
  try {
    I18N = await loadI18n(lang);
    CURRENT_LANG = lang;
    localStorage.setItem('ui_language', lang);
    document.documentElement.lang = lang;
    document.querySelectorAll('.lang-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.lang === lang);
    });
    applyTranslations();
    // Re-render dynamic content that wasn't done via data-i18n
    rerenderDynamicLabels();
  } catch (e) {
    console.error('Failed to set language:', e);
  }
}

function rerenderDynamicLabels() {
  // Update status text if currently in a known state
  const statusText = $('status-text');
  if (statusText && statusText.dataset.stateKey) {
    statusText.textContent = t(statusText.dataset.stateKey);
  }
  // Refresh Start button tooltip (translated)
  updateStartEnabled();
  // Update person row placeholders (baked in at creation time, need manual refresh)
  document.querySelectorAll('.person-name').forEach(el => {
    el.placeholder = t('form.people.name_placeholder');
  });
  document.querySelectorAll('.person-desc').forEach(el => {
    el.placeholder = t('form.people.desc_placeholder');
  });
  renderSyscheck(lastSysinfo);
}

// ── Helpers ───────────────────────────────────────────────
function $(id) { return document.getElementById(id); }
function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderSyscheck(s) {
  const syscheck = $('syscheck-rows');
  if (!syscheck || !s) return;

  const row = (label, value, ok) => {
    const icon = ok === true ? '✓' : ok === false ? '✗' : '—';
    const cls  = ok === true ? 'ok' : ok === false ? 'err' : 'na';
    return `<div class="syscheck-row">
      <span class="syscheck-label">${escHtml(label)}</span>
      <span class="syscheck-value">${escHtml(value)}</span>
      <span class="syscheck-icon ${cls}">${icon}</span>
    </div>`;
  };

  const chip = s.apple_silicon
    ? t('settings.syscheck.apple_silicon', { ram: s.ram_gb })
    : s.platform === 'Darwin'
      ? t('settings.syscheck.ram_only', { ram: s.ram_gb })
      : t('settings.syscheck.unsupported_platform', { platform: s.platform, ram: s.ram_gb });
  const isMacos = s.platform === 'Darwin';
  const whisperVal = s.whisper_backend === 'mlx'
    ? t('settings.syscheck.whisper_mlx')
    : s.whisper_backend === 'faster-whisper'
      ? t('settings.syscheck.whisper_cpu')
      : t('settings.syscheck.whisper_missing');

  syscheck.innerHTML = [
    row(t('settings.syscheck.macos'), chip, isMacos),
    row(
      t('settings.syscheck.ffmpeg'),
      s.ffmpeg ? t('settings.syscheck.installed') : t('settings.syscheck.ffmpeg_missing'),
      s.ffmpeg,
    ),
    row(t('settings.syscheck.whisper'), whisperVal, s.whisper_backend ? true : null),
    row(
      t('settings.syscheck.claude'),
      s.anthropic_connected ? t('settings.syscheck.api_key_configured') : t('settings.syscheck.api_key_missing'),
      s.anthropic_connected,
    ),
    row(
      t('settings.syscheck.openai'),
      s.openai_connected ? t('settings.syscheck.api_key_configured') : t('settings.syscheck.optional_not_configured'),
      s.openai_connected ? true : null,
    ),
  ].join('');
}

// ── Connector state ───────────────────────────────────────
let anthropicConnected = false;   // updated by fetchSysinfo() and loadConnectors()
let lastSysinfo = null;

function _setAnthropicConnected(connected) {
  anthropicConnected = connected;
  updateStartEnabled();
}

// ── Connectors tab ────────────────────────────────────────
const supportsTextSecurity = CSS.supports && (
  CSS.supports('-webkit-text-security', 'disc') || CSS.supports('text-security', 'disc')
);

function _applyMasking(input) {
  if (!supportsTextSecurity) {
    input.type = 'password';
    input.classList.remove('masked');
  }
}

async function loadConnectors() {
  // Apply masking to connector key inputs
  ['anthropic', 'openai'].forEach(p => {
    const el = $(`conn-key-${p}`);
    if (el) _applyMasking(el);
  });

  try {
    const res = await fetch('/connectors');
    const data = await res.json();
    _renderConnectorBadge('anthropic', data.anthropic);
    _renderConnectorBadge('openai', data.openai);
    _setAnthropicConnected(data.anthropic?.connected || false);
  } catch (e) {
    console.warn('Failed to load connectors:', e);
  }
}

function _renderConnectorBadge(provider, info) {
  if (!info) return;
  const badge = $(`conn-badge-${provider}`);
  if (!badge) return;
  const input = $(`conn-key-${provider}`);

  if (info.connected) {
    badge.innerHTML = `<span>${t('connectors.connected')}</span>`;
    badge.className = 'conn-badge ok';

    // Show masked key in input so user sees "a key is stored"
    if (input && info.masked && !input.dataset.userEditing) {
      input.value = info.masked;
      input.dataset.storedMask = 'true';  // sentinel — not a real key

      // Attach focus listener once: clear field so user can paste a new key
      if (!input._maskFocusAdded) {
        input._maskFocusAdded = true;
        input.addEventListener('focus', () => {
          if (input.dataset.storedMask === 'true') {
            input.value = '';
            delete input.dataset.storedMask;
          }
        });
        // If user starts typing, mark as actively editing
        input.addEventListener('input', () => {
          input.dataset.userEditing = 'true';
          delete input.dataset.storedMask;
        });
        // On blur with empty field after clearing, restore the mask
        input.addEventListener('blur', () => {
          if (input.value === '' && info.connected && info.masked) {
            input.value = info.masked;
            input.dataset.storedMask = 'true';
            delete input.dataset.userEditing;
          }
        });
      }
    }
  } else {
    badge.innerHTML = `<span>${t('connectors.not_set')}</span>`;
    badge.className = 'conn-badge';
    if (input) {
      input.value = '';
      delete input.dataset.storedMask;
      delete input.dataset.userEditing;
    }
  }

  // Env hint line
  const hintEl = $(`conn-env-hint-${provider}`);
  if (hintEl) hintEl.style.display = (info.connected && info.from_env) ? '' : 'none';
}

async function saveConnector(provider) {
  const keyEl = $(`conn-key-${provider}`);
  if (!keyEl) return;
  const key = keyEl.value.trim();
  const statusEl = $(`conn-status-${provider}`);

  statusEl.textContent = t('connectors.saving');
  statusEl.className = 'conn-status';

  try {
    const res = await fetch('/connectors/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, api_key: key }),
    });
    const data = await res.json();
    if (data.ok) {
      statusEl.textContent = t('connectors.saved');
      statusEl.className = 'conn-status ok';
      // Refresh badges + status row
      loadConnectors();
    } else {
      statusEl.textContent = `✗ ${data.error}`;
      statusEl.className = 'conn-status err';
    }
  } catch (e) {
    statusEl.textContent = `✗ ${e.message}`;
    statusEl.className = 'conn-status err';
  }
}

async function verifyConnector(provider) {
  const keyEl = $(`conn-key-${provider}`);
  if (!keyEl) return;
  // If the field contains the stored-mask placeholder (or is empty), send '' —
  // the backend will fall back to the stored key from config/env.
  const isPlaceholder = keyEl.dataset.storedMask === 'true';
  const key = isPlaceholder ? '' : keyEl.value.trim();
  const statusEl = $(`conn-status-${provider}`);

  // Only block if truly no key anywhere (field empty AND not connected)
  if (!key && !isPlaceholder) {
    const badge = $(`conn-badge-${provider}`);
    const isConnected = badge && badge.classList.contains('ok');
    if (!isConnected) {
      statusEl.textContent = t('verify.enter_key');
      statusEl.className = 'conn-status err';
      return;
    }
  }

  statusEl.textContent = t('connectors.checking');
  statusEl.className = 'conn-status';

  try {
    const res = await fetch('/connectors/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, api_key: key }),
    });
    const data = await res.json();
    if (data.ok) {
      if (provider === 'anthropic') {
        statusEl.textContent = t('connectors.verify_ok_anthropic', { model: data.model });
      } else {
        statusEl.textContent = t('connectors.verify_ok_openai');
      }
      statusEl.className = 'conn-status ok';
    } else {
      statusEl.textContent = t('connectors.verify_fail', { error: data.error || 'error' });
      statusEl.className = 'conn-status err';
    }
  } catch (e) {
    statusEl.textContent = t('connectors.verify_fail', { error: e.message });
    statusEl.className = 'conn-status err';
  }
}

// ── File / folder picker + info ───────────────────────────
let pickerBusy = false;

async function runPicker(endpoint) {
  if (pickerBusy) return;
  pickerBusy = true;
  const buttons = document.querySelectorAll('.btn-pick');
  buttons.forEach(b => b.disabled = true);
  try {
    const res = await fetch(endpoint);
    const data = await res.json();
    if (data.path) {
      $('path').value = data.path;
      loadPathInfo();
      updateStartEnabled();
    }
  } finally {
    pickerBusy = false;
    buttons.forEach(b => b.disabled = false);
  }
}

function pickFolder() { runPicker('/pick-folder'); }
function pickFile()   { runPicker('/pick-file');   }

function onFileToggle(checkbox) {
  const li = checkbox.closest('li');
  li.classList.toggle('deselected', !checkbox.checked);
  // Update summary header count
  const list = document.getElementById('path-file-list');
  if (!list) return;
  const checked = list.querySelectorAll('input[type="checkbox"]:checked').length;
  const total   = list.querySelectorAll('input[type="checkbox"]').length;
  const header  = document.querySelector('.path-info-header');
  if (header && checked !== total) {
    // Dim summary to show selection is active
    header.style.opacity = '0.7';
  } else if (header) {
    header.style.opacity = '';
  }
}

/** Returns array of selected filenames, or empty array (= all files) */
function getSelectedFiles() {
  const list = document.getElementById('path-file-list');
  if (!list) return [];
  const all  = list.querySelectorAll('li[data-filename]');
  const selected = [...all]
    .filter(li => li.querySelector('input[type="checkbox"]')?.checked)
    .map(li => li.dataset.filename);
  // If all selected, send empty (server interprets as "all")
  if (selected.length === all.length) return [];
  return selected;
}

let pathInfoTimer = null;
function onPathChange() {
  if (pathInfoTimer) clearTimeout(pathInfoTimer);
  pathInfoTimer = setTimeout(loadPathInfo, 500);
  updateStartEnabled();
}

async function loadPathInfo() {
  const path = $('path').value.trim();
  const infoEl = $('path-info');
  if (!path) {
    infoEl.style.display = 'none';
    return;
  }
  try {
    const res = await fetch('/folder-info?path=' + encodeURIComponent(path));
    const data = await res.json();
    if (data.error) {
      infoEl.innerHTML = `<span class="path-info-err">⚠ ${escHtml(data.error)}</span>`;
      infoEl.style.display = 'block';
      return;
    }
    if (data.is_file) {
      const f = data.files[0];
      if (f) {
        const icon = f.type === 'video' ? '🎬' : '📷';
        infoEl.innerHTML = `<div class="path-info-header">${icon} <b>${escHtml(f.name)}</b> — ${escHtml(f.size)}</div>`;
      } else {
        infoEl.innerHTML = `<span class="path-info-err">${escHtml(t('path_info.unsupported'))}</span>`;
      }
    } else {
      const parts = [];
      if (data.videos > 0) parts.push(`${data.videos} ${t('path_info.video')}`);
      if (data.photos > 0) parts.push(`${data.photos} ${t('path_info.photos')}`);
      const summary = parts.join(', ') || t('path_info.no_supported');
      let html = `<div class="path-info-header">📁 <b>${escHtml(data.name)}</b> — ${escHtml(summary)}</div>`;
      if (data.count > 0) {
        html += '<ul class="path-info-files" id="path-file-list">';
        for (const f of data.files) {
          const icon = f.type === 'video' ? '🎬' : '📷';
          const safeName = escHtml(f.name);
          html += `<li data-filename="${safeName}">` +
            `<input type="checkbox" checked onchange="onFileToggle(this)">` +
            `<span class="file-name">${icon} ${safeName}</span>` +
            `<span class="file-size">${escHtml(f.size)}</span>` +
            `</li>`;
        }
        if (data.has_more) {
          const more = t('path_info.more', { count: data.count - data.files.length });
          html += `<li class="muted" style="list-style:none;padding:2px 0 0 19px">${escHtml(more)}</li>`;
        }
        html += '</ul>';
      }
      infoEl.innerHTML = html;
    }
    infoEl.style.display = 'block';
  } catch (e) {
    infoEl.innerHTML = `<span class="path-info-err">⚠ ${escHtml(e.message)}</span>`;
    infoEl.style.display = 'block';
  }
}

// ── People list (dynamic +/-) ─────────────────────────────
const DEFAULT_PEOPLE_FALLBACK = [
  { name: 'Filip', desc: 'mężczyzna, kierowca motocykla' },
  { name: 'Jadzia', desc: 'kobieta, pasażerka' },
];

function renderPeople(people) {
  const list = $('people-list');
  list.innerHTML = '';
  people.forEach(p => addPersonRow(p.name, p.desc));
}

function addPerson() {
  addPersonRow('', '');
}

function addPersonRow(name, desc) {
  const list = $('people-list');
  const row = document.createElement('div');
  row.className = 'person-row';
  const namePh = t('form.people.name_placeholder');
  const descPh = t('form.people.desc_placeholder');
  const removeTitle = t('form.people.remove_title');
  row.innerHTML = `
    <input type="text" class="person-name" placeholder="${escHtml(namePh)}" value="${escHtml(name)}">
    <input type="text" class="person-desc" placeholder="${escHtml(descPh)}" value="${escHtml(desc)}">
    <button class="btn-mini btn-remove" onclick="removePerson(this)" title="${escHtml(removeTitle)}">−</button>
  `;
  list.appendChild(row);
}

function removePerson(btn) {
  btn.closest('.person-row').remove();
}

function getPeopleString() {
  const rows = document.querySelectorAll('.person-row');
  return [...rows].map(r => {
    const name = r.querySelector('.person-name').value.trim();
    const desc = r.querySelector('.person-desc').value.trim();
    if (!name) return '';
    return desc ? `${name} - ${desc}` : name;
  }).filter(s => s).join('; ');
}

function onTranscribeChange() {
  const checked = $('transcribe').checked;
  $('whisper-row').classList.toggle('visible', checked);
  updateStartEnabled();
}

// ── Start button enable/disable + tooltip ─────────────────
function updateStartEnabled() {
  if (activelyProcessing) return;  // locked during processing
  const path = $('path').value.trim();
  const aiOn = $('analyze_images').checked;
  const transcribeOn = $('transcribe').checked;

  let reason = '';
  if (!path) reason = t('tooltip.start_no_path');
  else if (!aiOn && !transcribeOn) reason = t('tooltip.start_no_features');
  else if (aiOn && !anthropicConnected) reason = t('tooltip.start_no_key');

  const btn = $('btn-start');
  btn.disabled = !!reason;
  btn.title = reason || '';
}

// ── UI updates ────────────────────────────────────────────
function setStatus(state, text, stateKey) {
  $('status-dot').className = 'dot ' + state;
  const el = $('status-text');
  el.textContent = text;
  if (stateKey) el.dataset.stateKey = stateKey;
  else delete el.dataset.stateKey;
}

function addLog(text, cls = '') {
  const log = $('log');
  const empty = $('empty-state');
  if (empty) empty.remove();
  const ts = new Date().toTimeString().slice(0, 8);
  const line = document.createElement('div');
  line.className = 'log-line' + (cls ? ' ' + cls : '');
  line.innerHTML = `<span class="ts">${ts}</span><span class="msg">${escHtml(text)}</span>`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function addFileCard(icon, name, preview, outputPath, fileTokens, fileCost) {
  const cards = $('file-cards');
  cards.style.display = 'flex';
  if ($('card-' + name)) return;
  const card = document.createElement('div');
  card.className = 'file-card';
  card.id = 'card-' + name;

  // Inline usage (tokens · cost) on same line as filename, separated by ·
  const usagePart = (fileTokens > 0)
    ? `<span class="file-usage">· ${formatTokens(fileTokens)} tok · $${fileCost.toFixed(3)}</span>`
    : '';

  card.innerHTML = `
    <div class="icon">${icon}</div>
    <div style="flex:1;min-width:0">
      <div class="file-card-name-row">
        <span class="name">${escHtml(name)}</span>
        ${usagePart}
      </div>
      ${preview ? `<div class="preview">${escHtml(preview)}</div>` : ''}
    </div>
    ${outputPath ? `<button class="reveal-btn" title="${t('file_card.open_title') || 'Open file'}">${t('file_card.open_btn') || 'Open'}</button>` : ''}`;

  if (outputPath) {
    const btn = card.querySelector('.reveal-btn');
    btn.addEventListener('click', () => openFile(outputPath));
  }

  cards.appendChild(card);
  cards.scrollTop = cards.scrollHeight;
}

async function openFile(path) {
  await fetch('/open-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
}

// ── Whisper active indicator ──────────────────────────────
let _whisperActiveTimer = null;
function setWhisperActive(active) {
  const el = $('m-whisper-status');
  if (!el) return;
  clearTimeout(_whisperActiveTimer);
  if (active) {
    el.textContent = '●';
    el.className = 'metric-value active';
  } else {
    el.textContent = '';
    el.className = 'metric-value idle';
  }
}

// ── Live step status ──────────────────────────────────────
function _fmtElapsed(raw) {
  // "0min 25s" → "25s"  |  "2min 03s" → "2min 03s"  |  pass-through anything else
  if (!raw) return '';
  const m = raw.match(/^(\d+)min\s+(\d+)s$/);
  if (!m) return raw;
  const min = parseInt(m[1], 10);
  const sec = parseInt(m[2], 10);
  return min === 0 ? `${sec}s` : `${min}min ${sec}s`;
}

function updateStepStatus(msg) {
  const el = $('step-status');
  if (!el) return;
  el.style.display = 'flex';

  // Whisper with VAD never reaches 100% — use an indeterminate shimmer + mic icon
  const isWhisper = msg.step && /transcrib|whisper/i.test(msg.step);

  const icon = $('step-icon');
  if (icon) icon.textContent = isWhisper ? '🎙' : '⏳';

  // Clean up step name: strip trailing "..." and backend noise
  const stepName = (msg.step || '').replace(/\.\.\.$/, '').replace(/\s*\(Whisper\)/, '');
  $('step-name').textContent = stepName;

  const bar     = $('step-bar');
  const barWrap = bar.parentElement;
  const pct     = $('step-pct');
  const elapsed = $('step-elapsed');
  const hasProgress = !isWhisper && msg.progress !== null && msg.progress !== undefined;

  barWrap.classList.toggle('indeterminate', isWhisper);

  if (isWhisper) {
    // Indeterminate shimmer + elapsed — no percentage (VAD makes it meaningless)
    barWrap.style.display = 'block';
    pct.style.display = 'none';
    const t = _fmtElapsed(msg.elapsed);
    elapsed.textContent = t;
    elapsed.style.display = t ? 'inline' : 'none';
  } else if (hasProgress) {
    bar.style.width = (msg.progress * 100).toFixed(0) + '%';
    barWrap.style.display = 'block';
    pct.textContent = msg.progress_label || `${(msg.progress * 100).toFixed(0)}%`;
    pct.style.display = 'inline';
    elapsed.style.display = 'none';
  } else {
    barWrap.style.display = 'none';
    pct.style.display = 'none';
    elapsed.textContent = _fmtElapsed(msg.elapsed);
    elapsed.style.display = 'inline';
  }

  $('step-eta').textContent = msg.eta_files || '';
}

function hideStepStatus() {
  const el = $('step-status');
  if (el) el.style.display = 'none';
}

// ── Token usage / cost (tracked internally; shown per-file in cards) ─────────
function updateUsage(msg) {
  // Usage widget removed from header — costs are shown inline in file cards.
  // This function kept as a no-op stub so state restore still works cleanly.
}

function formatTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
  return String(n);
}

function formatCost(usd) {
  if (usd >= 1000) return '$' + (usd / 1000).toFixed(1) + 'k';
  if (usd >= 100)  return '$' + usd.toFixed(0);      // $100, $234
  return '$' + usd.toFixed(2);                        // $0.00 – $99.99
}

// ── SSE message handling ──────────────────────────────────
function handleMsg(msg) {
  if (msg.type === 'ping') return;
  if (msg.type === 'log') {
    addLog(msg.text);
  } else if (msg.type === 'warn') {
    addLog(msg.text, 'warn');
  } else if (msg.type === 'step_status') {
    updateStepStatus(msg);
    setWhisperActive(msg.step && /transcrib|whisper/i.test(msg.step));
  } else if (msg.type === 'usage') {
    updateUsage(msg);
  } else if (msg.type === 'total') {
    totalFiles = msg.total;
  } else if (msg.type === 'progress') {
    setWhisperActive(false);
    const pct = Math.round((msg.current / msg.total) * 100);
    $('progress-bar').style.width = pct + '%';
    setStatus('running', `[${msg.current}/${msg.total}] ${msg.file}`);
    hideStepStatus();
  } else if (msg.type === 'done_file') {
    setWhisperActive(false);
    addFileCard('✅', msg.file, msg.preview, msg.output, msg.file_tokens || 0, msg.file_cost || 0);
    hideStepStatus();
  } else if (msg.type === 'skipped') {
    setWhisperActive(false);
    addFileCard('⏭️', msg.file, t('status.skipped'));
  } else if (msg.type === 'error_file') {
    setWhisperActive(false);
    addLog(`${t('status.error_file_prefix')}: ${msg.file} — ${msg.error}`, 'err');
    addFileCard('❌', msg.file, msg.error);
    hideStepStatus();
  } else if (msg.type === 'error') {
    setWhisperActive(false);
    addLog(msg.text, 'err');
    setStatus('error', t('status.error'), 'status.error');
    activelyProcessing = false;
    hideStepStatus();
    resetUI();
  } else if (msg.type === 'done') {
    setWhisperActive(false);
    $('progress-bar').style.width = '100%';
    setStatus('done', t('status.done_summary', msg));
    addLog(t('status.finished_summary', msg), 'ok');
    activelyProcessing = false;
    hideStepStatus();
    resetUI();
  }
}

// ── Start / stop ──────────────────────────────────────────
function startProcessing(resumeExtra = {}, callbacks = {}) {
  const path = $('path').value.trim();
  if (!path) { alert(t('alerts.provide_path')); return; }

  const ctxEl = $('context');
  const context = ctxEl.value.trim() || ctxEl.placeholder || '';

  const config = {
    path:           path,
    people:         getPeopleString(),
    context:        context,
    interval:       parseInt($('interval').value) || 5,
    analyze_images: $('analyze_images').checked,
    transcribe:     $('transcribe').checked,
    whisper_model:  $('whisper_model').value,
    output_dir:     null,
    overwrite:      $('overwrite').checked,
    generate_summary: $('generate_summary').checked,
    budget_usd:     (v => isNaN(v) ? null : v)(parseFloat($('budget_usd').value)),
    files:          getSelectedFiles(),  // [] = all, [...] = filtered subset
    ...resumeExtra,
  };

  $('log').innerHTML = '';
  $('file-cards').innerHTML = '';
  $('file-cards').style.display = 'none';
  $('progress-bar').style.width = '0%';
  totalFiles = 0;
  activelyProcessing = true;
  claimTab();

  $('btn-start').style.display = 'none';
  $('btn-stop').style.display = 'block';
  setStatus('running', t('status.processing'), 'status.processing');
  setFormLocked(true);

  fetch('/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config)
  }).then(r => r.json()).then(data => {
    if (data.error) {
      addLog(data.error, 'err');
      resetUI();
      if (callbacks.onError) callbacks.onError();
      return;
    }
    if (callbacks.onSuccess) callbacks.onSuccess();
    connectStream();
  }).catch(err => {
    addLog(`Failed to start: ${err}`, 'err');
    resetUI();
    if (callbacks.onError) callbacks.onError();
  });
}

function stopProcessing() {
  fetch('/stop', { method: 'POST' });
  setStatus('idle', t('status.stopping'), 'status.stopping');
  activelyProcessing = false;
  $('btn-stop').disabled = true;
}

function resetUI() {
  releaseTab();
  $('btn-start').style.display = 'block';
  $('btn-stop').style.display = 'none';
  $('btn-stop').disabled = false;
  setFormLocked(false);
  updateStartEnabled();
}

function setFormLocked(locked) {
  // CSS class kills pointer-events on left panel (except form-actions)
  const panelLeft = document.querySelector('.panel-left');
  if (panelLeft) panelLeft.classList.toggle('locked', locked);

  // Also disable Settings pane while processing
  document.querySelectorAll('#pane-settings input, #pane-settings textarea, #pane-settings select, #pane-settings button').forEach(el => {
    el.disabled = locked;
  });
  const status = $('settings-status');
  if (status) {
    if (locked) {
      status.textContent = t('lock.banner');
      status.className = 'warn';
    } else if (status.className === 'warn') {
      status.textContent = '';
      status.className = '';
    }
  }
}

// ── SSE connection (with auto-reconnect) ──────────────────
function connectStream() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (eventSource) eventSource.close();

  eventSource = new EventSource('/stream');
  eventSource.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    handleMsg(msg);
    if (msg.type === 'done' || msg.type === 'error') eventSource.close();
  };
  eventSource.onerror = () => {
    eventSource.close();
    if (activelyProcessing) scheduleReconnect();
    else resetUI();
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  setStatus('running', t('status.connection_lost'), 'status.connection_lost');
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    try {
      const res = await fetch('/status');
      const data = await res.json();
      if (data.processing) {
        addLog(t('status.reconnected'), 'ok');
        connectStream();
      } else {
        await restoreState();
      }
    } catch {
      scheduleReconnect();
    }
  }, 3000);
}

async function restoreState() {
  try {
    const res = await fetch('/state');
    const state = await res.json();

    if (state.usage && (state.usage.input || state.usage.output)) {
      updateUsage(state.usage);
    }

    if (state.log.length === 0 && !state.processing) return;

    addLog(t('status.restoring'), 'ok');
    for (const msg of state.log) handleMsg(msg);

    if (state.processing) {
      activelyProcessing = true;
      $('btn-start').style.display = 'none';
      $('btn-stop').style.display = 'block';
      setStatus('running', state.progress?.file
        ? `[${state.progress.current}/${state.progress.total}] ${state.progress.file}`
        : t('status.processing'));
      setFormLocked(true);
      connectStream();
    }
  } catch (e) {
    console.warn('Failed to fetch state:', e);
  }
}

// ── Static system info (fetched once on load) ─────────────
async function fetchSysinfo() {
  try {
    const res = await fetch('/sysinfo');
    if (!res.ok) return;
    const s = await res.json();

    const wrap = $('m-whisper-wrap');
    const label = $('m-whisper-label');
    const status = $('m-whisper-status');

    if (s.whisper_backend) {
      label.textContent = s.whisper_backend === 'mlx' ? 'NE' : 'WSPR';
      status.textContent = '';
      status.className = 'metric-value idle';
      wrap.title = `Whisper: ${s.whisper_label}${s.apple_silicon ? ' · Apple Silicon' : ''} · ${s.ram_gb} GB RAM`;
      wrap.style.display = '';
    }

    lastSysinfo = s;
    renderSyscheck(s);

    // Update Start button enable state based on Anthropic key presence
    if (typeof s.anthropic_connected !== 'undefined') {
      _setAnthropicConnected(s.anthropic_connected);
    }
  } catch {
    // server not ready yet — ignore
  }
}

// ── System metrics polling ────────────────────────────────
async function pollMetrics() {
  try {
    const res = await fetch('/metrics');
    if (!res.ok) return;
    const m = await res.json();

    const cpuEl = $('m-cpu');
    cpuEl.textContent = `${m.cpu.toFixed(0)}%`;
    cpuEl.className = 'metric-value' + (m.cpu > 90 ? ' hot' : m.cpu > 70 ? ' warn' : '');

    const ramEl = $('m-ram');
    ramEl.textContent = `${m.ram.toFixed(0)}%`;
    ramEl.className = 'metric-value' + (m.ram > 90 ? ' hot' : m.ram > 75 ? ' warn' : '');

    const loadEl = $('m-load');
    loadEl.textContent = m.load.toFixed(1);
    const loadRatio = m.load / m.ncpu;
    loadEl.className = 'metric-value' + (loadRatio > 2 ? ' hot' : loadRatio > 1.3 ? ' warn' : '');

    const therm = $('m-thermal');
    therm.className = 'thermal ' + m.thermal;
    therm.title = `${t('metrics.thermal_tooltip')}: ${m.thermal_label}`;
  } catch {
    // server offline — keep previous values
  }
}

// ── Tabs (Processing / Settings / Connectors) ─────────────
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  $('pane-run').style.display         = tab === 'run'         ? '' : 'none';
  $('pane-settings').style.display    = tab === 'settings'    ? '' : 'none';
  $('pane-connectors').style.display  = tab === 'connectors'  ? '' : 'none';
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.tab');
  if (btn) switchTab(btn.dataset.tab);
});

// ── Settings: load / save / reset / preset ────────────────
async function loadSettings() {
  const status = $('settings-status');
  status.textContent = t('settings.loading');
  status.className = '';
  try {
    const res = await fetch('/config');
    const data = await res.json();
    fillSettingsForm(data.config, data.prompt);
    status.textContent = '';
  } catch (e) {
    status.textContent = '✗ ' + e.message;
    status.className = 'err';
  }
}

// Tracks the active provider name (read from config) so save can write
// back to the same provider section. Currently only 'anthropic' but ready
// to extend when more providers land.
let activeProviderName = 'anthropic';

function fillSettingsForm(cfg, prompt) {
  activeProviderName = cfg.ai?.provider || 'anthropic';
  const p = cfg.ai[activeProviderName];

  $('cfg-model').value = p.model;
  $('cfg-max-video').value = p.max_tokens_video;
  $('cfg-max-photo').value = p.max_tokens_photo;
  $('cfg-price-in').value = p.price_input_per_mtok_usd;
  $('cfg-price-out').value = p.price_output_per_mtok_usd;
  $('cfg-timeout').value = p.timeout_sec;

  $('cfg-video-width').value = cfg.frames.video_width_px;
  $('cfg-photo-width').value = cfg.frames.photo_width_px;
  $('cfg-jpeg-q').value = cfg.frames.jpeg_quality;
  $('cfg-max-frames').value = cfg.frames.max_per_video;

  $('cfg-whisper-default').value = cfg.whisper.default_model;
  $('cfg-whisper-tiers').value = cfg.whisper.fallback_tiers.join(', ');

  $('cfg-default-context').value = cfg.defaults.context;
  $('cfg-default-interval').value = cfg.defaults.interval_sec;

  $('cfg-prompt').value = prompt;
}

function readSettingsForm() {
  const providerCfg = {
    model: $('cfg-model').value.trim(),
    max_tokens_video: parseInt($('cfg-max-video').value),
    max_tokens_photo: parseInt($('cfg-max-photo').value),
    price_input_per_mtok_usd: parseFloat($('cfg-price-in').value),
    price_output_per_mtok_usd: parseFloat($('cfg-price-out').value),
    timeout_sec: parseInt($('cfg-timeout').value),
  };
  return {
    config: {
      ai: {
        provider: activeProviderName,
        [activeProviderName]: providerCfg,
      },
      frames: {
        video_width_px: parseInt($('cfg-video-width').value),
        photo_width_px: parseInt($('cfg-photo-width').value),
        jpeg_quality: parseInt($('cfg-jpeg-q').value),
        max_per_video: parseInt($('cfg-max-frames').value),
      },
      whisper: {
        default_model: $('cfg-whisper-default').value,
        fallback_tiers: $('cfg-whisper-tiers').value.split(',').map(s => s.trim()).filter(Boolean),
        timeout_sec: 300,
      },
      defaults: {
        people: [],
        context: $('cfg-default-context').value,
        interval_sec: parseInt($('cfg-default-interval').value),
      },
      server: { port: 5555, log_buffer_max: 500, heartbeat_sec: 2 },
    },
    prompt: $('cfg-prompt').value,
  };
}

async function saveSettings() {
  const status = $('settings-status');
  status.textContent = t('settings.saving');
  status.className = '';
  try {
    const body = readSettingsForm();
    const current = await (await fetch('/config')).json();
    body.config.defaults.people = current.config.defaults.people;
    body.config.whisper.timeout_sec = current.config.whisper.timeout_sec;
    body.config.server = current.config.server;

    const res = await fetch('/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.ok) {
      status.textContent = t('settings.saved');
      status.className = 'ok';
    } else {
      status.textContent = '✗ ' + (data.error || 'error');
      status.className = 'err';
    }
  } catch (e) {
    status.textContent = '✗ ' + e.message;
    status.className = 'err';
  }
}

async function resetSettings() {
  // Resets ONLY config values (not the prompt — prompt has its own preset buttons)
  if (!confirm(t('settings.reset_confirm'))) return;
  const status = $('settings-status');
  status.textContent = t('settings.resetting');
  status.className = '';
  try {
    const res = await fetch('/config/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ what: 'config' }),
    });
    const data = await res.json();
    if (data.ok) {
      fillSettingsForm(data.config, data.prompt);
      status.textContent = t('settings.restored');
      status.className = 'ok';
    }
  } catch (e) {
    status.textContent = '✗ ' + e.message;
    status.className = 'err';
  }
}

async function loadPromptPreset(lang) {
  const langLabel = t('lang.' + lang);
  if (!confirm(t('settings.prompt_preset_confirm', { lang: langLabel }))) return;
  const status = $('settings-status');
  status.textContent = t('settings.resetting');
  status.className = '';
  try {
    const res = await fetch('/config/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ what: 'prompt', lang }),
    });
    const data = await res.json();
    if (data.ok) {
      $('cfg-prompt').value = data.prompt;
      status.textContent = t('settings.prompt_loaded', { lang: langLabel });
      status.className = 'ok';
    }
  } catch (e) {
    status.textContent = '✗ ' + e.message;
    status.className = 'err';
  }
}

// ── Guard against accidental tab close ────────────────────
window.addEventListener('beforeunload', (e) => {
  if (activelyProcessing) {
    e.preventDefault();
    e.returnValue = '';
    return '';
  }
});

// ── Init ──────────────────────────────────────────────────
window.addEventListener('load', async () => {
  // 1. Set initial language (must happen BEFORE rendering dynamic content)
  await setLang(detectInitialLang());
  checkSingleTab();

  // 2. Load people defaults from server config
  try {
    const res = await fetch('/config');
    const data = await res.json();
    const people = data.config?.defaults?.people;
    if (Array.isArray(people) && people.length > 0) {
      renderPeople(people);
    } else {
      renderPeople(DEFAULT_PEOPLE_FALLBACK);
    }
  } catch {
    renderPeople(DEFAULT_PEOPLE_FALLBACK);
  }

  // 3. Initial Start button state
  updateStartEnabled();

  // 4. Restore processing state if anything is running on server
  await restoreState();

  // 5. Check for interrupted batch and show resume banner (skip if already processing)
  if (!activelyProcessing) await checkBatchState();

  // 6. Start polling metrics + one-time system info
  fetchSysinfo();
  pollMetrics();
  setInterval(pollMetrics, 3000);
});

// ── Batch resume ──────────────────────────────────────────
let _batchStateData = null;

async function checkBatchState() {
  try {
    const data = await fetch('/batch-state').then(r => r.json());
    if (!data || !data.config) return;
    _batchStateData = data;
    const banner = $('resume-banner');
    const text = $('resume-banner-text');
    const processed = data.processed || 0;
    const total = data.total || '?';
    const cost = `$${(data.cost_usd || 0).toFixed(2)}`;
    text.textContent = t('resume.banner', { processed, total, cost });
    banner.style.display = 'flex';
  } catch { /* no state or network error — stay silent */ }
}

function resumeBatch() {
  if (!_batchStateData) return;
  const s = _batchStateData;
  $('resume-banner').style.display = 'none';
  // _batchStateData cleared only after /start confirms success —
  // if start fails, banner is restored so the user can retry resume.
  const cfg = s.config;
  // Restore form fields from saved config
  if (cfg.path)     $('path').value = cfg.path;
  if (cfg.interval) $('interval').value = cfg.interval;
  if (cfg.context !== undefined) $('context').value = cfg.context || '';
  $('analyze_images').checked  = !!cfg.analyze_images;
  $('transcribe').checked      = !!cfg.transcribe;
  $('overwrite').checked       = !!cfg.overwrite;
  $('generate_summary').checked = cfg.generate_summary !== false;
  if (cfg.whisper_model) $('whisper_model').value = cfg.whisper_model;
  onTranscribeChange();
  loadPathInfo();
  // Start with resume offset — pass saved people/files directly so they
  // aren't rebuilt from the form (which may not be fully restored yet)
  startProcessing({
    people:            cfg.people ?? getPeopleString(),
    files:             Array.isArray(cfg.files) ? cfg.files : [],
    budget_usd:        cfg.budget_usd ?? null,
    resume_from_index:    s.next_index,
    resume_next_filepath: s.next_filepath ?? null,
    resume_processed:     s.processed,
    resume_skipped:       s.skipped,
    resume_errors:        s.errors,
    resume_cost_usd:      s.cost_usd ?? 0,
  }, {
    onSuccess: () => { _batchStateData = null; },
    onError:   () => { $('resume-banner').style.display = 'flex'; },
  });
}

async function discardBatch() {
  _batchStateData = null;
  $('resume-banner').style.display = 'none';
  await fetch('/batch-state/discard', { method: 'POST' });
}
