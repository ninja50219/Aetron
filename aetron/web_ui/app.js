/* Keep selection explicit: rendering an outline must never fetch source. */
'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="aetron-token"]').content;
let state = {}, busy = false, asking = false, candidates = [], outline = null, source = null;
let selectedPath = '', selectedName = '', reportText = '', lastQuery = '', toastTimer;
const labels = {python:'Python',csharp:'C#',javascript:'JavaScript',typescript:'TypeScript',lua:'Lua',luau:'Luau'};
const languageName = name => labels[name] || name;
const node = (tag, value = '', className = '') => {
  const el = document.createElement(tag);
  el.textContent = value;
  if (className) el.className = className;
  return el;
};
const show = (id, visible) => $(id).classList.toggle('hidden', !visible);
const basename = path => path.split(/[\\/]/).pop();
const empty = (title, message) => {
  const el = node('div', '', 'empty');
  el.append(node('strong', title), node('p', message));
  return el;
};
function stored(key, fallback) {
  try { return JSON.parse(localStorage.getItem('aetron.' + key)) ?? fallback; }
  catch { return fallback; }
}
function persist(key, value) {
  try { localStorage.setItem('aetron.' + key, JSON.stringify(value)); }
  catch { /* A blocked browser store must not prevent code browsing. */ }
}
let histories = stored('searches', {});
if (!histories || typeof histories !== 'object' || Array.isArray(histories)) histories = {};
function recentQueries() {
  const values = histories[state.root];
  return Array.isArray(values) ? values.filter(q => typeof q === 'string').slice(0, 8) : [];
}
function renderSearchHistory() {
  $('recentSearches').replaceChildren();
  for (const query of recentQueries()) {
    const b = node('button', query);
    b.title = query;
    b.onclick = () => runSearch(query);
    $('recentSearches').append(b);
  }
  if (!recentQueries().length) $('recentSearches').append(node('p', 'Your searches will appear here.', 'sidebar-note'));
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';
  $('themeLabel').textContent = theme === 'dark' ? 'Light appearance' : 'Dark appearance';
  persist('theme', theme);
}
applyTheme(stored('theme', 'light'));
function notify(message) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  show('toast', true);
  toastTimer = setTimeout(() => show('toast', false), 2600);
}
async function copy(value, label) {
  try { await navigator.clipboard.writeText(value); notify(label + ' copied'); }
  catch { notify('Clipboard unavailable. Select the text and copy it manually.'); }
}
async function api(action, data = {}) {
  let response;
  try {
    response = await fetch('/api/' + action, {method:'POST', headers:{'Content-Type':'application/json','X-Aetron-Token':token}, body:JSON.stringify({revision:state.revision,...data})});
  } catch { throw Error('Cannot reach Aetron. Keep its terminal open, or restart it and open the new address.'); }
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'This action could not be completed.');
  return result;
}
function availability() {
  for (const id of ['refresh','query','searchButton','overviewTab','omittedTab','askTab','explorerTab']) $(id).disabled = !state.root || busy;
  // A question in flight holds the index it started with, so a rescan would be
  // refused by the server anyway. Saying so with a disabled button beats an
  // error banner for something the page already knew.
  for (const id of ['question','askButton','provider','model']) $(id).disabled = !state.root || busy || asking;
  if (asking) $('refresh').disabled = true;
  $('languageFilter').disabled = !candidates.length || busy;
  $('copyCode').disabled = !source?.text || busy;
  $('copyLocation').disabled = !source?.text || busy;
}
async function work(message, fn) {
  if (busy) return;
  busy = true;
  show('error', false);
  document.body.classList.add('busy');
  $('status').textContent = message;
  // Disable existing controls during a request; newly rendered selections use
  // the same busy gate, so rapid clicks cannot mix two project revisions.
  const controls = [...document.querySelectorAll('button,input,select')];
  const disabled = controls.map(el => el.disabled);
  controls.forEach(el => el.disabled = true);
  try { await fn(); $('status').textContent = 'Ready'; }
  catch (error) {
    $('errorText').textContent = error.message;
    show('error', true);
    $('status').textContent = 'Action failed · see message above';
  } finally {
    controls.forEach((el, i) => el.disabled = disabled[i]);
    busy = false;
    document.body.classList.remove('busy');
    availability();
  }
}
function view(name) {
  show('welcome', !state.root);
  show('askPanel', !!state.root && name === 'ask');
  show('explorer', !!state.root && name === 'explorer');
  show('reportPanel', !!state.root && !['ask','explorer'].includes(name));
  for (const id of ['ask','explorer','overview','omitted']) {
    $(id + 'Tab').classList.toggle('active', id === name);
    if (id === name) $(id + 'Tab').setAttribute('aria-current', 'page');
    else $(id + 'Tab').removeAttribute('aria-current');
  }
  $('pageName').textContent = {ask:'Ask Aetron',explorer:'Code explorer',overview:'Overview',omitted:'Excluded files'}[name];
}
function projectPicker() {
  if (busy) return;
  $('projectPath').value = '';
  $('projectDialog').showModal();
  $('projectPath').focus();
}
function renderState() {
  for (const target of ['recent','welcomeRecent']) {
    $(target).replaceChildren();
    for (const path of state.recent || []) {
      const b = node('button', '', 'project-row');
      b.title = path;
      const label = node('span');
      label.append(node('strong', basename(path)), node('small', path));
      b.append(node('span', '', 'folder-icon'), label, node('span', '→', 'arrow'));
      b.onclick = () => openProject(path);
      $(target).append(b);
    }
    if (!state.recent?.length) $(target).append(empty('No recent projects', 'Projects you open will be saved here.'));
  }
  $('warning').textContent = state.gitignore ? '' : 'Project .gitignore rules are not active because the optional pathspec package is missing.';
  show('warning', !state.gitignore);
  if (state.root) {
    $('projectName').textContent = state.name;
    $('projectSwitch').title = state.root;
    $('projectHint').textContent = 'Switch project';
    $('breadcrumb').textContent = state.name;
    $('projectStats').replaceChildren();
    for (const [count, label] of [[state.files,'files'],[state.lines,'lines'],[state.parsed,'analyzed']]) {
      const span = node('span');
      span.append(node('strong', count.toLocaleString()), document.createTextNode(' ' + label));
      $('projectStats').append(span);
    }
    $('omittedCount').textContent = state.skipped.length + state.pruned.length;
    show('omittedCount', true);
    $('statusRight').textContent = state.root;
    $('statusRight').title = state.root;
  }
  renderSearchHistory();
}
function resetSource() {
  source = null;
  selectedName = '';
  show('sourceEmpty', true);
  show('sourceCode', false);
  show('sourceProblem', false);
  $('sourceCode').replaceChildren();
  $('sourceLocation').textContent = 'Select a definition';
}
function resetReader() {
  selectedPath = '';
  outline = null;
  resetSource();
  $('detailTitle').textContent = 'No file selected';
  $('detailPath').textContent = 'Choose a file from the search results';
  show('readerEmpty', true);
  show('fileContent', false);
  show('copyPath', false);
}
function resetSearch() {
  candidates = [];
  lastQuery = '';
  $('query').value = '';
  show('clearQuery', false);
  show('matchHint', false);
  $('languageFilter').replaceChildren(new Option('All languages', ''));
  $('resultCount').textContent = '0';
  $('searchDescription').textContent = 'Search matches names and docstrings. Try movement or login.';
  $('results').replaceChildren(empty('Find something in your project', 'Search for a file, class, or function using the field above.'));
  resetReader();
}
async function openProject(path) {
  if (busy) return;
  $('projectDialog').close();
  await work('Indexing project…', async () => {
    state = await api('open', {path});
    renderState(); resetSearch(); resetAsk(); view('ask');
    $('indexTime').textContent = 'Index up to date';
  });
  if (state.root) $('question').focus();
}
function renderCandidates() {
  $('results').replaceChildren();
  const visible = candidates.filter(c => !$('languageFilter').value || c.language === $('languageFilter').value);
  $('resultCount').textContent = visible.length;
  for (const c of visible) {
    const b = node('button', '', 'result-button');
    b.classList.toggle('selected', c.path === selectedPath);
    b.setAttribute('aria-pressed', String(c.path === selectedPath));
    b.title = c.path;
    const title = node('div', '', 'result-title');
    title.append(node('strong', basename(c.path)), node('span', c.percent + '%', 'relevance'));
    b.append(title, node('span', c.path, 'result-path'), node('div', c.reason + (c.parsed ? '' : ' · No parser available'), 'result-reason'));
    b.onclick = () => showFile(c);
    $('results').append(b);
  }
  if (!visible.length) {
    const el = empty(candidates.length ? 'No files in this language' : 'No matches for “' + lastQuery + '”', candidates.length ? 'Choose another language or clear the filter.' : 'Try part of a name. Search uses names and docstrings, so spelling matters.');
    const actions = node('div', '', 'suggestions');
    if (candidates.length) {
      const clear = node('button', 'All languages');
      clear.onclick = () => { $('languageFilter').value = ''; renderCandidates(); };
      actions.append(clear);
    } else {
      for (const query of recentQueries().filter(q => q !== lastQuery).slice(0, 3)) {
        const b = node('button', query); b.onclick = () => runSearch(query); actions.append(b);
      }
    }
    el.append(actions); $('results').append(el);
  }
}
async function runSearch(query) {
  if (!state.root || busy) return;
  query = query.trim();
  if (!query) { $('query').focus(); return; }
  await work('Searching the index…', async () => {
    const data = await api('search', {query});
    candidates = data.candidates; lastQuery = query; resetReader();
    $('query').value = query; show('clearQuery', true); view('explorer');
    histories[state.root] = [query, ...recentQueries().filter(q => q !== query)].slice(0, 8);
    persist('searches', histories); renderSearchHistory();
    $('languageFilter').replaceChildren(new Option('All languages', ''));
    for (const lang of [...new Set(candidates.map(c => c.language))].sort()) $('languageFilter').append(new Option(languageName(lang), lang));
    $('searchDescription').textContent = candidates.length + ' ' + (candidates.length === 1 ? 'file' : 'files') + ' matching “' + query + '”';
    show('matchHint', !!candidates.length);
    renderCandidates();
  });
}
function symbolGroup(s) {
  if (['function','method'].includes(s.kind)) return 'Functions & methods';
  if (['class','interface','struct','enum'].includes(s.kind)) return 'Types';
  return 'Fields & other definitions';
}
function renderSymbols() {
  $('symbols').replaceChildren();
  const filter = $('symbolFilter').value.trim().toLowerCase();
  const symbols = outline.symbols.filter(s => s.qualified_name.toLowerCase().includes(filter));
  $('symbolCount').textContent = filter ? symbols.length + '/' + outline.symbols.length : outline.symbols.length;
  for (const group of ['Functions & methods','Types','Fields & other definitions']) {
    const items = symbols.filter(s => symbolGroup(s) === group);
    if (!items.length) continue;
    $('symbols').append(node('div', group, 'symbol-group'));
    for (const s of items) {
      const b = node('button', '', 'symbol-button');
      const ambiguous = outline.symbols.filter(other => other.qualified_name === s.qualified_name).length > 1;
      b.disabled = ambiguous;
      b.title = ambiguous ? s.qualified_name + ': multiple definitions share this name; individual source selection is unavailable.' : s.kind + ' ' + s.qualified_name + ' · lines ' + s.line + '–' + s.end_line;
      b.setAttribute('aria-label', b.title);
      b.classList.toggle('selected', s.qualified_name === selectedName);
      b.setAttribute('aria-pressed', String(s.qualified_name === selectedName));
      b.append(node('span', group === 'Functions & methods' ? 'ƒ' : s.kind.slice(0,1).toUpperCase(), 'kind ' + (group === 'Functions & methods' ? 'callable' : group === 'Types' ? 'class' : '')), node('span', s.name, 'symbol-name'), node('span', s.line, 'symbol-line'));
      b.onclick = () => showSource(s);
      $('symbols').append(b);
    }
  }
  if (!symbols.length) $('symbols').append(empty(filter ? 'No matching definitions' : 'No definitions found', filter ? 'Try a shorter name or clear this filter.' : 'This file has no indexed definitions.'));
}
async function showFile(candidate) {
  await work('Loading definitions…', async () => {
    const data = await api('structure', {path:candidate.path});
    outline = data; selectedPath = candidate.path; resetSource(); renderCandidates();
    $('detailTitle').textContent = basename(selectedPath);
    $('detailPath').textContent = selectedPath;
    $('detailPath').title = selectedPath;
    show('copyPath', true); show('readerEmpty', false); show('fileContent', true);
    $('fileNotice').textContent = data.unavailable || data.summary || '';
    show('fileNotice', !!(data.unavailable || data.summary));
    $('symbolFilter').value = ''; renderSymbols();
    $('imports').replaceChildren(...data.imports.map(value => node('div', value)));
    show('importsDetails', !!data.imports.length);
  });
}
async function showSource(symbol) {
  await work('Reading ' + symbol.name + '…', async () => {
    const data = await api('source', {path:selectedPath, name:symbol.qualified_name});
    source = data; selectedName = symbol.qualified_name; renderSymbols();
    $('sourceLocation').textContent = symbol.name + ' · L' + data.line + '–' + data.end_line;
    $('sourceLocation').title = data.location;
    $('sourceProblem').textContent = data.problem ? data.problem + ' Rescan the project after code changes.' : '';
    show('sourceProblem', !!data.problem); show('sourceEmpty', false); show('sourceCode', !!data.text);
    $('sourceCode').replaceChildren();
    if (data.text) data.text.split('\n').forEach((line, i) => {
      const row = node('span', '', 'code-line');
      const number = node('span', data.start_line + i, 'line-number');
      number.setAttribute('aria-hidden', 'true');
      row.append(number, node('span', line || ' ', 'line-text'));
      $('sourceCode').append(row);
    });
    $('sourceCode').scrollTop = 0;
  });
}
function section(title) {
  const el = node('section', '', 'report-section'); el.append(node('h2', title)); $('report').append(el); return el;
}
async function overview() {
  await work('Building project overview…', async () => {
    const result = await api('summary'); reportText = result.text;
    $('reportTitle').textContent = 'Project overview';
    $('reportDescription').textContent = 'Measured structure, dependencies, and analysis limitations.';
    $('report').replaceChildren();
    for (const block of reportText.trim().split(/\n\s*\n/)) {
      const lines = block.split('\n');
      const title = lines.shift(); section(title).append(node('pre', lines.join('\n')));
    }
    view('overview');
  });
}
function omitted() {
  if (!state.root || busy) return;
  $('reportTitle').textContent = 'Excluded files';
  $('reportDescription').textContent = 'Every file or directory omitted from this index, with the scanner’s recorded reason.';
  $('report').replaceChildren();
  const records = [...state.skipped.map(s => [s.rel_path,s.reason]), ...state.pruned.map(p => [p,'Directory pruned by scanner ignore rules'])];
  reportText = records.map(row => row.join(' — ')).join('\n') || 'No files or directories were excluded.';
  for (const [title,rows] of [['Skipped files',state.skipped.map(s => [s.rel_path,s.reason])],['Pruned directories',state.pruned.map(p => [p,'Directory pruned by scanner ignore rules'])]]) {
    const box = section(title + ' (' + rows.length + ')');
    if (!rows.length) { box.append(node('pre', 'None')); continue; }
    const table = node('table', '', 'omitted-table');
    const head = node('thead'), header = node('tr');
    header.append(node('th','Path'),node('th','Reason')); head.append(header); table.append(head);
    const body = node('tbody');
    for (const row of rows) { const tr = node('tr'); tr.append(...row.map(value => node('td',value))); body.append(tr); }
    table.append(body); box.append(table);
  }
  view('omitted');
}

/* Asking a model -----------------------------------------------------------
   This runs outside work(), because a local model answers in minutes and the
   page has to stay readable meanwhile: the request starts the job and polling
   shows the steps as they happen. Only the controls that would disturb a job
   in flight are disabled. */
const SVG = 'http://www.w3.org/2000/svg';
const STEP_LABEL = {SEARCH:'SEARCH', STRUCTURE:'OUTLINE', SOURCE:'SOURCE', ANSWER:'ANSWER'};
const EXAMPLES = ['Where is movement?', 'How does saving work?', 'What starts the program?'];

function renderProviders() {
  if (!state.providers || $('provider').options.length) return;
  for (const name of state.providers) $('provider').append(new Option(name, name, name === state.default_provider, name === state.default_provider));
  providerNote();
}
function providerNote() {
  const local = (state.local_providers || []).includes($('provider').value);
  $('providerNote').textContent = local
    ? 'Runs on this computer. Nothing leaves it.'
    : 'Your question, and whatever the model asks for, is sent to this provider. Its API key is read from your environment, never from this page.';
  // A hosted model is named by the person paying for it; see openai_compatible.py.
  $('model').placeholder = local || $('provider').value === 'anthropic' ? 'Default model' : 'Model name (required)';
  // The sidebar said this unconditionally, which stopped being true the day a
  // hosted provider could be chosen.
  $('privacyNote').textContent = local ? 'Code stays on this computer' : `Questions go to ${$('provider').value}`;
}
function resetAsk() {
  $('trail').replaceChildren();
  $('answer').replaceChildren();
  show('askEmpty', true);
  show('askError', false);
  show('askSteps', false);
  $('question').value = '';
  $('askSuggestions').replaceChildren();
  for (const example of EXAMPLES) {
    const b = node('button', example);
    b.onclick = () => { $('question').value = example; runAsk(); };
    $('askSuggestions').append(b);
  }
}
function renderTrail(steps, running) {
  $('trail').replaceChildren();
  for (const step of steps) {
    const row = node('div', '', 'trail-step' + (step.refused ? ' refused' : ''));
    row.append(node('b', STEP_LABEL[step.command] || 'RETRY'), node('span', step.argument || step.note || '—'));
    $('trail').append(row);
  }
  if (running) {
    const row = node('div', '', 'trail-step pending');
    row.append(node('b', '···'), node('span', 'Waiting for the model…'));
    $('trail').append(row);
  }
  // Counted the way the answer card counts them, or the two disagree on screen:
  // ANSWER is the model stopping, not another request to Aetron.
  const requests = steps.filter(step => step.command && step.command !== 'ANSWER').length;
  $('askSteps').textContent = requests + ' ' + (requests === 1 ? 'request' : 'requests');
  show('askSteps', !!requests);
}
function askFailed(message) {
  asking = false;
  document.body.classList.remove('busy');
  $('status').textContent = 'Question failed · see message above';
  $('askError').textContent = message;
  show('askError', true);
  availability();
}
function ring(percent) {
  const box = node('div', '', 'score-ring');
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 120 120');
  svg.setAttribute('aria-hidden', 'true');
  const circumference = 2 * Math.PI * 52;
  for (const className of ['ring-track', 'ring-value']) {
    const circle = document.createElementNS(SVG, 'circle');
    for (const [name, value] of [['cx',60],['cy',60],['r',52],['class',className]]) circle.setAttribute(name, value);
    if (className === 'ring-value') {
      // An SVG presentation attribute, not a style: style-src no longer allows
      // inline styles, and this has to change with every answer.
      circle.setAttribute('stroke-dasharray', (circumference * percent / 100).toFixed(2) + ' ' + circumference.toFixed(2));
      circle.setAttribute('transform', 'rotate(-90 60 60)');
    }
    svg.append(circle);
  }
  const face = node('div', '', 'ring-face');
  face.append(node('strong', percent + '%'), node('small', 'CONFIDENCE'));
  box.append(svg, face);
  return box;
}
function renderAnswer(answer) {
  const cited = answer.citation;
  const card = node('section', '', 'answer-card');
  const head = node('div', '', 'answer-head');
  head.append(node('h2', 'Answer'), node('span', answer.requests + ' ' + (answer.requests === 1 ? 'request' : 'requests') + ' · ' +
    (answer.files_read.length ? 'source read from ' + answer.files_read.length + ' ' + (answer.files_read.length === 1 ? 'file' : 'files') : 'no source code read'), 'subtle'));
  const body = node('div', '', 'answer-body');
  if (cited) {
    // No score without a citation: "this project has no multiplayer code" is a
    // correct answer, and a nought beside it would read as a verdict on the
    // sentence when the number only ever rated a definition.
    const score = node('div', '', 'score');
    const caption = node('p', cited.checks_passed + ' of ' + cited.checks.length + ' checks passed');
    caption.append(node('em', 'not a probability'));
    score.append(ring(cited.confidence), caption);
    body.append(score);
  }
  const main = node('div', '', 'answer-main');
  main.append(node('p', answer.text, 'answer-text'));
  if (cited) {
    const cite = node('div', '', 'cite');
    const open = node('button', cited.location, 'chip open');
    open.title = 'Open ' + cited.rel_path + ' in the code explorer';
    open.onclick = () => openCited(cited);
    cite.append(open, node('span', cited.kind + ' ' + cited.qualified_name, 'chip'), node('span', 'lines ' + cited.line + '–' + cited.end_line, 'chip'));
    if (cited.language) cite.append(node('span', languageName(cited.language), 'chip'));
    main.append(cite);
    if (cited.summary) main.append(node('p', cited.summary, 'answer-summary'));
  }
  body.append(main);
  card.append(head, body);
  if (cited) {
    const checks = node('details', '', 'checks');
    checks.append(node('summary', 'How this number was reached'));
    const list = node('ul');
    for (const check of cited.checks) {
      const item = node('li');
      item.append(node('b', check.passed ? '✓' : '○', check.passed ? 'yes' : 'no'), node('span', check.detail));
      list.append(item);
    }
    checks.append(list);
    card.append(checks);
    if (cited.text) {
      const code = node('div', '', 'answer-code');
      const toolbar = node('div', '', 'source-toolbar');
      const copyButton = node('button', 'Copy code', 'button small');
      copyButton.onclick = () => copy(cited.text, 'Code');
      toolbar.append(node('span', cited.kind + ' ' + cited.qualified_name + ' · ' + cited.location), copyButton);
      const pre = node('pre', '', 'source-code');
      cited.text.split('\n').forEach((line, i) => {
        const row = node('span', '', 'code-line');
        const number = node('span', cited.start_line + i, 'line-number');
        number.setAttribute('aria-hidden', 'true');
        row.append(number, node('span', line || ' ', 'line-text'));
        pre.append(row);
      });
      if (cited.problem) {
        const notice = node('div', cited.problem + ' Rescan the project after code changes.', 'inline-notice');
        code.append(toolbar, notice, pre);
      } else code.append(toolbar, pre);
      card.append(code);
    }
  } else {
    const note = node('div', '', 'checks');
    note.append(node('p', 'Nothing to open: the model answered without settling on a definition it had read. Every request it did make is listed above.', 'subtle'));
    card.append(note);
  }
  $('answer').replaceChildren(card);
}
async function pollAsk() {
  if (!asking) return;
  let result;
  try { result = await api('ask_status'); }
  catch (error) { askFailed(error.message); return; }
  renderTrail(result.steps, !result.done);
  if (!result.done) { setTimeout(pollAsk, 900); return; }
  asking = false;
  document.body.classList.remove('busy');
  availability();
  if (result.error) return askFailed(result.error);
  if (result.answer.incomplete) return askFailed(result.answer.incomplete);
  $('status').textContent = 'Ready';
  renderAnswer(result.answer);
}
async function runAsk() {
  const question = $('question').value.trim();
  if (!state.root || busy || asking) return;
  if (!question) { $('question').focus(); return; }
  asking = true;
  availability();
  show('askError', false);
  show('askEmpty', false);
  $('answer').replaceChildren();
  document.body.classList.add('busy');
  $('status').textContent = 'Asking the model. It works through the levels one step at a time…';
  renderTrail([], true);
  view('ask');
  try {
    const started = await api('ask', {question, provider:$('provider').value, model:$('model').value.trim()});
    renderTrail(started.steps, true);
    setTimeout(pollAsk, 400);
  } catch (error) { askFailed(error.message); }
}
async function openCited(cited) {
  view('explorer');
  await runSearch(basename(cited.rel_path).replace(/\.[^.]+$/, ''));
  const match = candidates.find(c => c.path === cited.rel_path);
  // The citation authorized this file on the server, so it opens either way;
  // going through the search first is only so the results panel agrees with
  // what the reader is showing.
  await showFile(match || {path: cited.rel_path});
  const symbol = outline?.symbols.find(s => s.qualified_name === cited.qualified_name);
  if (symbol) await showSource(symbol);
}
$('askForm').onsubmit = event => { event.preventDefault(); runAsk(); };
$('askTab').onclick = () => view('ask');
$('provider').onchange = providerNote;

$('openForm').onsubmit = event => { event.preventDefault(); openProject($('projectPath').value); };
for (const id of ['projectSwitch','openProjectButton','welcomeOpen']) $(id).onclick = projectPicker;
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => $(b.dataset.close).close());
document.querySelector('.brand').onclick = event => { event.preventDefault(); view('explorer'); };
$('searchForm').onsubmit = event => { event.preventDefault(); runSearch($('query').value); };
$('query').oninput = () => show('clearQuery', !!$('query').value);
$('clearQuery').onclick = () => { $('query').value = ''; show('clearQuery',false); $('query').focus(); };
$('languageFilter').onchange = renderCandidates;
$('symbolFilter').oninput = renderSymbols;
$('explorerTab').onclick = () => view('explorer');
$('overviewTab').onclick = overview;
$('omittedTab').onclick = omitted;
$('refresh').onclick = () => work('Rescanning project…', async () => {
  state = await api('refresh'); renderState(); resetSearch(); resetAsk(); view('ask');
  $('indexTime').textContent = 'Updated ' + new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  notify('Index refreshed. Search again to see current definitions.');
});
$('themeButton').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
$('helpButton').onclick = () => $('helpDialog').showModal();
$('clearSearches').onclick = () => { if (state.root) { delete histories[state.root]; persist('searches',histories); renderSearchHistory(); } };
$('dismissError').onclick = () => show('error', false);
$('copyPath').onclick = () => copy(selectedPath, 'File path');
$('copyCode').onclick = () => source?.text && copy(source.text, 'Code');
$('copyLocation').onclick = () => source?.text && copy(source.location, 'Location');
$('copyReport').onclick = () => copy(reportText, 'Report');
function wrap(enabled) { $('sourceCode').classList.toggle('wrap',enabled); $('wrapCode').setAttribute('aria-pressed',String(enabled)); persist('wrap',enabled); }
wrap(stored('wrap', false) === true);
$('wrapCode').onclick = () => wrap($('wrapCode').getAttribute('aria-pressed') !== 'true');
document.addEventListener('keydown', event => {
  if (document.querySelector('dialog[open]') || busy) return;
  const editing = ['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName);
  if (((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') || (event.key === '/' && !editing)) {
    event.preventDefault();
    if (!state.root) projectPicker();
    else { view('explorer'); $('query').focus(); $('query').select(); }
  }
});
work('Connecting to local workspace…', async () => {
  state = await api('state'); renderState(); renderProviders(); resetSearch(); resetAsk(); view('ask');
});
