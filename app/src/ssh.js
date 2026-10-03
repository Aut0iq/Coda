// Обёртка над нативным модулем HubSsh (modules/hub-ssh). Один сеанс — много команд.
import { requireOptionalNativeModule } from 'expo-modules-core';

const Native = requireOptionalNativeModule('HubSsh');
export const sshAvailable = !!Native;

let seq = 0;
const nextId = (p) => `${p}${++seq}`;

export async function open({ host, port = 22, user, password, fingerprint = null }) {
  if (!Native) throw new Error('SSH недоступен в этой сборке приложения');
  const id = nextId('s');
  const r = await Native.open(id, host, Number(port) || 22, user, password, fingerprint);
  return { id, fingerprint: r.fingerprint };
}

/** Выполняет команду, строки вывода (stdout+stderr) отдаёт в onLine. -> код выхода */
export async function exec(session, command, { stdin = null, onLine } = {}) {
  const execId = nextId('e');
  const sub = Native.addListener('line', (e) => { if (e.execId === execId && onLine) onLine(e.text); });
  try {
    // stderr сливаем в stdout: установщик и docker пишут в оба потока
    return await Native.exec(session.id, execId, `(${command}) 2>&1`, stdin);
  } finally {
    sub.remove();
  }
}

export async function close(session) {
  try { await Native?.close(session.id); } catch { /* уже закрыт */ }
}

/** Сообщение JSch/сети → то, что можно показать человеку. */
export function friendlySshError(err) {
  const m = String((err && err.message) || err || '');
  if (/auth fail|authentication/i.test(m)) return 'Неверный логин или пароль (или вход по паролю отключён на сервере)';
  if (/timeout|timed out/i.test(m)) return 'Сервер не отвечает. Проверь IP и порт SSH, и что сервер включён';
  if (/refused/i.test(m)) return 'Сервер отказал в соединении: порт SSH закрыт или указан не тот';
  if (/unknownhost|unresolved|resolve/i.test(m)) return 'Не нашёл такой адрес. Проверь, как записан IP или домен';
  if (/unreachable|no route/i.test(m)) return 'До сервера не добраться по сети';
  if (/HOSTKEY_CHANGED/.test(m)) return 'Ключ сервера изменился с прошлого подключения. Если ты не переустанавливал систему, '
    + 'это может быть подмена — не продолжай';
  if (/algorithm|kex|negotiat/i.test(m)) return 'Не удалось договориться о шифровании с сервером (слишком старый или строгий SSH)';
  return m || 'Не удалось подключиться по SSH';
}
