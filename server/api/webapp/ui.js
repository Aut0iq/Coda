/* Отрисовка мини-аппа: только HTML из данных, без обращений к сети.
   Все обработчики вешает app.js по data-атрибутам. */
const UI = (() => {
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // приложение открывают по адресу вида https://хост/mb/app/ — API лежит рядом, на уровень выше
  const BASE = new URL('../', location.href).pathname;
  const img = (u) => (u ? BASE + 'api/img?u=' + encodeURIComponent(u) : '');

  const plural = (n, one, few, many) => {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    return b === 1 ? one : many;
  };
  const tracks = (n) => `${n} ${plural(n, 'трек', 'трека', 'треков')}`;

  const dur = (sec) => {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec} с`;
    const m = Math.floor(sec / 60);
    return m < 60 ? `${m} мин ${String(sec % 60).padStart(2, '0')} с`
                  : `${Math.floor(m / 60)} ч ${String(m % 60).padStart(2, '0')} мин`;
  };
  const when = (ts) => {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  };
  // proxy, proxy2, proxy3… — по ключу на каждый прокси из настроек
  const proxied = (via) => Object.keys(via || {}).filter((k) => /^proxy\d*$/.test(k))
    .reduce((n, k) => n + via[k].length, 0);
  const albumType = (t) => ({ single: 'сингл', ep: 'EP', compile: 'сборник' }[t] || '');

  /* ------------------------------ поиск ------------------------------ */
  function artistCard(a, active) {
    const fans = a.fans >= 1000000 ? (a.fans / 1000000).toFixed(1) + 'M'
              : a.fans >= 1000 ? Math.round(a.fans / 1000) + 'K' : (a.fans || '');
    return `<button class="artist${active ? ' is-active' : ''}" type="button" data-artist="${esc(a.id)}">
      <img src="${img(a.picture)}" alt="" loading="lazy">
      <span>${esc(a.name)}</span><em>${esc(fans)}</em></button>`;
  }

  function albumCard(al, owned, prog) {
    const meta = [al.year, albumType(al.type), al.tracks ? tracks(al.tracks) : '']
      .filter(Boolean).join(' · ');
    let flag = '';
    if (prog && prog.total && prog.done < prog.total) {
      flag = `<span class="flag part">${prog.done} из ${prog.raw || prog.total}</span>`;
    } else if (owned) {
      flag = '<span class="flag done">скачан</span>';
    }
    return `<button class="album" type="button" data-album="${esc(al.id)}">
      <div class="cover${owned ? ' is-owned' : ''}">
        ${flag}${al.live ? '<span class="flag live">live</span>' : ''}
        <img src="${img(al.cover)}" alt="" loading="lazy">
      </div>
      <b>${esc(al.title)}</b><small>${esc(meta)}</small></button>`;
  }

  /* --------------------------- карточка релиза ------------------------ */
  const STATE_ICON = { keep: '⬇', dupe: '✓', live: '🎤' };

  function planTracks(plan) {
    if (!plan || !plan.tracks.length) return '';
    return `<ul class="tracklist">${plan.tracks.map((t) => {
      const why = t.state === 'dupe' ? (t.where ? `есть в «${esc(t.where)}»` : 'уже есть')
                : t.state === 'live' ? 'live-версия, пропущу' : '';
      const title = why
        ? `<span class="skip">${esc(t.title)}<span class="why">${why}</span></span>`
        : `<span>${esc(t.title)}</span>`;
      return `<li class="${t.state}"><span class="n">${t.nn || ''}</span>
        <span class="st">${STATE_ICON[t.state] || ''}</span>${title}</li>`;
    }).join('')}</ul>`;
  }

  function albumSheet(d) {
    const plan = d.plan;
    const meta = [d.artist, d.year, tracks(d.total)].filter(Boolean).join(' · ');
    let chips = '', cta = `Скачать ${tracks(d.total)}`, note = '';
    if (plan && !plan.unknown) {
      chips = `<div class="chips">
        ${plan.keep ? `<span class="chip warn">Скачаю ${plan.keep}</span>` : ''}
        ${plan.dupes ? `<span class="chip good">Уже есть ${plan.dupes}</span>` : ''}
        ${plan.lives ? `<span class="chip bad">Live ${plan.lives}</span>` : ''}
        ${plan.not_found.length ? `<span class="chip">Нет на Deezer ${plan.not_found.length}</span>` : ''}
      </div>`;
      cta = plan.keep ? `Скачать ${tracks(plan.keep)}` : 'Всё уже есть';
      if (plan.live_album) note = '<p class="caption">Это live-альбом — скачаю целиком, раз выбран явно.</p>';
    } else if (plan && plan.unknown) {
      note = '<p class="caption">Navidrome не ответил, что уже есть — качаю всё подряд.</p>';
    }
    const disabled = plan && !plan.unknown && !plan.keep ? ' disabled' : '';
    return `<div class="sheet-head">
        <img src="${img(d.cover_big || d.cover)}" alt="">
        <div><h2>${esc(d.title)}</h2><p>${esc(meta)}</p></div>
      </div>
      ${chips}
      <button class="cta" id="dl" type="button"${disabled}>${esc(cta)}</button>
      <div class="subactions">
        <button class="btn" id="dl-again" type="button">⟳ Всё заново</button>
      </div>
      ${note}
      ${plan ? planTracks(plan) : `<ul class="tracklist">${(d.tracks || []).map((t, i) =>
        `<li class="keep"><span class="n">${i + 1}</span><span class="st">⬇</span><span>${esc(t)}</span></li>`
      ).join('')}</ul>`}`;
  }

  /* ------------------------------ очередь ----------------------------- */
  function activeCard(a) {
    const total = a.total || 0;
    const donefail = (a.done || 0) + (a.failed || 0);
    const pct = total ? Math.round((donefail / total) * 100) : 0;
    const el = a.started ? (Date.now() / 1000 - a.started) : 0;
    const eta = donefail && total > donefail ? (el / donefail) * (total - donefail) : 0;
    const via = a.via || {};
    const skipped = (a.skipped_dupes || []).length + (a.skipped_live || []).length;
    const chips = [
      via.soundcloud ? `<span class="chip info">${via.soundcloud.length} с SoundCloud</span>` : '',
      via.cookies ? `<span class="chip warn">${via.cookies.length} через cookies</span>` : '',
      proxied(via) ? `<span class="chip pl">${proxied(via)} через прокси</span>` : '',
      skipped ? `<span class="chip good">${skipped} пропущено</span>` : '',
      a.failed ? `<span class="chip bad">${a.failed} с ошибкой</span>` : '',
    ].filter(Boolean).join('');
    return `<div class="active">
      <img src="${img(a.cover)}" alt="">
      <div>
        <h3>${esc(a.album_name)}</h3>
        <p>${esc(a.album_artist)}</p>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <p class="now"><span class="counter">${donefail} / ${total}</span>
          ${el ? ' · ' + dur(el) : ''}${eta ? ' · осталось ~' + dur(eta) : ''}</p>
        ${a.now ? `<p class="now">Сейчас: ${esc(a.now)}</p>` : ''}
        ${a.stage === 'upload' ? `<p class="now">Передаю на сервер Navidrome${a.upload ? `: ${a.upload.done} из ${a.upload.total}` : '…'}</p>` : ''}
        ${chips ? `<div class="via">${chips}</div>` : ''}
        ${a.cancel
          ? '<p class="now">Останавливаю: начатые треки доскачаю.</p>'
          : `<button class="stop" type="button" data-stop="${esc(a.id)}">⏹ Остановить</button>`}
      </div>
    </div>`;
  }

  function pendingRow(j, pos) {
    const n = j.total_raw || j.total || 0;
    return `<li class="row">
      <span class="pos">${pos}</span>
      <img src="${img(j.cover)}" alt="">
      <span><b>${esc(j.album_name)}</b><small>${esc(j.album_artist)} · ${tracks(n)}</small></span>
      <button type="button" data-cancel="${esc(j.id)}">Убрать</button></li>`;
  }

  function notice(n) {
    if (!n) return '';
    const head = { playlists: '🎧 Плейлисты', scan: '🔄 Navidrome', cookies: '🔞 Cookies', remote: '🖥 Сервер Navidrome', error: '⚠️ Ошибка' }[n.kind] || '';
    return `<div class="notice${n.kind === 'error' ? ' error' : ''}">
      ${head ? `<b>${head}</b> — ` : ''}${esc(n.text)}</div>`;
  }

  /* ----------------------------- библиотека --------------------------- */
  function stats(list) {
    const albums = list.length;
    const trk = list.reduce((n, h) => n + (h.done || 0), 0);
    const artists = new Set(list.map((h) => h.album_artist)).size;
    if (!albums) return '';
    return `<div><b>${albums}</b><span>${plural(albums, 'альбом', 'альбома', 'альбомов')}</span></div>
      <div><b>${artists}</b><span>${plural(artists, 'исполнитель', 'исполнителя', 'исполнителей')}</span></div>
      <div><b>${trk}</b><span>${plural(trk, 'трек', 'трека', 'треков')}</span></div>`;
  }

  function libFilter(counts, cur) {
    const item = (key, label, cls) =>
      `<button class="chip${cls ? ' ' + cls : ''}${cur === key ? ' is-on' : ''}" type="button"
        data-filter="${key}">${label}</button>`;
    return item('all', 'Все', '')
      + (counts.partial ? item('partial', `Частично ${counts.partial}`, 'warn') : '')
      + (counts.error ? item('error', `Ошибки ${counts.error}`, 'bad') : '');
  }

  function libArtist(name, items) {
    const trk = items.reduce((n, h) => n + (h.done || 0), 0);
    const figs = items.map((h) => {
      const cls = h.status === 'error' ? 'err' : h.status === 'partial' ? 'part' : '';
      const tail = h.status === 'error' ? ' · ошибка'
                 : h.status === 'partial' ? ` · ${h.n_failed || h.failed || 0} не скачалось` : '';
      return `<figure><button type="button" data-job="${esc(h.id)}">
        <img src="${img(h.cover)}" alt="" loading="lazy">
        <figcaption${cls ? ` class="${cls}"` : ''}>${esc(h.album_name)}${tail}</figcaption>
      </button></figure>`;
    }).join('');
    return `<div class="lib-artist">
      <h3><span>${esc(name)}</span><em>${items.length} альб. · ${trk} тр.</em></h3>
      <div class="strip">${figs}</div></div>`;
  }

  function libActions(filter, n) {
    if (!n) return '';
    return `<button class="btn warn" type="button" data-act="retry">⟳ Повторить все (${n})</button>
      <button class="btn bad" type="button" data-act="remove">🗑 Убрать из списка (${n})</button>`;
  }

  /* --------------------------- карточка задачи ------------------------ */
  function failBlock(list, cookieOnly) {
    if (!list.length) return '';
    const head = cookieOnly ? `🔞 Нужны cookies YouTube — ${list.length}`
                            : `Не скачалось — ${list.length}`;
    return `<div class="fails${cookieOnly ? ' cookie' : ''}"><h5>${head}</h5><ul>${
      list.map((l) => {
        const i = l.indexOf(' — ');
        const title = i > 0 ? l.slice(0, i) : l;
        const why = i > 0 ? l.slice(i + 3) : '';
        return `<li><b>${esc(title)}</b>${esc(why)}</li>`;
      }).join('')}</ul></div>`;
  }

  function skipList(rows, cls, icon, why) {
    if (!rows.length) return '';
    return `<ul class="tracklist">${rows.map((l) =>
      `<li class="${cls}"><span class="n"></span><span class="st">${icon}</span>
        <span class="skip">${esc(l)}<span class="why">${why}</span></span></li>`).join('')}</ul>`;
  }

  function jobSheet(j) {
    const status = { done: 'Готово', partial: 'Частично', error: 'Ошибка',
                     downloading: 'Качается', queued: 'В очереди' }[j.status] || j.status;
    const cls = j.status === 'error' ? 'bad' : j.status === 'partial' ? 'warn' : 'good';
    const took = j.started && j.finished ? dur(j.finished - j.started) : '';
    const fails = j.failed_tracks || [];
    const cookieFails = fails.filter((l) => /cookies/i.test(l));
    const otherFails = fails.filter((l) => !/cookies/i.test(l));
    const via = j.via || {};
    const kv = [
      (j.skipped_dupes || []).length ? ['Пропущено как дубли', (j.skipped_dupes || []).length, 'var(--green)'] : null,
      (j.skipped_live || []).length ? ['Live-версии', (j.skipped_live || []).length, 'var(--coral)'] : null,
      (via.soundcloud || []).length ? ['Взято с SoundCloud', via.soundcloud.length, 'var(--blue)'] : null,
      (via.cookies || []).length ? ['Через cookies 18+', via.cookies.length, 'var(--gold)'] : null,
      proxied(via) ? ['Скачано через прокси', proxied(via), 'var(--teal)'] : null,
      j.skipped_cancel ? ['Не начато после остановки', j.skipped_cancel, 'var(--muted)'] : null,
      j.playlists ? ['Плейлисты', j.playlists.replace(/^🎧\s*Плейлисты\s*—\s*/, ''), 'var(--teal)'] : null,
    ].filter(Boolean);
    return `<div class="sheet-head">
        <img src="${img(j.cover)}" alt="">
        <div><h2>${esc(j.album_name)}</h2>
          <p>${esc(j.album_artist)}${j.finished ? ' · ' + when(j.finished) : ''}${took ? ' · ' + took : ''}</p>
          <div class="chips"><span class="chip ${cls}">${esc(status)}</span>
            <span class="chip">${j.done || 0} из ${j.total || 0}</span></div>
        </div></div>
      ${j.error ? `<div class="fails"><h5>Ошибка</h5><ul><li><b>${esc(j.error)}</b></li></ul></div>` : ''}
      ${failBlock(cookieFails, true)}
      ${failBlock(otherFails, false)}
      ${kv.length ? `<div class="kv">${kv.map(([k, v, c]) =>
        `<div><span>${k}</span><b style="color:${c}">${esc(v)}</b></div>`).join('')}</div>` : ''}
      ${(j.not_found || []).length ? `<h2 class="section-title">Нет на Deezer</h2>
        ${skipList(j.not_found, 'live', '—', 'не нашлось')}` : ''}
      ${(j.skipped_dupes || []).length ? `<h2 class="section-title">Пропущенные дубли</h2>
        ${skipList(j.skipped_dupes, 'dupe', '✓', '')}` : ''}
      ${(j.skipped_live || []).length ? `<h2 class="section-title">Live-версии</h2>
        ${skipList(j.skipped_live, 'live', '🎤', '')}` : ''}
      ${j.album_id ? `<button class="cta" id="job-retry" type="button"
        data-album="${esc(j.album_id)}">⟳ Повторить недостающее</button>` : ''}`;
  }

  /* --------------------------- запреты и сеть ------------------------- */
  const ZAPRET_URL = 'https://github.com/bol-van/zapret';
  const STATE_LABEL = {
    ok: 'работает', slow: 'медленно', timeout: 'не отвечает', reset: 'соединение сброшено',
    http: 'доступ запрещён', bot_check: 'принял сервер за бота', geo: 'недоступно в стране сервера',
    error: 'ошибка',
  };

  // что именно не так — по-русски, по одной строке на проблему
  function netProblems(r) {
    const by = Object.fromEntries((r.checks || []).map((c) => [c.id, c]));
    const out = [];
    const yt = by.youtube, sp = by.youtube_speed;
    if (yt && yt.state !== 'ok') out.push('YouTube не открывается с этого сервера');
    if (sp && sp.state !== 'ok' && (!yt || yt.state === 'ok' || ['slow', 'bot_check', 'geo'].includes(sp.state))) {
      out.push({
        slow: `YouTube отдаёт данные слишком медленно${sp.kbit ? ` (${sp.kbit} кбит/с)` : ''} — похоже на замедление`,
        bot_check: 'YouTube принимает сервер за бота (адрес дата-центра)',
        geo: 'Тестовое видео недоступно в стране сервера',
      }[sp.state] || 'YouTube не отдаёт аудио');
    }
    if (by.deezer && by.deezer.state !== 'ok') out.push('Deezer не открывается — без него не работают поиск и теги');
    if (by.soundcloud && by.soundcloud.state !== 'ok') out.push('SoundCloud не открывается — запасной источник недоступен');
    return out;
  }

  const ADVICE = {
    vpn: '<b>VPN на сервере.</b> Весь трафик сервера пойдёт через другую страну — самый надёжный вариант. '
       + 'Настрой так, чтобы SSH-доступ к серверу не оборвался.',
    zapret: `<b>zapret.</b> Обход блокировок прямо на сервере, без VPN. Помогает от замедления YouTube, но не от `
          + `блокировки по IP. <a data-ext="${ZAPRET_URL}">Инструкция</a>`,
    cookies: '<b>Cookies аккаунта.</b> Загрузи их во вкладке «Ещё» — YouTube перестанет считать сервер ботом.',
    proxy: '<b>Прокси в другой стране.</b> Добавь его во вкладке «Ещё» — загрузки пойдут через него.',
  };

  // плашка вверху: только когда есть что сказать (level !== 'ok')
  function netBanner(r) {
    if (!r || r.level === 'ok') return '';
    const bad = r.level === 'blocked';
    const intro = r.ru
      ? `<p>Сервер находится в России${r.ip ? ` (${esc(r.ip)})` : ''}, а здесь YouTube и часть сервисов ограничены.</p>` : '';
    const probs = netProblems(r);
    return `<div class="nb ${bad ? 'bad' : 'warn'}">
      <b>${bad ? 'Сервер не достаёт до нужных сайтов' : 'Скачивание может работать плохо'}</b>
      ${intro}
      ${probs.length ? `<ul class="nb-probs">${probs.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
      ${(r.advice || []).length ? `<p class="nb-lead">Что можно сделать:</p>
        <ul class="nb-adv">${r.advice.map((a) => `<li>${ADVICE[a] || ''}</li>`).join('')}</ul>` : ''}
      <div class="nb-actions">
        <button type="button" data-nb="recheck">Проверить снова</button>
        <button type="button" data-nb="hide">Скрыть на сутки</button>
      </div>
    </div>`;
  }

  function netSection(snap) {
    const r = snap && snap.result;
    const head = `<h2 class="section-title">Сеть и запреты</h2>`;
    const btn = `<button class="btn" type="button" data-act="net-run"${snap && snap.running ? ' disabled' : ''}>${
      snap && snap.running ? '<span class="spin"></span> Проверяю…' : '↻ Проверить снова'}</button>`;
    if (!r) return `${head}<p class="caption left">Проверка ещё не выполнялась.</p>${btn}`;
    const rows = (r.checks || []).map((c) => {
      const cls = c.state === 'ok' ? 'ok' : (c.state === 'slow' || c.state === 'bot_check') ? 'warn' : 'bad';
      const val = c.state === 'ok' ? (c.kbit ? `${c.kbit} кбит/с` : `${c.ms} мс`) : (STATE_LABEL[c.state] || c.state);
      return `<div class="chk"><i class="dotc ${cls}"></i><span>${esc(c.name)}</span><em>${esc(val)}</em></div>`;
    }).join('');
    const where = r.country ? `Сервер: ${esc(r.country)}${r.ip ? ' · ' + esc(r.ip) : ''}` : 'Страну сервера определить не удалось';
    const note = r.proxy_ok ? '<p class="caption left">Напрямую что-то недоступно, но прокси из настроек справляется.</p>' : '';
    return `${head}<div class="card">${rows}</div>
      <p class="caption left">${where}${snap.checked_at ? ' · проверено ' + when(snap.checked_at) : ''}</p>
      ${note}${btn}`;
  }

  /* ------------------------ Navidrome на другом сервере ------------------------ */
  function size(n) {
    if (!n) return '0 Б';
    const u = ['Б', 'КБ', 'МБ', 'ГБ'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
  }

  function remoteSection(r) {
    if (!r || !r.configured) return '';
    const last = r.last || {};
    const lastTxt = !last.at ? '—' : last.ok ? `${when(last.at)} · файлов: ${last.files || 0}` : `ошибка · ${when(last.at)}`;
    const lastCol = !last.at ? 'var(--muted)' : last.ok ? 'var(--green)' : 'var(--coral)';
    const where = `${r.user}@${r.host}${r.port && r.port !== 22 ? ':' + r.port : ''}`;
    return `<h2 class="section-title">Сервер Navidrome</h2>
      <p class="caption left">Музыка скачивается здесь и по SSH уходит на тот сервер, в его папку с библиотекой.</p>
      <div class="kv">
        <div><span>Сервер</span><b>${esc(where)}</b></div>
        <div><span>Папка с музыкой</span><b>${esc(r.music_dir)}</b></div>
        <div><span>Ждёт передачи</span><b style="color:${r.pending_files ? 'var(--gold)' : 'var(--green)'}">${
          r.pending_files ? `${r.pending_files} · ${size(r.pending_bytes)}` : 'ничего'}</b></div>
        <div><span>Последняя передача</span><b style="color:${lastCol}">${esc(lastTxt)}</b></div>
      </div>
      ${last.ok === false && last.error ? `<p class="caption left" style="color:var(--coral)">${esc(last.error)}</p>` : ''}
      <div class="actions">
        <button class="btn" type="button" data-act="rm-test">↻ Проверить связь</button>
        ${r.pending_files ? '<button class="btn warn" type="button" data-act="rm-sync">Передать сейчас</button>' : ''}
      </div>`;
  }

  /* --------------------------- обновление загрузчика --------------------------- */
  function depsSection(p) {
    if (!p || !p.package) return '';
    const fresh = p.installed && p.installed !== p.running;
    const state = !p.enabled ? 'выключено'
      : p.error ? 'ошибка'
      : fresh ? `скачана ${p.installed}, включится после перезапуска`
      : p.checked_at ? `актуальная · проверено ${when(p.checked_at)}` : 'ещё не проверялось';
    const col = p.error ? 'var(--coral)' : fresh ? 'var(--gold)' : 'var(--green)';
    return `<h2 class="section-title">Загрузчик</h2>
      <p class="caption left">yt-dlp обновляется сам: проверка раз в час и перед каждой загрузкой.</p>
      <div class="kv">
        <div><span>Версия yt-dlp</span><b>${esc(p.running || '—')}</b></div>
        <div><span>Обновления</span><b style="color:${col}">${esc(state)}</b></div>
      </div>
      ${p.error ? `<p class="caption left" style="color:var(--coral)">${esc(p.error)}</p>` : ''}
      <div class="actions"><button class="btn" type="button" data-act="deps-check">↻ Проверить обновления</button></div>`;
  }

  /* -------------------------------- «Ещё» ----------------------------- */
  function more(d) {
    const v = d.settings.values;
    const sw = (key, label, hint) => `<label class="sw"><span>${label}${hint ? `<small>${hint}</small>` : ''}</span>
      <input type="checkbox" data-set="${key}"${v[key] ? ' checked' : ''}><i></i></label>`;
    const ck = d.cookies;
    const ckChip = !ck.have ? '<span class="chip warn">не подключены</span>'
      : ck.status === 'invalid' ? '<span class="chip bad">устарели</span>'
      : ck.status === 'ok' ? `<span class="chip good">работают${ck.age_days != null ? ` · ${ck.age_days} дн.` : ''}</span>`
      : '<span class="chip">подключены</span>';
    const info = d.info || {};
    return `${netSection(d.net)}

      ${remoteSection(d.remote)}

      <h2 class="section-title">Загрузка</h2>
      <div class="card">
        <div class="setrow"><span>Формат</span>
          <div class="seg">
            ${['m4a', 'mp3'].map((f) => `<button type="button" data-fmt="${f}" class="${v.audio_format === f ? 'is-on' : ''}">${f.toUpperCase()}</button>`).join('')}
          </div></div>
        <div class="setrow"><span>Треков одновременно</span>
          <div class="step"><button type="button" data-step="-1" aria-label="Меньше">−</button><b>${v.concurrency}</b>
            <button type="button" data-step="1" aria-label="Больше">+</button></div></div>
        ${sw('skip_dupes', 'Пропускать дубли', 'не качать то, что уже есть в Navidrome')}
        ${sw('skip_live', 'Пропускать live', 'в студийных альбомах и сборниках')}
        ${sw('soundcloud', 'SoundCloud как запасной источник')}
        ${sw('track_numbers', 'Номер трека в имени файла', 'в тегах он есть всегда')}
        ${sw('playlists', 'Плейлисты по исполнителям', `от ${v.pl_min_tracks} песен исполнителя`)}
      </div>

      <h2 class="section-title">Прокси</h2>
      <p class="caption left">Запасной выход в сеть: если напрямую не вышло, трек пробуется через них по очереди.
        По одному адресу в строке, например socks5://host:1080</p>
      <textarea id="px-text" rows="3" spellcheck="false" autocapitalize="off"
        placeholder="socks5://host:1080">${esc((v.proxies || []).join('\n'))}</textarea>
      <button class="btn" type="button" data-act="px-save">Сохранить прокси</button>

      <h2 class="section-title">Cookies для 18+ ${ckChip}</h2>
      <p class="caption left">Нужны только для роликов 18+. Экспорт cookies.txt с youtube.com из браузера,
        где ты вошёл в аккаунт (лучше второстепенный).</p>
      <textarea id="ck-text" rows="3" spellcheck="false" autocapitalize="off"
        placeholder="Вставь содержимое cookies.txt или выбери файл"></textarea>
      <input type="file" id="ck-file" accept=".txt,text/plain" hidden>
      <div class="actions">
        <button class="btn" type="button" data-act="ck-pick">Выбрать файл</button>
        <button class="btn warn" type="button" data-act="ck-save">Загрузить</button>
        ${ck.have ? '<button class="btn" type="button" data-act="ck-check">Проверить</button>'
                  + '<button class="btn bad" type="button" data-act="ck-del">Удалить</button>' : ''}
      </div>

      <h2 class="section-title">Сервер</h2>
      <div class="kv">
        <div><span>Версия</span><b>${esc(info.version || '—')}</b></div>
        <div><span>Работает</span><b>${info.uptime != null ? dur(info.uptime) : '—'}</b></div>
        <div><span>Navidrome</span><b style="color:${info.navidrome ? 'var(--green)' : 'var(--coral)'}">${info.navidrome ? 'подключён' : 'не настроен'}</b></div>
      </div>
      ${depsSection(d.deps)}`;
  }

  return { esc, img, dur, when, tracks, plural, artistCard, albumCard, albumSheet,
           activeCard, pendingRow, notice, stats, libFilter, libActions, libArtist, jobSheet,
           netProblems, netBanner, netSection, remoteSection, depsSection, size, more, BASE };
})();
