// deno run --no-prompt --allow-read --allow-write --allow-run --allow-env tests/protocol_test.js  (из app/)
import { bashBin, isWin, tempDir, toPosix } from './_sh.js';
import { parseLine, parseKV, shQuote, buildUploadScript, installArgs, explainError, startScript, followScript,
  isMusicDir, remoteSetupInput, remoteSetupScript } from '../src/protocol.js';
import { restrictionBanner, checklist, portsInfo } from '../src/banner.js';

let failed = 0;
const ok = (c, name) => { if (!c) { failed++; console.error('✗', name); } else console.log('✓', name); };
const eq = (a, b, name) => ok(JSON.stringify(a) === JSON.stringify(b), `${name}${JSON.stringify(a) === JSON.stringify(b) ? '' : `\n   получено ${JSON.stringify(a)}\n   ожидалось ${JSON.stringify(b)}`}`);

// --- разбор строк установщика ---
eq(parseLine('::step docker Docker'), { type: 'step', id: 'docker', label: 'Docker' }, 'step');
eq(parseLine('::step up Запускаю контейнеры'), { type: 'step', id: 'up', label: 'Запускаю контейнеры' }, 'step с пробелами');
eq(parseLine('::log Docker: 26.1.3'), { type: 'log', text: 'Docker: 26.1.3' }, 'log');
eq(parseLine('::warn TLS_PORTS Открой порты 80 и 443'), { type: 'warn', code: 'TLS_PORTS', text: 'Открой порты 80 и 443' }, 'warn');
eq(parseLine('::error PORTS_BUSY Порт 80 занят (nginx)'), { type: 'error', code: 'PORTS_BUSY', text: 'Порт 80 занят (nginx)' }, 'error');
eq(parseLine('::error UNEXPECTED'), { type: 'error', code: 'UNEXPECTED', text: '' }, 'error без текста');
eq(parseLine('::result {"ok":true,"token":"abc","tls":false}'), { type: 'result', data: { ok: true, token: 'abc', tls: false } }, 'result');
eq(parseLine('::result {сломанный json').type, 'raw', 'битый result — это raw');
eq(parseLine('Get:1 http://deb.debian.org bookworm InRelease\r'), { type: 'raw', text: 'Get:1 http://deb.debian.org bookworm InRelease' }, 'raw, \\r срезан');
eq(parseLine('::unknown x').type, 'raw', 'неизвестная команда — raw');
eq(parseLine(undefined), { type: 'raw', text: '' }, 'undefined');

// --- key=value ---
const kv = parseKV('os=debian\nport80=busy:docker-proxy\nip=1.2.3.4\nмусор\ndone=yes\n  net_youtube=ok  \na=b=c');
eq([kv.os, kv.port80, kv.ip, kv.done, kv.net_youtube, kv.a], ['debian', 'busy:docker-proxy', '1.2.3.4', 'yes', 'ok', 'b=c'], 'parseKV');

// --- shQuote ---
eq(shQuote("it's"), `'it'\\''s'`, 'shQuote с апострофом');

// --- скрипт загрузки: проверяем реальным sh ---
const files = {
  'install.sh': '#!/usr/bin/env bash\necho "$HOME" `date` $(id)\n',
  'Caddyfile': '{$HUB_ADDR} {\n\treverse_proxy api:8081\n}',          // без \n в конце
  'api/webapp/app.js': "const s = 'it\\'s'; // ${x} \\n\n",
  'api/tricky.txt': 'строка\n__HUB_EOF_1__\nещё\n',                    // похож на разделитель
  'preflight.sh': 'echo hi\n',
  'VERSION': '0.1.0\n',
};
const T = await tempDir();
const UP = `${T}/hub-test-up`;
const script = buildUploadScript(files, UP);
const tmp = toPosix(await Deno.makeTempFile({ suffix: '.sh' }));
await Deno.writeTextFile(tmp, script);
const run = await new Deno.Command(bashBin(), { args: [tmp], stdout: 'piped', stderr: 'piped' }).output();
ok(run.code === 0, 'скрипт загрузки выполнился: ' + new TextDecoder().decode(run.stderr));
let same = true;
for (const [p, c] of Object.entries(files)) {
  const got = await Deno.readTextFile(`${UP}/${p}`);
  if (got !== c) { same = false; console.error('   отличается', p, JSON.stringify(got), JSON.stringify(c)); }
}
ok(same, 'все файлы на месте байт в байт (подстановки $ ` не сработали, \\n в конце сохранён)');
if (!isWin) { // в NTFS нет бита исполнения
  const st = await Deno.stat(`${UP}/install.sh`);
  ok((st.mode & 0o111) !== 0, '*.sh исполняемые');
}
await Deno.remove(T, { recursive: true });
await Deno.remove(tmp);
let threw = false; try { buildUploadScript({ '../x': 'a' }, '/tmp/y'); } catch { threw = true; }
ok(threw, 'путь с .. отвергается');

// --- аргументы установщика ---
eq(installArgs({ host: '1.2.3.4', tls: 'auto' }), `--host '1.2.3.4' --tls auto`, 'args auto');
eq(installArgs({ host: '1.2.3.4', tls: 'off', httpPort: 8081, ndUser: 'me' }), `--host '1.2.3.4' --tls off --http-port 8081 --nd-user 'me'`, 'args off');
eq(installArgs({ host: "x'; rm -rf /", tls: 'auto' }), `--host 'x'\\''; rm -rf /' --tls auto`, 'host не может выйти из кавычек');

// --- отвязанная установка: настоящий bash ---
{
  const T2 = await tempDir();
  const text = (b) => new TextDecoder().decode(b);
  const sh = (script) => new Deno.Command(bashBin(), { args: ['-c', script], stdout: 'piped', stderr: 'piped' });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = `${T2}/install.log`, pid = `${T2}/install.pid`;
  const fake = [
    '#!/usr/bin/env bash',
    'echo "::step a Первый"; sleep 1',
    'echo "plain line"; echo "::warn W текст"; sleep 1',
    'printf "with cr\\r\\n"',
    'echo \'::result {"ok":true}\'',
    '',
  ].join('\n');
  await Deno.writeTextFile(`${T2}/fake.sh`, fake);
  const start = startScript({ installCmd: `bash ${T2}/fake.sh`, log, pidFile: pid });
  eq(text((await sh(start).output()).stdout).trim(), 'started', 'установщик запущен отвязанно');
  eq(text((await sh(start).output()).stdout).trim(), 'attached', 'пока установка идёт, вторая копия не запускается');
  const all = text((await sh(followScript(1, log)).output()).stdout).trim().split('\n');
  ok(all[0] === '@@1 ::step a Первый' && all.at(-1).startsWith('@@') && all.at(-1).endsWith(' ::exit 0'), 'журнал читается целиком, в конце ::exit 0');
  ok(all.some((l) => l === '@@2 plain line') && all.some((l) => l.includes('::result {"ok":true}')), 'строки пронумерованы, ::result на месте');
  ok(!all.join('').includes('\r'), '\\r вычищен: счёт строк не расходится с журналом');
  const from3 = text((await sh(followScript(3, log)).output()).stdout).trim().split('\n');
  ok(from3[0].startsWith('@@3 ') && from3.length === all.length - 2, 'чтение с заданной строки (после обрыва связи)');
  ok(Deno.statSync(log).size > 0, 'журнал создан');

  // читатель убит на середине (как оборвавшееся SSH-соединение) — установка не должна остановиться
  const log2 = `${T2}/install2.log`;
  await Deno.writeTextFile(`${T2}/slow.sh`, '#!/usr/bin/env bash\necho one; sleep 1; echo two; sleep 1; echo three\n');
  await sh(startScript({ installCmd: `bash ${T2}/slow.sh`, log: log2, pidFile: `${T2}/p2.pid` })).output();
  const reader = sh(followScript(1, log2)).spawn();
  await sleep(500);
  try { reader.kill('SIGKILL'); } catch { /* уже завершился */ }
  await reader.output().catch(() => {});
  const rest = text((await sh(followScript(1, log2)).output()).stdout);
  ok(/three/.test(rest) && /::exit 0/.test(rest), 'обрыв читателя не убивает установку: дошла до конца');
  const bad = text((await sh(followScript(1, `${T2}/нет-такого.log`)).output()).stdout);
  ok(/::error NO_LOG/.test(bad), 'нет журнала — понятная ошибка');

  // umask установщика не должен меняться: иначе его файлы станут 600 от root, и контейнер (uid 1000) их не прочитает
  const log3 = `${T2}/install3.log`;
  await Deno.writeTextFile(`${T2}/um.sh`, `#!/usr/bin/env bash\numask > "${T2}/umask.txt"\n`);
  await sh(`umask 022; ${startScript({ installCmd: `bash ${T2}/um.sh`, log: log3, pidFile: `${T2}/p3.pid` })}`).output();
  await sh(followScript(1, log3)).output();                       // ждём конца
  ok(/^0*22$/.test(Deno.readTextFileSync(`${T2}/umask.txt`).trim()), 'запуск отвязанно не меняет umask установщика');
  if (!isWin) ok((Deno.statSync(log3).mode & 0o077) === 0, 'журнал закрыт для остальных (600)');
  await Deno.remove(T2, { recursive: true });
}
ok(explainError('NO_LOG').title === 'Журнал установки пропал', 'NO_LOG описан');

// --- два сервера: аргументы установщика и настройка передачи ---
eq(installArgs({ host: '1.2.3.4', tls: 'auto', mode: 'remote' }), `--host '1.2.3.4' --tls auto --mode remote`, 'args: режим remote');
eq(installArgs({ host: '1.2.3.4', tls: 'auto', mode: 'local', musicDir: '/srv/music' }),
  `--host '1.2.3.4' --tls auto --mode local --music-dir '/srv/music'`, 'args: local со своей папкой');
eq(installArgs({ host: '1.2.3.4', tls: 'auto', mode: 'local' }), `--host '1.2.3.4' --tls auto --mode local`, 'args: local без своей папки');
ok(!installArgs({ host: '1.2.3.4', tls: 'auto' }).includes('--mode'), 'args: режим не указан — сервер остаётся как был');
ok(!installArgs({ host: 'h', tls: 'auto', mode: 'remote', musicDir: '/x' }).includes('--music-dir'), 'remote: своей папки здесь нет (она на другом сервере)');
ok(isMusicDir('/srv/music') && isMusicDir('/mnt/disk-1/Music_2') && !isMusicDir('music') && !isMusicDir('/a/../b')
  && !isMusicDir('/a b') && !isMusicDir("/a'b") && !isMusicDir('/a;rm') && !isMusicDir(''), 'isMusicDir');
const inp = remoteSetupInput({ host: 'h', port: '2200', user: 'u', password: 'p"a\'ss\\', musicDir: '/m', ndUrl: 'http://h:4533', ndUser: 'nu', ndPass: 'np' });
ok(inp.endsWith('\n') && inp.trim().split('\n').length === 1, 'stdin настройки — одна строка JSON');
eq(JSON.parse(inp), { host: 'h', port: 2200, user: 'u', password: 'p"a\'ss\\', music_dir: '/m', nd_url: 'http://h:4533', nd_user: 'nu', nd_pass: 'np' },
  'stdin настройки: пароль со спецсимволами доезжает без искажений');
{
  // настоящий bash с подставным docker: проверяем stdin, перезапуск при успехе и код выхода
  const T3 = await tempDir();
  const text = (b) => new TextDecoder().decode(b);
  const fakeDocker = [
    '#!/bin/sh',
    `echo "$*" >> "${T3}/calls.txt"`,
    'case "$*" in',
    '  *remote_setup.py*) cat > "' + T3 + '/stdin.txt"; if [ -f "' + T3 + '/FAIL" ]; then echo "::error REMOTE_AUTH нет"; exit 1; fi; echo \'::result {"ok":true}\'; exit 0 ;;',
    '  *"compose restart"*|*"restart api"*) exit 0 ;;',
    'esac',
    'exit 0',
    '',
  ].join('\n');
  await Deno.writeTextFile(`${T3}/docker`, fakeDocker);
  const runScript = async () => {
    const script = remoteSetupScript(T3);
    const p = new Deno.Command(bashBin(), { args: ['-c', script], stdin: 'piped', stdout: 'piped', stderr: 'piped',
      env: { PATH: `${T3}${isWin ? ';' : ':'}${Deno.env.get('PATH')}` } }).spawn();
    const w = p.stdin.getWriter(); await w.write(new TextEncoder().encode(inp)); await w.close();
    return p.output();
  };
  let r = await runScript();
  const calls = Deno.readTextFileSync(`${T3}/calls.txt`);
  ok(r.code === 0 && text(r.stdout).includes('::result'), 'настройка: скрипт отработал, ::result получен');
  ok(Deno.readTextFileSync(`${T3}/stdin.txt`) === inp, 'настройка: JSON с паролем дошёл до remote_setup.py через stdin');
  ok(!calls.includes('p"a'), 'настройка: пароль не попал в аргументы команд');
  ok(/restart api/.test(calls), 'настройка: после успеха api перезапущен');
  Deno.writeTextFileSync(`${T3}/FAIL`, '1');
  Deno.removeSync(`${T3}/calls.txt`);
  r = await runScript();
  ok(r.code === 1 && text(r.stdout).includes('::error REMOTE_AUTH'), 'настройка: код выхода и ::error передаются наружу');
  ok(!/restart/.test(Deno.readTextFileSync(`${T3}/calls.txt`)), 'настройка: при ошибке api не перезапускается');
  await Deno.remove(T3, { recursive: true });
}
ok(explainError('REMOTE_AUTH').editRemote === true && explainError('REMOTE_DIR').editRemote === true, 'ошибки передачи предлагают поправить данные сервера Navidrome');
ok(explainError('PORTS_BUSY').editRemote === undefined, 'прочие ошибки — без правки данных Navidrome');

// --- ошибки ---
ok(explainError('PORTS_BUSY').offerHttp === true, 'PORTS_BUSY предлагает HTTP');
ok(explainError('IMAGES').restriction === true, 'IMAGES = запреты');
ok(explainError('ZZZ').title === 'Ошибка установки', 'неизвестный код');

// --- плашка запретов перед установкой ---
const okKv = { country: 'DE', ip: '1.1.1.1', docker: '26.1', net_youtube: 'ok', net_deezer: 'ok', net_soundcloud: 'ok', net_registry: 'ok', net_ghcr: 'ok', net_getdocker: 'ok' };
eq(restrictionBanner(okKv), null, 'всё открыто, не РФ — плашки нет');
eq(restrictionBanner({}), null, 'нет данных — плашки нет');
const ruOk = restrictionBanner({ ...okKv, country: 'RU' });
ok(ruOk.level === 'info' && /замедля/.test(ruOk.intro) && ruOk.advice.length === 0, 'РФ, всё открыто — информационная плашка без советов');
const ruBlocked = restrictionBanner({ ...okKv, country: 'RU', ip: '5.6.7.8', net_youtube: 'reset', net_deezer: 'http' });
ok(ruBlocked.level === 'blocked' && ruBlocked.problems.length === 2, 'РФ + YouTube и Deezer закрыты');
eq(ruBlocked.advice, ['vpn', 'zapret'], 'РФ: советы VPN и zapret');
ok(ruBlocked.intro.includes('в России (5.6.7.8)'), 'РФ: в тексте IP');
const deBlocked = restrictionBanner({ ...okKv, net_youtube: 'timeout' });
eq(deBlocked.advice, ['vpn'], 'не РФ: только VPN');
const dockerBlocked = restrictionBanner({ ...okKv, country: 'RU', docker: 'none', net_registry: 'timeout', net_ghcr: 'reset' });
ok(dockerBlocked.problems.some((p) => /Docker/.test(p)), 'реестры закрыты и Docker нет — предупреждаем про установку');
ok(restrictionBanner({ ...okKv, net_registry: 'timeout', net_ghcr: 'reset' }) === null, 'реестры закрыты, но Docker уже стоит — не пугаем');

// --- чек-лист и порты ---
const rows = checklist({ kernel: 'Linux', os: 'debian', os_version: '12', arch: 'x86_64', mem_mb: '960', disk_free_mb: '6321', docker: 'none', compose: 'none', installed: 'no' });
ok(rows[0].ok === true && rows.find((r) => /Память/.test(r.text)).ok === 'warn', 'чек-лист: 960 МБ — предупреждение');
ok(rows.find((r) => /Docker/.test(r.text)).text.includes('поставлю'), 'чек-лист: Docker будет установлен');
ok(checklist({ kernel: 'Linux', arch: 'x86_64', docker: '20.10', compose: 'none' }).some((r) => /плагин/.test(r.text)), 'чек-лист: нужен плагин compose');
eq(portsInfo({ port80: 'busy:nginx', port443: 'free', installed: 'no' }), { httpsPossible: false, busy: ['80 (nginx)'], port8080Busy: false }, 'порты: 80 занят nginx');
ok(portsInfo({ port80: 'busy:docker-proxy', port443: 'busy:docker-proxy', installed: 'yes' }).httpsPossible === true, 'порты заняты нами же при обновлении — не мешают');
ok(portsInfo({ port8080: 'busy:docker-proxy', installed: 'yes' }).port8080Busy === false, 'порт 8080 занят нами же при обновлении — подсказки «занят» нет');
ok(portsInfo({ port8080: 'busy:nginx', installed: 'no' }).port8080Busy === true, 'порт 8080 занят чужим — подсказка есть');

if (failed) { console.error(`\nпровалено: ${failed}`); Deno.exit(1); }
console.log('\nвсё хорошо');
