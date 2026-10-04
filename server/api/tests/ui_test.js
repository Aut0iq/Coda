// Запуск: deno run --no-prompt --allow-read tests/ui_test.js  (из server/api)
const src = await Deno.readTextFile(new URL('../webapp/ui.js', import.meta.url));
const load = (href) => new Function('location', src + '; return UI;')({ href });
const UI = load('https://h.example/mb/app/');

let failed = 0;
const ok = (cond, name) => { if (!cond) { failed++; console.error('✗', name); } else console.log('✓', name); };
const chk = (id, state, extra = {}) => ({ id, name: id, state, ms: 5, detail: '', ...extra });
const okChecks = [chk('youtube', 'ok'), chk('youtube_speed', 'ok', { kbit: 9000 }), chk('deezer', 'ok'), chk('soundcloud', 'ok')];

ok(UI.netBanner(null) === '', 'нет данных — нет плашки');
ok(UI.netBanner({ level: 'ok', checks: okChecks, advice: [] }) === '', 'level ok — нет плашки');

const ru = UI.netBanner({
  level: 'blocked', ru: true, ip: '1.2.3.4', advice: ['vpn', 'zapret'],
  checks: [chk('youtube', 'reset'), chk('youtube_speed', 'reset'), chk('deezer', 'ok'), chk('soundcloud', 'ok')],
});
ok(ru.includes('в России (1.2.3.4)'), 'РФ: сказано, что сервер в России');
ok(ru.includes('VPN на сервере') && ru.includes('zapret.'), 'РФ: оба совета — VPN и zapret');
ok(ru.includes('data-ext="https://github.com/bol-van/zapret"'), 'zapret: ссылка идёт через оболочку (data-ext)');
ok(!ru.includes('href='), 'в плашке нет прямых href (WebView увёл бы со страницы)');
ok((ru.match(/YouTube/g) || []).length >= 1 && !ru.includes('слишком медленно'), 'YouTube reset');
ok(UI.netProblems({ checks: [chk('youtube', 'reset'), chk('youtube_speed', 'reset')] }).length === 1,
   'одна проблема YouTube не дублируется строкой про скорость');
ok(ru.includes('data-nb="recheck"') && ru.includes('data-nb="hide"'), 'кнопки «проверить снова» и «скрыть»');

const slow = UI.netBanner({
  level: 'limited', ru: true, ip: '', advice: ['vpn', 'zapret'],
  checks: [chk('youtube', 'ok'), chk('youtube_speed', 'slow', { kbit: 120 }), chk('deezer', 'ok'), chk('soundcloud', 'ok')],
});
ok(slow.includes('120 кбит/с') && slow.includes('замедление'), 'замедление: показана скорость');
ok(slow.includes('Скачивание может работать плохо'), 'limited: мягкий заголовок');

const bot = UI.netBanner({
  level: 'limited', ru: false, ip: '5.6.7.8', advice: ['cookies', 'proxy'],
  checks: [chk('youtube', 'ok'), chk('youtube_speed', 'bot_check'), chk('deezer', 'ok'), chk('soundcloud', 'ok')],
});
ok(!bot.includes('России') && !bot.includes('zapret.'), 'не РФ: ни России, ни zapret');
ok(bot.includes('принимает сервер за бота') && bot.includes('Cookies аккаунта') && bot.includes('Прокси'), 'бот-проверка: cookies и прокси');

const evil = UI.netBanner({ level: 'blocked', ru: true, ip: '<img src=x onerror=1>', advice: ['vpn'], checks: [chk('deezer', 'http')] });
ok(!evil.includes('<img src=x') && evil.includes('&lt;img'), 'IP экранируется');
ok(evil.includes('Deezer не открывается'), 'Deezer: отдельная строка');

// BASE и картинки: приложение живёт под /mb/app/, API — под /mb/
ok(UI.BASE === '/mb/', 'BASE=/mb/ под префиксом');
ok(UI.img('https://cdn-images.dzcdn.net/a.jpg') === '/mb/api/img?u=https%3A%2F%2Fcdn-images.dzcdn.net%2Fa.jpg', 'img() с префиксом');
ok(load('https://h.example/app/').BASE === '/', 'BASE=/ без префикса');
ok(UI.img('') === '', 'пустая обложка');

// прокси: любое число ключей proxy, proxy2, proxy3…
const via = { proxy: ['a'], proxy2: ['b', 'c'], proxy3: ['d'], soundcloud: ['x'] };
const active = UI.activeCard({ id: '1', album_name: 'A', album_artist: 'B', total: 5, done: 4, failed: 0, via, started: 0 });
ok(active.includes('4 через прокси'), 'activeCard: сумма по всем proxy*');
const job = UI.jobSheet({ id: '1', album_name: 'A', album_artist: 'B', status: 'done', done: 4, total: 5, via });
ok(job.includes('Скачано через прокси') && job.includes('>4<'), 'jobSheet: прокси суммируются');
ok(!UI.activeCard({ id: '1', album_name: 'A', album_artist: 'B', total: 1, via: {} }).includes('через прокси'), 'без прокси чипа нет');

// карточка релиза больше не предлагает «без сообщений» (это было про Telegram)
ok(!UI.albumSheet({ title: 't', artist: 'a', total: 1, tracks: ['x'] }).includes('dl-quiet'), 'нет «без сообщений»');

// вкладка «Ещё»
const data = {
  settings: { values: { audio_format: 'mp3', concurrency: 4, skip_dupes: true, skip_live: false, soundcloud: true,
                        track_numbers: false, playlists: true, pl_min_tracks: 5, proxies: ['socks5://a:1', 'http://"x"'] } },
  net: { running: false, checked_at: 1700000000, result: { country: 'RU', ip: '1.2.3.4', level: 'ok', advice: [], checks: okChecks } },
  cookies: { have: true, status: 'invalid', age_days: 12.5 },
  info: { version: '0.1.0', uptime: 3700, navidrome: true },
};
const more = UI.more(data);
ok(/data-set="skip_dupes" checked/.test(more) && !/data-set="skip_live" checked/.test(more), 'тумблеры отражают настройки');
ok(more.includes('data-fmt="mp3" class="is-on"') && more.includes('data-fmt="m4a" class=""'), 'формат: выбран mp3');
ok(more.includes('<b>4</b>'), 'параллельность 4');
ok(more.includes('socks5://a:1\nhttp://&quot;x&quot;'), 'прокси в textarea экранируются');
ok(more.includes('устарели') && more.includes('data-act="ck-del"'), 'cookies: устарели + кнопка удалить');
ok(more.includes('Сервер: RU · 1.2.3.4') && more.includes('9000 кбит/с'), 'сеть: страна, IP и скорость');
ok(UI.more({ ...data, cookies: { have: false } }).includes('не подключены'), 'cookies: не подключены');
ok(!UI.more({ ...data, cookies: { have: false } }).includes('ck-del'), 'без cookies нет «Удалить»');
// --- Navidrome на другом сервере ---
ok(!more.includes('Сервер Navidrome'), 'один сервер: блока «Сервер Navidrome» нет');
ok(UI.remoteSection(null) === '' && UI.remoteSection({ configured: false }) === '', 'нет настройки — нет блока');
const rm = UI.remoteSection({ configured: true, host: '203.0.113.9', port: 22, user: 'music', music_dir: '/srv/music',
  pending_files: 0, pending_bytes: 0, last: { at: 1790000000, ok: true, files: 11 } });
ok(rm.includes('music@203.0.113.9') && !rm.includes('music@203.0.113.9:22') && rm.includes('/srv/music'), 'сервер, пользователь и папка; порт 22 не показывается');
ok(rm.includes('ничего') && rm.includes('файлов: 11') && !rm.includes('rm-sync'), 'всё передано — кнопки «Передать сейчас» нет');
const rmBad = UI.remoteSection({ configured: true, host: 'h', port: 2200, user: 'u', music_dir: '/m', pending_files: 3,
  pending_bytes: 5 * 1024 * 1024, last: { at: 1790000000, ok: false, error: 'Сервер <недоступен>' } });
ok(rmBad.includes('h:2200') && rmBad.includes('3 · 5.0 МБ') && rmBad.includes('data-act="rm-sync"'), 'ждёт передачи: счётчик, размер, кнопка');
ok(rmBad.includes('Сервер &lt;недоступен&gt;') && rmBad.includes('ошибка'), 'ошибка передачи показана и экранирована');
ok(UI.more({ ...data, remote: { configured: true, host: 'h', port: 22, user: 'u', music_dir: '/m', pending_files: 0, last: {} } })
  .includes('Сервер Navidrome'), 'вкладка «Ещё» показывает блок при настройке');
ok(UI.size(0) === '0 Б' && UI.size(1536) === '1.5 КБ' && UI.size(3 * 1024 ** 3) === '3.0 ГБ', 'размеры');
ok(UI.activeCard({ id: 'a', album_name: 'A', album_artist: 'B', total: 3, done: 3, stage: 'upload', upload: { done: 1, total: 3 } })
  .includes('Передаю на сервер Navidrome: 1 из 3'), 'карточка задачи: прогресс передачи');
ok(!UI.activeCard({ id: 'a', album_name: 'A', album_artist: 'B', total: 3, done: 1, stage: 'downloading' }).includes('Передаю'),
   'при обычной загрузке строки про передачу нет');
// --- обновление загрузчика ---
ok(UI.depsSection(null) === '' && !more.includes('Загрузчик'), 'нет данных об обновлениях — нет блока');
const dp = UI.depsSection({ enabled: true, package: 'yt-dlp', running: '2026.09.01', installed: '2026.09.01', checked_at: 1790000000, error: '' });
ok(dp.includes('2026.09.01') && dp.includes('актуальная') && dp.includes('data-act="deps-check"'), 'загрузчик: версия, статус, кнопка');
ok(UI.depsSection({ enabled: true, package: 'yt-dlp', running: '2026.09.01', installed: '2026.10.01', checked_at: 1 })
  .includes('скачана 2026.10.01, включится после перезапуска'), 'загрузчик: новая версия ждёт перезапуска');
ok(UI.depsSection({ enabled: true, package: 'yt-dlp', running: '1', installed: '1', error: 'PyPI <недоступен>' })
  .includes('PyPI &lt;недоступен&gt;'), 'загрузчик: ошибка показана и экранирована');
ok(UI.netSection({ running: true, result: null }).includes('disabled'), 'идёт проверка — кнопка заблокирована');
ok(UI.netSection(null).includes('ещё не выполнялась'), 'нет данных о сети');

if (failed) { console.error(`\nпровалено: ${failed}`); Deno.exit(1); }
console.log('\nвсё хорошо');
