// Плашка «запреты» для экрана проверки перед установкой (до того, как на сервере появится API).
// После установки то же самое показывает мини-апп по данным /api/netcheck (webapp/ui.js) —
// формулировки держим одинаковыми.

export const ZAPRET_URL = 'https://github.com/bol-van/zapret';

export const ADVICE_TEXT = {
  vpn: {
    title: 'VPN на сервере',
    text: 'Весь трафик сервера пойдёт через другую страну — самый надёжный вариант. '
        + 'Настрой так, чтобы SSH-доступ к серверу не оборвался.',
  },
  zapret: {
    title: 'zapret',
    text: 'Обход блокировок прямо на сервере, без VPN. Помогает от замедления YouTube, '
        + 'но не от блокировки по IP.',
    url: ZAPRET_URL,
    urlLabel: 'Инструкция',
  },
};

const BAD = (v) => v !== undefined && v !== 'ok';
const DOMAINS = [
  ['youtube', 'YouTube не открывается с этого сервера'],
  ['deezer', 'Deezer не открывается — без него не работают поиск и теги'],
  ['soundcloud', 'SoundCloud не открывается — запасной источник недоступен'],
];

/**
 * kv — разобранный вывод preflight.sh. Возвращает модель плашки или null.
 * level: blocked — что-то нужное не открывается; info — сервер в РФ, но всё открылось.
 */
export function restrictionBanner(kv) {
  if (!kv || kv.net_youtube === undefined) return null;   // curl не было — проверить нечем
  const ru = kv.country === 'RU';
  const problems = DOMAINS.filter(([id]) => BAD(kv[`net_${id}`])).map(([, text]) => text);
  const registries = ['registry', 'ghcr', 'getdocker'].filter((id) => BAD(kv[`net_${id}`]));
  const installRisk = registries.length >= 2 && kv.docker === 'none';
  if (installRisk) {
    problems.push('Реестры Docker и скрипт установки недоступны — Docker и образы могут не скачаться');
  }

  if (!problems.length) {
    if (!ru) return null;
    return {
      level: 'info', ru, ip: kv.ip || '',
      title: 'Сервер находится в России',
      intro: 'Сейчас всё открывается, но YouTube здесь часто замедляют, и скачивание может стать очень '
           + 'медленным. Скорость проверю после установки; если понадобится, приложение подскажет '
           + 'про VPN или zapret.',
      problems: [], advice: [],
    };
  }
  return {
    level: 'blocked', ru, ip: kv.ip || '',
    title: 'Сервер не достаёт до нужных сайтов',
    intro: ru ? `Сервер находится в России${kv.ip ? ` (${kv.ip})` : ''}, а здесь YouTube и часть сервисов ограничены.` : '',
    problems,
    advice: ru ? ['vpn', 'zapret'] : ['vpn'],
  };
}

/** Что показать в чек-листе перед установкой: [{ok:true|false|'warn', text}]. */
export function checklist(kv) {
  const rows = [];
  const add = (ok, text) => rows.push({ ok, text });
  add(kv.kernel === 'Linux', `Система: ${kv.os || '?'} ${kv.os_version || ''} (${kv.arch || '?'})`.trim());
  if (!['x86_64', 'aarch64', 'arm64'].includes(kv.arch)) add(false, `Процессор ${kv.arch} не поддерживается`);
  const mem = Number(kv.mem_mb || 0);
  add(mem >= 1000 ? true : mem >= 600 ? 'warn' : false, `Память: ${mem} МБ${mem < 1000 ? ' — маловато, будет медленно' : ''}`);
  const disk = Number(kv.disk_free_mb || 0);
  add(disk >= 4096 ? true : disk >= 2048 ? 'warn' : false, `Диск: свободно ${(disk / 1024).toFixed(1)} ГБ`);
  add(kv.docker !== 'none' ? true : 'warn', kv.docker !== 'none' ? `Docker ${kv.docker}` : 'Docker не установлен — поставлю сам');
  if (kv.docker !== 'none' && kv.compose === 'none') add('warn', 'Нет docker compose — поставлю плагин');
  if (kv.installed === 'yes') add('warn', 'Coda уже установлен — настройки и музыка сохранятся, обновлю');
  return rows;
}

/** Порты 80/443 для HTTPS: {httpsPossible, busy:[…]} */
export function portsInfo(kv) {
  const busy = [];
  if (kv.installed !== 'yes') {
    if ((kv.port80 || '').startsWith('busy')) busy.push(`80 (${kv.port80.slice(5) || 'занят'})`);
    if ((kv.port443 || '').startsWith('busy')) busy.push(`443 (${kv.port443.slice(5) || 'занят'})`);
  }
  // при обновлении порт 8080 занят нашим же Caddy — это не помеха
  return { httpsPossible: busy.length === 0, busy, port8080Busy: kv.installed !== 'yes' && (kv.port8080 || '').startsWith('busy') };
}
