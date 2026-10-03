// Что приложение делает с сервером: подключиться, залить пакет, проверить, установить.
import * as ssh from './ssh';
import bundle from './bundle.generated';
import {
  INSTALL_LOG, INSTALL_PID, buildUploadScript, followScript, installArgs, parseKV, parseLine, remoteSetupInput,
  remoteSetupScript, shQuote, startScript,
} from './protocol';

const DIR = '/tmp/music-hub-bundle';

export class DeployError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

export const bundleVersion = bundle.version;

/** -> { session, fingerprint }: session передаётся в остальные функции, fingerprint запоминается для сервера. */
export async function connect({ host, port, user, password, fingerprint }) {
  try {
    const session = await ssh.open({ host, port, user, password, fingerprint });
    return { session, fingerprint: session.fingerprint };
  } catch (e) {
    const code = /HOSTKEY_CHANGED/.test(String(e && e.message)) ? 'HOSTKEY' : 'SSH';
    throw new DeployError(code, ssh.friendlySshError(e));
  }
}

/** sudo (если не root) + заливка пакета в /tmp. */
export async function prepare(session, { user, password }) {
  if (user !== 'root') {
    const code = await ssh.exec(session, `sudo -S -p '' -v`, { stdin: `${password}\n` });
    if (code !== 0) throw new DeployError('SUDO', 'У этого пользователя нет права sudo, либо пароль не подошёл');
  }
  const tail = [];
  const code = await ssh.exec(session, 'bash -s', {
    stdin: buildUploadScript(bundle.files, DIR),
    onLine: (l) => tail.push(l),
  });
  if (code !== 0) throw new DeployError('UPLOAD', `Не удалось залить файлы на сервер: ${tail.slice(-3).join(' ')}`);
}

export async function runPreflight(session) {
  let out = '';
  await ssh.exec(session, `bash ${DIR}/preflight.sh`, { onLine: (l) => { out += `${l}\n`; } });
  const kv = parseKV(out);
  if (kv.done !== 'yes') throw new DeployError('PREFLIGHT', 'Проверка сервера не завершилась');
  return kv;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const MAX_DROPS = 8;          // сколько раз за установку можно потерять связь и переподключиться

/** Новое SSH-подключение после обрыва: ~1–2 минуты попыток. Неверный пароль и смена ключа — сразу ошибка. */
async function reopen(conn) {
  for (let i = 0; i < 14; i++) {
    await sleep(Math.min(6000, 1500 + i * 500));
    try {
      return await ssh.open(conn);
    } catch (e) {
      const m = String((e && e.message) || e);
      if (/HOSTKEY_CHANGED/.test(m)) throw new DeployError('HOSTKEY', ssh.friendlySshError(e));
      if (/auth fail|authentication/i.test(m)) throw new DeployError('SSH', ssh.friendlySshError(e));
    }
  }
  throw new DeployError('SSH', 'Связь с сервером потеряна и не восстановилась. Установка на самом сервере могла '
    + 'продолжиться — нажми «Повторить»: приложение подключится к ней.');
}

/**
 * Запускает install.sh на сервере ОТВЯЗАННО от SSH-сеанса и читает его журнал. Телефон уснул или пропала
 * сеть — установка идёт дальше, приложение переподключается и продолжает читать с того же места.
 * События (шаги, журнал, предупреждения) — в onEvent; ::result сюда НЕ попадает: в нём токен и пароль,
 * они возвращаются только значением.
 *
 * ref = { current: session } — после переподключения в ref.current лежит новый сеанс.
 * opts.conn = параметры ssh.open для переподключения (хост SSH, не публичный адрес).
 */
export async function runInstall(ref, { user, password, host, tls, httpPort, ndUser, mode, musicDir, conn }, onEvent) {
  const root = user === 'root';
  const stdin = root ? null : `${password}\n`;
  const asRoot = (script) => (root ? `bash -c ${shQuote(script)}` : `sudo -S -p '' bash -c ${shQuote(script)}`);
  const args = installArgs({ host, tls, httpPort, ndUser, mode, musicDir });

  // 1. запуск (или подключение к уже идущей установке)
  const head = [];
  let code;
  for (let attempt = 0; ; attempt++) {
    try {
      head.length = 0;
      code = await ssh.exec(ref.current, asRoot(startScript({ installCmd: `bash ${DIR}/install.sh ${args}` })),
        { stdin, onLine: (l) => head.push(l) });
      break;
    } catch (e) {
      if (attempt >= 1) throw new DeployError('SSH', ssh.friendlySshError(e));
      // сеанс мог умереть, пока экран ждал нажатия «Повторить»
      onEvent({ type: 'log', text: 'Переподключаюсь к серверу…' });
      try { await ssh.close(ref.current); } catch { /* уже закрыт */ }
      ref.current = await reopen(conn);
    }
  }
  const startMode = head.map((l) => l.trim()).find((l) => l === 'started' || l === 'attached');
  if (code !== 0 || !startMode) throw new DeployError('UNEXPECTED', `Не удалось запустить установщик: ${head.slice(-2).join(' ')}`);
  if (startMode === 'attached') onEvent({ type: 'log', text: 'Установка уже идёт на сервере — подключаюсь к ней' });

  // 2. чтение журнала; строки приходят как «@@номер текст», по номеру продолжаем после обрыва
  let seen = 0;
  let result = null;
  let error = null;
  let finished = false;
  let readerFailed = false;
  let exitCode = null;
  const onLine = (raw) => {
    let text = raw;
    const m = /^@@(\d+) ([\s\S]*)$/.exec(raw);
    if (m) {
      const n = Number(m[1]);
      if (n <= seen) return;                  // эту строку уже получали до обрыва
      seen = n;
      text = m[2];
    }
    if (text.startsWith('::exit ')) { finished = true; exitCode = Number(text.slice(7)); return; }
    const ev = parseLine(text);
    if (ev.type === 'result') { result = ev.data; return; }
    if (ev.type === 'error') { error = ev; if (ev.code === 'NO_LOG') readerFailed = true; }
    onEvent(ev);
  };

  let drops = 0;
  while (!finished) {
    let failure = null;
    try {
      await ssh.exec(ref.current, asRoot(followScript(seen + 1)), { stdin, onLine });
    } catch (e) {
      failure = e;
    }
    if (finished || readerFailed) break;
    if (++drops > MAX_DROPS) throw new DeployError('SSH', ssh.friendlySshError(failure) || 'Связь с сервером постоянно обрывается');
    onEvent({ type: 'log', text: 'Связь с сервером прервалась — переподключаюсь. Установка на сервере идёт дальше…' });
    try { await ssh.close(ref.current); } catch { /* уже закрыт */ }
    ref.current = await reopen(conn);
    onEvent({ type: 'log', text: 'Связь восстановлена' });
  }

  if (result && result.ok) return result;
  if (error) throw new DeployError(error.code, error.text);
  throw new DeployError('UNEXPECTED', `Установщик завершился с кодом ${exitCode} без результата`);
}

/**
 * Настройка передачи музыки на ДРУГОЙ сервер (где Navidrome): сервер скачивания сам заходит туда по SSH.
 * Пароль того сервера уходит только в stdin команды по уже открытому SSH до сервера скачивания — не в
 * аргументы (их видно в списке процессов) и не по HTTP. Событиями идут шаги и предупреждения; ошибка —
 * DeployError с кодом REMOTE_*. Возвращает сведения без секретов.
 */
export async function setupRemote(ref, { user, password, remote, hubDir }, onEvent) {
  const root = user === 'root';
  const script = remoteSetupScript(hubDir);
  const cmd = root ? `bash -c ${shQuote(script)}` : `sudo -S -p '' bash -c ${shQuote(script)}`;
  // sudo забирает первую строку (пароль), остальное — JSON — доходит до remote_setup.py
  const stdin = (root ? '' : `${password}\n`) + remoteSetupInput(remote);
  let result = null;
  let error = null;
  let code;
  try {
    code = await ssh.exec(ref.current, cmd, {
      stdin,
      onLine: (line) => {
        const ev = parseLine(line);
        if (ev.type === 'result') { result = ev.data; return; }
        if (ev.type === 'error') error = ev;
        onEvent(ev);
      },
    });
  } catch (e) {
    throw new DeployError('SSH', ssh.friendlySshError(e));
  }
  if (result && result.ok) return result;
  if (error) throw new DeployError(error.code, error.text);
  throw new DeployError('REMOTE_UNEXPECTED', `Настройка передачи завершилась с кодом ${code} без результата`);
}

/** Журнал установки содержит ::result с токеном — после успеха его не оставляем на сервере. */
export async function dropLog(ref, { user, password }) {
  const root = user === 'root';
  const cmd = `rm -f ${INSTALL_LOG} ${INSTALL_PID}`;
  try {
    await ssh.exec(ref.current, root ? cmd : `sudo -S -p '' ${cmd}`, { stdin: root ? null : `${password}\n` });
  } catch { /* не критично: файл с правами 600 и в /tmp */ }
}

export async function cleanup(session) {
  try { await ssh.exec(session, `rm -rf ${DIR}`); } catch { /* не критично */ }
  await ssh.close(session);
}
