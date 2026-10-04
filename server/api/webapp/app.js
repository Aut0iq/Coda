/* Music Hub: состояние, сеть, навигация. Отрисовка — в ui.js.
   Работает в WebView мобильного приложения (токен вкладывает оболочка) и в обычном браузере. */
const $ = (s) => document.querySelector(s);
const tap = () => { try { navigator.vibrate?.(8); } catch (e) {} };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const EMPTY_HINT = 'Начни с имени исполнителя. Найду альбомы через Deezer, скачаю в Navidrome.\n'
  + 'Ссылку Deezer, Spotify или YouTube тоже можно вставить сюда.';
// зеркало sources.is_link на стороне бота
const LINK_RE = /(open\.spotify\.com|spotify:|deezer\.com\/|deezer\.page\.link|link\.deezer\.com|youtube\.com\/|youtu\.be\/)/i;

const state = {
  meta: null,
  status: null,
  history: [],
  hrev: -1,
  revision: -1,
  artist: null,
  albums: null,
  filter: 'all',
  sheet: null,          // 'album' | 'job'
  tab: 'search',
  net: null,            // ответ /api/netcheck
  more: null,           // данные вкладки «Ещё»
};

/* --------------------------- оболочка приложения --------------------------- */
const shell = window.ReactNativeWebView || null;
const toShell = (msg) => { try { shell?.postMessage(JSON.stringify(msg)); } catch (e) {} };

function readToken() {
  // для отладки из обычного браузера: …/app/#token=…  (запоминаем на вкладку и стираем из адреса)
  const m = /[#&]token=([^&]+)/.exec(location.hash);
  if (m) {
    try { sessionStorage.setItem('hub_token', decodeURIComponent(m[1])); } catch (e) {}
    history.replaceState(null, '', location.pathname + location.search);
  }
  try { return window.HUB_TOKEN || sessionStorage.getItem('hub_token') || ''; }
  catch (e) { return window.HUB_TOKEN || ''; }
}
const TOKEN = readToken();

const lightMq = window.matchMedia?.('(prefers-color-scheme: light)');
function applyTheme() { document.body.classList.toggle('theme-light', !!lightMq?.matches); }
applyTheme();
lightMq?.addEventListener?.('change', applyTheme);

// ссылки наружу открывает оболочка (в WebView они бы увели со страницы приложения)
function openExternal(url) {
  if (shell) toShell({ type: 'open', url });
  else window.open(url, '_blank', 'noopener');
}
document.addEventListener('click', (e) => {
  const a = e.target.closest('[data-ext]');
  if (!a) return;
  e.preventDefault();
  openExternal(a.dataset.ext);
});

// оболочка спрашивает, есть ли что закрывать по кнопке «назад»
function notifyNav() { toShell({ type: 'nav', tab: state.tab, sheet: !!state.sheet }); }
window.hubBack = () => {
  if (state.sheet) { closeSheet(); return true; }
  if (state.tab !== 'search') { switchTab('search'); return true; }
  return false;
};

/* -------------------------------- сеть -------------------------------- */
async function api(path, opts = {}) {
  const res = await fetch(UI.BASE + path.replace(/^\//, ''), {
    ...opts,
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN, ...(opts.headers || {}) },
  });
  if (res.status === 401) toShell({ type: 'unauthorized' });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.status);
  return res.json();
}

function toast(text, isError) {
  const t = $('#toast');
  t.textContent = text;
  t.classList.toggle('is-err', !!isError);
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 3400);
}

/* ------------------------------- вкладки ------------------------------ */
function switchTab(name) {
  state.tab = name;
  document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('is-active', x.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('is-open', v.id === 'view-' + name));
  if (name === 'library') { $('#tab-dot').hidden = true; }
  if (name === 'more') loadMore();
  window.scrollTo({ top: 0 });
  notifyNav();
}
document.querySelectorAll('.tab').forEach((b) => {
  b.onclick = () => { tap(); switchTab(b.dataset.view); };
});

/* -------------------------------- поиск ------------------------------- */
let searchTimer;
$('#search-empty').textContent = EMPTY_HINT;

$('#q').addEventListener('input', (e) => {
  const v = e.target.value.trim();
  $('#q-clear').classList.toggle('is-on', v.length > 0);
  clearTimeout(searchTimer);
  if (v.length < 2 || LINK_RE.test(v)) return;
  searchTimer = setTimeout(() => runSearch(v), 380);
});

$('#q').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const v = e.target.value.trim();
  if (LINK_RE.test(v)) { e.target.blur(); openLink(v); }
});

$('#q-clear').onclick = () => {
  $('#q').value = '';
  $('#q-clear').classList.remove('is-on');
  $('#artists').hidden = true;
  $('#artist-head').hidden = true;
  $('#albums').innerHTML = '';
  $('#search-empty').textContent = EMPTY_HINT;
  $('#search-empty').hidden = false;
  state.artist = null;
};

$('#back-artists').onclick = () => {
  tap();
  $('#artist-head').hidden = true;
  $('#albums').innerHTML = '';
  document.querySelectorAll('.artist').forEach((a) => a.classList.remove('is-active'));
  state.artist = null;
  window.scrollTo({ top: 0 });
};

async function runSearch(q) {
  $('#search-empty').hidden = true;
  let data;
  try { data = await api('/api/search?q=' + encodeURIComponent(q)); }
  catch (err) { return toast('Поиск не прошёл: ' + err.message, true); }

  const box = $('#artists');
  box.hidden = data.artists.length === 0;
  if (!data.artists.length) {
    $('#search-empty').textContent = 'Никого не нашёл. Попробуй другое написание.';
    $('#search-empty').hidden = false;
    box.innerHTML = '';
    return;
  }
  state.found = data.artists;
  box.innerHTML = data.artists.map((a) => UI.artistCard(a, false)).join('');
  if (data.artists.length === 1) box.firstElementChild.click();
}

$('#artists').addEventListener('click', (e) => {
  const b = e.target.closest('[data-artist]');
  if (!b) return;
  tap();
  document.querySelectorAll('.artist').forEach((a) => a.classList.toggle('is-active', a === b));
  openArtist((state.found || []).find((a) => String(a.id) === b.dataset.artist));
});

async function openArtist(a) {
  if (!a) return;
  state.artist = a;
  $('#search-empty').hidden = true;
  $('#artist-head').hidden = false;
  $('#artist-name').textContent = a.name;
  $('#artist-sub').textContent = '';
  const grid = $('#albums');
  grid.innerHTML = Array.from({ length: 6 }, () => '<div class="skeleton"></div>').join('');

  let data;
  try { data = await api(`/api/artist/${a.id}/albums`); }
  catch (err) { grid.innerHTML = ''; return toast('Альбомы не загрузились: ' + err.message, true); }

  const owned = new Set(data.owned || []);
  const prog = data.progress || {};
  state.albums = data.albums;
  $('#artist-sub').textContent =
    `${data.albums.length} ${UI.plural(data.albums.length, 'релиз', 'релиза', 'релизов')}`
    + (owned.size ? ` · ${owned.size} уже в библиотеке` : '');
  grid.innerHTML = data.albums.length
    ? data.albums.map((al) => UI.albumCard(al, owned.has(String(al.id)), prog[String(al.id)])).join('')
    : '<p class="empty">У этого исполнителя нет альбомов в Deezer.</p>';
}

$('#albums').addEventListener('click', (e) => {
  const b = e.target.closest('[data-album]');
  if (b) { tap(); openAlbum(b.dataset.album); }
});

/* ------------------------------- шторка ------------------------------- */
$('#sheet').addEventListener('click', (e) => { if (e.target.dataset.close !== undefined) closeSheet(); });

function openSheet(html, kind) {
  $('#sheet-content').innerHTML = html;
  $('#sheet').hidden = false;
  state.sheet = kind;
  notifyNav();
}
function closeSheet() {
  if (state.sheet === 'confirm' && confirmBox.cancel) { const c = confirmBox.cancel; confirmBox.cancel = null; c(); }
  $('#sheet').hidden = true;
  state.sheet = null;
  notifyNav();
}

async function openAlbum(albumId) {
  openSheet('<p class="empty" style="margin:40px auto">Смотрю треклист…</p>', 'album');
  let al;
  try { al = await api('/api/album/' + albumId + '?plan=1'); }
  catch (err) { closeSheet(); return toast('Треклист не открылся: ' + err.message, true); }
  const grid = state.albums || [];
  const found = grid.find((x) => String(x.id) === String(albumId));
  if (found) al.cover_big = found.cover_big;
  showSheetFor(al, { album_id: albumId });
}

async function openLink(url) {
  openSheet('<p class="empty" style="margin:40px auto">Разбираю ссылку…</p>', 'album');
  let d;
  try { d = await api('/api/resolve', { method: 'POST', body: JSON.stringify({ url }) }); }
  catch (err) { closeSheet(); return toast('Ссылка не открылась: ' + err.message, true); }
  showSheetFor(d, { token: d.token });
}

function showSheetFor(d, base) {
  openSheet(UI.albumSheet(d), 'album');
  const go = (extra, text) => async (e) => {
    e.target.disabled = true;
    e.target.textContent = 'Отправляю…';
    await enqueue({ ...base, ...extra }, text);
    closeSheet();
  };
  $('#dl').onclick = go({}, `${d.artist} — ${d.title} в очереди`);
  $('#dl-again').onclick = go({ ignore_dupes: true }, `${d.title}: качаю всё заново`);
}

async function enqueue(body, okText) {
  tap();
  try {
    await api('/api/enqueue', { method: 'POST', body: JSON.stringify(body) });
    toast(okText);
    refresh();
  } catch (err) {
    toast('Не встало в очередь: ' + err.message, true);
  }
}

/* ------------------------- очередь и действия ------------------------- */
$('#active-wrap').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-stop]');
  if (!b) return;
  tap();
  b.disabled = true;
  await api('/api/cancel/' + b.dataset.stop, { method: 'POST' }).catch(() => {});
  refresh();
});

$('#pending').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-cancel]');
  if (!b) return;
  tap();
  await api('/api/cancel/' + b.dataset.cancel, { method: 'POST' }).catch(() => {});
  refresh();
});

$('#btn-scan').onclick = async (e) => {
  tap();
  e.target.disabled = true;
  try { toast((await api('/api/scan', { method: 'POST' })).message); }
  catch (err) { toast('Скан не запустился: ' + err.message, true); }
  e.target.disabled = false;
};

$('#btn-playlists').onclick = async (e) => {
  tap();
  e.target.disabled = true;
  try {
    await api('/api/playlists', { method: 'POST' });
    toast('Собираю плейлисты, это небыстро');
  } catch (err) { toast('Плейлисты: ' + err.message, true); }
  refresh();
};

$('#btn-cookies').onclick = () => {
  tap();
  switchTab('more');
  setTimeout(() => $('#ck-text')?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 250);
};

/* -------------------------- отрисовка статуса ------------------------- */
function renderStatus(s) {
  const a = s.active;
  $('#active-wrap').innerHTML = a ? UI.activeCard(a) : '';

  const list = $('#pending');
  list.innerHTML = s.pending.map((j, i) => UI.pendingRow(j, i + 1)).join('');
  $('#pending-title').hidden = s.pending.length === 0;
  $('#queue-empty').hidden = !!a || s.pending.length > 0;

  const n = s.pending.length + (a ? 1 : 0);
  const badge = $('#tab-badge');
  badge.hidden = n === 0;
  badge.textContent = n;

  const busy = new Set(s.busy || []);
  const pl = $('#btn-playlists');
  pl.disabled = busy.has('playlists');
  pl.innerHTML = busy.has('playlists')
    ? '<span class="spin"></span> Собираю плейлисты…'
    : '🎧 Собрать плейлисты';

  const ck = $('#btn-cookies');
  const meta = state.meta;
  if (meta) {
    const bad = meta.cookies_status === 'invalid';
    ck.className = 'btn ' + (!meta.cookies ? 'warn' : bad ? 'bad' : 'green');
    ck.textContent = !meta.cookies ? '🔞 Cookies не подключены'
      : bad ? '🔞 Cookies устарели'
      : `🔞 Cookies работают${meta.cookies_age_days != null ? ` · ${meta.cookies_age_days} дн.` : ''}`;
  }

  $('#notice').innerHTML = UI.notice(s.notice);
}

function renderLibrary() {
  // исправленные (перекачаны позже целиком) не показываем — они уже не ошибки
  const all = state.history.filter((h) => !h.fixed);
  const counts = {
    partial: all.filter((h) => h.status === 'partial').length,
    error: all.filter((h) => h.status === 'error').length,
  };
  if (state.filter !== 'all' && !counts[state.filter]) state.filter = 'all';
  $('#lib-filter').innerHTML = all.length ? UI.libFilter(counts, state.filter) : '';
  $('#tab-dot').hidden = !counts.error;
  $('#lib-actions').innerHTML = state.filter === 'all' ? '' : UI.libActions(state.filter, counts[state.filter]);

  const list = state.filter === 'all'
    ? all.filter((h) => h.status !== 'error')
    : all.filter((h) => h.status === state.filter);

  $('#stats').innerHTML = UI.stats(all.filter((h) => h.status === 'done' || h.status === 'partial'));
  $('#library-empty').hidden = all.length > 0;

  const byArtist = new Map();
  list.forEach((h) => {
    if (!byArtist.has(h.album_artist)) byArtist.set(h.album_artist, []);
    byArtist.get(h.album_artist).push(h);
  });
  $('#library').innerHTML = [...byArtist.entries()]
    .map(([name, items]) => UI.libArtist(name, items)).join('');
}

function confirmBox(text) {
  return new Promise((resolve) => {
    openSheet(`<p class="confirm-text">${UI.esc(text)}</p>
      <div class="subactions">
        <button class="btn" id="cf-no" type="button">Отмена</button>
        <button class="btn bad" id="cf-yes" type="button">Да</button>
      </div>`, 'confirm');
    const done = (ok) => { confirmBox.cancel = null; closeSheet(); resolve(ok); };
    $('#cf-yes').onclick = () => done(true);
    $('#cf-no').onclick = () => done(false);
    confirmBox.cancel = () => resolve(false);
  });
}

$('#lib-actions').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]');
  if (!b) return;
  tap();
  const what = state.filter;                     // error | partial
  const word = what === 'error' ? 'с ошибкой' : 'скачанные частично';
  if (b.dataset.act === 'retry') {
    b.disabled = true;
    b.textContent = 'Ставлю в очередь…';
    try {
      const r = await api('/api/retry_failed', { method: 'POST', body: JSON.stringify({ statuses: [what] }) });
      const extra = r.no_album ? ` · ${r.no_album} без альбома (трек или плейлист) повторить нельзя` : '';
      toast(r.queued ? `В очереди ${r.queued} ${UI.plural(r.queued, 'альбом', 'альбома', 'альбомов')}${extra}`
                     : `Повторять нечего${extra}`);
    } catch (err) { toast('Не вышло: ' + err.message, true); }
    refresh();
  } else if (b.dataset.act === 'remove') {
    const ok = await confirmBox(`Убрать из списка все альбомы ${word}? Пропадут только записи `
      + 'в мини-аппе — файлы в Navidrome останутся.');
    if (!ok) return;
    try {
      const r = await api('/api/history/remove', { method: 'POST', body: JSON.stringify({ statuses: [what] }) });
      toast(`Убрано из списка: ${r.removed}` + (r.fixed_removed ? ` (и ${r.fixed_removed} уже исправленных)` : ''));
      state.filter = 'all';
    } catch (err) { toast('Не вышло: ' + err.message, true); }
    refresh();
  }
});

$('#lib-filter').addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  tap();
  state.filter = b.dataset.filter;
  renderLibrary();
});

$('#library').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-job]');
  if (!b) return;
  tap();
  openSheet('<p class="empty" style="margin:40px auto">Открываю…</p>', 'job');
  let j;
  try { j = await api('/api/job/' + b.dataset.job); }
  catch (err) { closeSheet(); return toast('Не открылось: ' + err.message, true); }
  openSheet(UI.jobSheet(j), 'job');
  const retry = $('#job-retry');
  if (retry) retry.onclick = async () => {
    retry.disabled = true;
    retry.textContent = 'Отправляю…';
    await enqueue({ album_id: retry.dataset.album }, `${j.album_name}: повторяю`);
    closeSheet();
  };
});

/* ------------------------------ обновление ---------------------------- */
function apply(s) {
  state.revision = s.revision;
  if (s.history) { state.history = s.history; state.hrev = s.history_rev; }
  state.status = s;
  renderStatus(s);
  renderLibrary();
}

async function refresh() {
  try { apply(await api('/api/status')); } catch (e) {}
}

async function loop() {
  for (;;) {
    try {
      apply(await api(`/api/status?since=${state.revision}&hrev=${state.hrev}`));
    } catch (err) {
      await new Promise((r) => setTimeout(r, 4000));
    }
  }
}

/* ------------------------- запреты: плашка и проверка ------------------------ */
const HIDE_KEY = 'hub_nb_hide';
const HIDE_MS = 24 * 3600 * 1000;
const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };

function renderNet() {
  const r = state.net && state.net.result;
  const hiddenAt = Number(lsGet(HIDE_KEY) || 0);
  const html = r && Date.now() - hiddenAt > HIDE_MS ? UI.netBanner(r) : '';
  $('#netbanner').innerHTML = html;
  $('#netbanner').hidden = !html;
  $('#tab-net').hidden = !(r && r.level !== 'ok');
  if (state.tab === 'more' && state.more) { state.more.net = state.net; renderMore(); }
}

async function refreshNet() {
  try { state.net = await api('/api/netcheck'); renderNet(); } catch (e) {}
}

let netRunning = false;
async function runNet() {
  if (netRunning) return;
  netRunning = true;
  try {
    await api('/api/netcheck/run', { method: 'POST' });
    state.net = { ...(state.net || {}), running: true };
    renderNet();
    for (let i = 0; i < 45; i++) {          // проверка идёт до минуты
      await sleep(2000);
      state.net = await api('/api/netcheck');
      if (!state.net.running) break;
    }
  } catch (err) { toast('Проверка не запустилась: ' + err.message, true); }
  netRunning = false;
  renderNet();
}

$('#netbanner').addEventListener('click', (e) => {
  const b = e.target.closest('[data-nb]');
  if (!b) return;
  tap();
  if (b.dataset.nb === 'recheck') runNet();
  if (b.dataset.nb === 'hide') { lsSet(HIDE_KEY, String(Date.now())); renderNet(); }
});

/* ---------------------------------- «Ещё» ---------------------------------- */
async function loadMore() {
  try {
    const [settings, net, cookies, info, remote, deps] = await Promise.all([
      api('/api/settings'), api('/api/netcheck'), api('/api/cookies'), api('/api/info'),
      api('/api/remote').catch(() => ({ configured: false })), api('/api/deps').catch(() => null)]);
    state.net = net;
    state.more = { settings, net, cookies, info, remote, deps };
    renderMore();
  } catch (err) {
    $('#more').innerHTML = `<p class="empty">Не удалось загрузить: ${UI.esc(err.message)}</p>`;
  }
}

function renderMore() {
  const y = window.scrollY;
  $('#more').innerHTML = UI.more(state.more);
  window.scrollTo({ top: y });
}

async function saveSettings(patch) {
  try {
    const r = await api('/api/settings', { method: 'POST', body: JSON.stringify(patch) });
    state.more.settings.values = r.values;
    return true;
  } catch (err) {
    toast('Не сохранилось: ' + err.message, true);
    return false;
  }
}

$('#more').addEventListener('change', async (e) => {
  const t = e.target;
  if (t.matches('[data-set]')) {
    tap();
    if (!(await saveSettings({ [t.dataset.set]: t.checked }))) t.checked = !t.checked;
  } else if (t.id === 'ck-file' && t.files && t.files[0]) {
    const file = t.files[0];
    if (file.size > 1000000) return toast('Файл слишком большой для cookies', true);
    $('#ck-text').value = await file.text();
    uploadCookies();
  }
});

async function uploadCookies() {
  const text = $('#ck-text').value.trim();
  if (!text) return toast('Вставь cookies.txt или выбери файл', true);
  try {
    const r = await api('/api/cookies', { method: 'POST', body: JSON.stringify({ text }) });
    toast(r.status === 'ok' ? `✅ Cookies сохранены (${r.saved}) и работают`
      : `Сохранено ${r.saved}, но YouTube их не принял${r.why ? ': ' + r.why : ''}`, r.status !== 'ok');
    state.meta = await api('/api/meta');
    await loadMore();
  } catch (err) { toast(err.message, true); }
}

$('#more').addEventListener('click', async (e) => {
  const seg = e.target.closest('[data-fmt]');
  if (seg) {
    tap();
    if (await saveSettings({ audio_format: seg.dataset.fmt })) {
      seg.parentElement.querySelectorAll('button').forEach((x) => x.classList.toggle('is-on', x === seg));
    }
    return;
  }
  const step = e.target.closest('[data-step]');
  if (step) {
    tap();
    const cur = state.more.settings.values.concurrency;
    const next = Math.min(8, Math.max(1, cur + Number(step.dataset.step)));
    if (next !== cur && (await saveSettings({ concurrency: next }))) {
      step.parentElement.querySelector('b').textContent = next;
    }
    return;
  }
  const b = e.target.closest('[data-act]');
  if (!b) return;
  tap();
  const act = b.dataset.act;
  if (act === 'net-run') runNet();
  else if (act === 'px-save') {
    const list = $('#px-text').value.split('\n').map((x) => x.trim()).filter(Boolean);
    if (await saveSettings({ proxies: list })) { toast(list.length ? 'Прокси сохранены' : 'Прокси убраны'); refreshNet(); }
  } else if (act === 'ck-pick') $('#ck-file').click();
  else if (act === 'ck-save') uploadCookies();
  else if (act === 'deps-check') {
    b.disabled = true;
    try {
      const r = await api('/api/deps/check', { method: 'POST' });
      toast(r.error ? r.error
        : r.restarting ? `yt-dlp обновлён до ${r.installed}, сервис перезапускается`
        : r.restart_needed ? `yt-dlp ${r.installed} скачан, включится после текущей загрузки`
        : `yt-dlp ${r.running} — актуальная версия`, !!r.error);
      if (r.restarting) setTimeout(loadMore, 8000); else await loadMore();
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }
  else if (act === 'rm-test') {
    b.disabled = true;
    try {
      const r = await api('/api/remote/test', { method: 'POST' });
      toast(r.ok ? '✅ Связь с сервером Navidrome есть' : r.error, !r.ok);
      await loadMore();
    } catch (err) { toast(err.message, true); b.disabled = false; }
  } else if (act === 'rm-sync') {
    b.disabled = true;
    try {
      const r = await api('/api/remote/sync', { method: 'POST' });
      toast(r.ok ? `Передано файлов: ${r.files}` : r.error, !r.ok);
      await loadMore();
    } catch (err) { toast(err.message, true); b.disabled = false; }
  }
  else if (act === 'ck-check') {
    b.disabled = true;
    try {
      const r = await api('/api/cookies/check', { method: 'POST' });
      toast(r.status === 'ok' ? '✅ Cookies работают' : `Cookies: ${r.status}${r.why ? ' — ' + r.why : ''}`, r.status !== 'ok');
      await loadMore();
    } catch (err) { toast(err.message, true); b.disabled = false; }
  } else if (act === 'ck-del') {
    if (!(await confirmBox('Удалить cookies? Треки 18+ перестанут скачиваться.'))) return;
    try { await api('/api/cookies', { method: 'DELETE' }); state.meta = await api('/api/meta'); await loadMore(); }
    catch (err) { toast(err.message, true); }
  }
});

(async () => {
  try { state.meta = await api('/api/meta'); } catch (e) {}
  refreshNet();
  setInterval(refreshNet, 10 * 60 * 1000);
  await refresh();
  loop();
})();
