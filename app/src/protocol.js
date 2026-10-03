// Разговор приложения с установщиком на сервере. Чистые функции — без React Native,
// поэтому проверяются обычным deno-тестом (tests/protocol_test.js).

/**
 * Строка вывода установщика → событие.
 *   ::step id подпись | ::log текст | ::warn КОД текст | ::error КОД текст | ::result {json}
 * Всё остальное (вывод apt, docker pull) — {type:'raw'}.
 */
export function parseLine(line) {
  const text = String(line ?? '').replace(/\r$/, '');
  if (!text.startsWith('::')) return { type: 'raw', text };
  const m = /^::(step|log|warn|error|result)(?: (.*))?$/.exec(text);
  if (!m) return { type: 'raw', text };
  const [, kind, rest = ''] = m;
  switch (kind) {
    case 'step': {
      const i = rest.indexOf(' ');
      return i < 0 ? { type: 'step', id: rest, label: rest }
                   : { type: 'step', id: rest.slice(0, i), label: rest.slice(i + 1) };
    }
    case 'log':
      return { type: 'log', text: rest };
    case 'warn':
    case 'error': {
      const i = rest.indexOf(' ');
      return i < 0 ? { type: kind, code: rest, text: '' }
                   : { type: kind, code: rest.slice(0, i), text: rest.slice(i + 1) };
    }
    case 'result':
      try { return { type: 'result', data: JSON.parse(rest) }; }
      catch { return { type: 'raw', text }; }
  }
  return { type: 'raw', text };
}

/** key=value построчно (вывод preflight.sh) → объект. */
export function parseKV(output) {
  const out = {};
  for (const line of String(output ?? '').split('\n')) {
    const m = /^([a-z0-9_]+)=(.*)$/.exec(line.trim());
    if (m) out[m[1]] = m[2];
  }
  return out;
}

/** Строка для sh в одинарных кавычках. */
export const shQuote = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;

/**
 * Скрипт, который одним потоком раскладывает файлы по каталогу на сервере:
 * mkdir + cat <<'EOF'. Передаётся по SSH в stdin, без SFTP — нативному модулю нужен
 * только exec. Кавычки у разделителя отключают подстановки: содержимое ложится как есть.
 */
export function buildUploadScript(files, dir) {
  const lines = ['set -e', `rm -rf ${shQuote(dir)}`, `mkdir -p ${shQuote(dir)}`];
  const made = new Set([dir]);
  let n = 0;
  for (const [path, content] of Object.entries(files)) {
    if (path.startsWith('/') || path.split('/').includes('..')) throw new Error(`плохой путь: ${path}`);
    const full = `${dir}/${path}`;
    const parent = full.slice(0, full.lastIndexOf('/'));
    if (!made.has(parent)) { lines.push(`mkdir -p ${shQuote(parent)}`); made.add(parent); }
    let tag;
    do { tag = `__HUB_EOF_${++n}__`; } while (content.includes(tag));
    // heredoc кладёт перевод строки в конец: файл без него получил бы лишний
    const body = content.endsWith('\n') ? content.slice(0, -1) : content;
    lines.push(`cat > ${shQuote(full)} <<'${tag}'`, body, tag);
    if (!content.endsWith('\n')) lines.push(`truncate -s -1 ${shQuote(full)}`);
  }
  lines.push(`chmod +x ${shQuote(dir)}/*.sh`, 'echo ::log bundle ok');
  return lines.join('\n') + '\n';
}

/**
 * Аргументы install.sh из выбора пользователя.
 * mode: 'local' — Navidrome ставится на этот же сервер (musicDir — своя папка для музыки, необязательно);
 *       'remote' — здесь только скачивание, музыка уезжает на другой сервер; не указан — сервер остаётся в том
 *       режиме, в котором был установлен.
 */
export function installArgs({ host, tls, httpPort, ndUser, mode, musicDir }) {
  const a = ['--host', shQuote(host), '--tls', tls === 'off' ? 'off' : 'auto'];
  if (tls === 'off') a.push('--http-port', String(httpPort || 8080));
  if (ndUser) a.push('--nd-user', shQuote(ndUser));
  if (mode === 'remote') a.push('--mode', 'remote');
  else if (mode === 'local') {
    a.push('--mode', 'local');
    if (musicDir) a.push('--music-dir', shQuote(musicDir));
  }
  return a.join(' ');
}

/** Папка с музыкой на сервере: полный путь из безопасных символов (то же правило проверяет install.sh). */
export const isMusicDir = (p) => /^\/[A-Za-z0-9._/@+-]+$/.test(p) && !/(^|\/)\.\.(\/|$)/.test(p);

/**
 * Скрипт (для `bash -c`, от root), который настраивает передачу музыки на другой сервер: запускает в
 * контейнере api remote_setup.py (параметры — JSON в stdin, пароль не попадает в аргументы и списки процессов),
 * а при успехе перезапускает api — адрес Navidrome движок читает при старте — и ждёт, пока он поднимется.
 */
export function remoteSetupScript(hubDir = '/opt/music-hub') {
  return [
    `cd ${shQuote(hubDir)} || exit 9`,
    'docker compose exec -T api python remote_setup.py',
    'rc=$?',
    'if [ $rc = 0 ]; then',
    '  docker compose restart api >/dev/null 2>&1',
    '  for i in $(seq 1 40); do',
    '    docker compose exec -T api python -c "import urllib.request as u; u.urlopen(\'http://127.0.0.1:8081/healthz\', timeout=3)" >/dev/null 2>&1 && break',
    '    sleep 1',
    '  done',
    'fi',
    'exit $rc',
  ].join('\n');
}

/** Одна строка JSON для stdin remote_setup.py. */
export function remoteSetupInput(r) {
  return JSON.stringify({
    host: r.host, port: Number(r.port) || 22, user: r.user, password: r.password, music_dir: r.musicDir,
    nd_url: r.ndUrl || '', nd_user: r.ndUser || '', nd_pass: r.ndPass || '',
  }) + '\n';
}

export const INSTALL_LOG = '/tmp/music-hub-install.log';
export const INSTALL_PID = '/tmp/music-hub-install.pid';

/**
 * Скрипт запуска установщика ОТВЯЗАННО от SSH-сеанса (для `bash -c`, от root): телефон уснул, связь
 * оборвалась — установка на сервере идёт дальше. Вывод ложится в журнал (права 600: в нём ::result с
 * токеном), последняя строка журнала — `::exit <код>`. Выводит `started`; если установка уже идёт —
 * `attached` и второй копии не запускает (повторное нажатие «Повторить» просто подключится к ней).
 */
export function startScript({ installCmd, log = INSTALL_LOG, pidFile = INSTALL_PID }) {
  const inner = `echo $$ > ${shQuote(pidFile)}; ${installCmd}; echo "::exit $?"`;
  return [
    `LOG=${shQuote(log)}`,
    `PIDF=${shQuote(pidFile)}`,
    `if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null && ! grep -q '^::exit ' "$LOG" 2>/dev/null; then echo attached; exit 0; fi`,
    '( umask 077; : > "$LOG" )',   // закрытым делаем только журнал: umask установщика не трогаем, иначе его файлы станут 600 от root
    'S=""; command -v setsid >/dev/null 2>&1 && S=setsid',
    `$S nohup bash -c ${shQuote(inner)} >> "$LOG" 2>&1 < /dev/null &`,
    'echo $! > "$PIDF"',          // сразу, не дожидаясь внутреннего echo $$: иначе быстрый повторный запуск не увидит pid
    'echo started',
  ].join('\n');
}

/**
 * Скрипт чтения журнала установки, начиная со строки `from` (для `bash -c`, от root). Каждая строка
 * приходит как `@@<номер> <текст>`: по номеру приложение после обрыва связи продолжает с того же места,
 * а посторонний шум (предупреждения sudo) без префикса не сбивает счёт. Завершается, когда в журнале
 * появилась строка `::exit`, и все строки до неё выданы.
 */
export function followScript(from, log = INSTALL_LOG) {
  return [
    `LOG=${shQuote(log)}`,
    `n=${Math.max(1, Number(from) | 0)}`,
    'while :; do',
    '  [ -f "$LOG" ] || { echo "::error NO_LOG Журнал установки не найден на сервере"; exit 3; }',
    '  fin=0; grep -q \'^::exit \' "$LOG" && fin=1',          // до подсчёта: строки до ::exit уже все в файле
    '  total=$(wc -l < "$LOG" | tr -d \' \')',
    '  if [ "$total" -ge "$n" ]; then',
    '    sed -n "${n},${total}p" "$LOG" | tr -d \'\\r\' | awk -v s="$n" \'{ printf "@@%d %s\\n", s + NR - 1, $0 }\'',
    '    n=$((total + 1))',
    '  fi',
    '  [ "$fin" = 1 ] && exit 0',
    '  sleep 1',
    'done',
  ].join('\n');
}

/** Понятный текст для кода ошибки установщика + что предложить. */
export function explainError(code, text = '') {
  const known = {
    NOT_ROOT: { title: 'Нужен root', hint: 'Войди под root или под пользователем с правом sudo.' },
    ARCH: { title: 'Процессор не поддерживается', hint: 'Нужен сервер x86_64 или ARM64.' },
    OS: { title: 'Система не поддерживается', hint: 'Нужен Linux.' },
    NO_CURL: { title: 'Не поставился curl', hint: 'Поставь curl на сервере вручную и повтори.' },
    DOCKER_INSTALL: { title: 'Docker не поставился', hint: 'Поставь Docker вручную и повтори установку.' },
    DOCKER_START: { title: 'Docker не запускается', hint: 'Проверь на сервере: systemctl status docker.' },
    COMPOSE_INSTALL: { title: 'Не поставился docker compose', hint: 'Поставь плагин compose вручную и повтори.' },
    PORTS_BUSY: { title: 'Порты 80 и 443 заняты', hint: 'Для HTTPS они нужны. Можно поставить без HTTPS на другом порту.', offerHttp: true },
    PORT_BUSY: { title: 'Порт занят', hint: 'Выбери другой порт.' },
    IMAGES: { title: 'Образы Docker не скачиваются', hint: 'Похоже на блокировку. Нужен VPN на сервере или zapret.', restriction: true },
    BUILD: { title: 'Не получилось собрать образ', hint: 'Нет доступа к реестрам и пакетам. Нужен VPN на сервере или zapret.', restriction: true },
    UP: { title: 'Контейнеры не запустились', hint: 'Журнал выше покажет причину.' },
    NAVIDROME_START: { title: 'Navidrome не запустился', hint: 'Посмотри журнал: docker compose logs navidrome.' },
    BAD_ARGS: { title: 'Неверные параметры', hint: '' },
    API_START: { title: 'Сервис скачивания не запустился', hint: 'Журнал выше покажет причину: docker compose logs api.' },
    MUSIC_OWNER: { title: 'Папка с музыкой принадлежит другому пользователю', hint: 'Выполни на сервере: chown -R 1000:1000 <папка>.' },
    REMOTE_ARGS: { title: 'Проверь данные сервера Navidrome', hint: '', editRemote: true },
    REMOTE_AUTH: { title: 'Сервер Navidrome не принял логин или пароль', hint: 'Проверь логин и пароль SSH. Вход по паролю там должен быть разрешён хотя бы на время настройки.', editRemote: true },
    REMOTE_CONN: { title: 'Сервер скачивания не достучался до сервера Navidrome', hint: 'Сервер скачивания сам подключается к тому серверу по SSH: проверь адрес, порт SSH и файрвол на нём.', editRemote: true },
    REMOTE_HOSTKEY: { title: 'Ключ сервера Navidrome изменился', hint: 'Если систему там не переустанавливали, это может быть подмена.', editRemote: true },
    REMOTE_DIR: { title: 'Нет доступа к папке с музыкой на сервере Navidrome', hint: 'Выбери папку, в которую этот пользователь может писать, или войди под пользователем с доступом к ней.', editRemote: true },
    REMOTE_SFTP: { title: 'На сервере Navidrome выключен SFTP', hint: 'Музыка передаётся по SFTP: включи подсистему sftp в sshd_config.', editRemote: true },
    REMOTE_ND_AUTH: { title: 'Navidrome не принял логин или пароль', hint: 'Это логин и пароль самого Navidrome (не SSH).', editRemote: true },
    REMOTE_NOKEY: { title: 'Нет ключа для сервера Navidrome', hint: 'Настрой передачу заново.', editRemote: true },
    REMOTE_UNEXPECTED: { title: 'Не удалось настроить передачу на сервер Navidrome', hint: 'Скопируй журнал и покажи разработчику.', editRemote: true },
    NO_LOG: { title: 'Журнал установки пропал', hint: 'Запусти установку ещё раз: она продолжится с того же места.' },
    UNEXPECTED: { title: 'Установщик остановился', hint: 'Скопируй журнал и покажи разработчику.' },
  };
  const k = known[code] || { title: 'Ошибка установки', hint: '' };
  return { code, ...k, detail: text };
}
