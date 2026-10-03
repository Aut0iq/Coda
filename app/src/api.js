// Запросы приложения к серверу (не к мини-аппу в WebView — тот ходит сам).

async function withTimeout(url, opts = {}, ms = 10000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try { return await fetch(url, { ...opts, signal: ctl.signal }); }
  finally { clearTimeout(timer); }
}

/** С телефона до сервера дошли? -> {ok, reason} */
export async function checkReachable(api, ms = 8000) {
  try {
    const r = await withTimeout(`${api}/healthz`, {}, ms);
    return r.ok ? { ok: true } : { ok: false, reason: `Сервер ответил ${r.status}` };
  } catch (e) {
    const m = String(e && e.message || e);
    if (/abort/i.test(m)) return { ok: false, reason: 'Сервер не отвечает (таймаут)' };
    if (/ssl|cert|trust/i.test(m)) return { ok: false, reason: 'Телефон не доверяет сертификату сервера' };
    return { ok: false, reason: 'Нет соединения с сервером' };
  }
}

/** Токен подходит? Возвращает данные /api/info или бросает ошибку. */
export async function hubInfo(api, token) {
  const r = await withTimeout(`${api}/api/info`, { headers: { Authorization: `Bearer ${token}` } });
  if (r.status === 401) throw Object.assign(new Error('Токен не подходит'), { code: 'AUTH' });
  if (!r.ok) throw new Error(`Сервер ответил ${r.status}`);
  return r.json();
}
