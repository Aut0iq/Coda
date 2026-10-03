// Помощники тестов: bash и временные каталоги, одинаково работающие на Linux и в Windows (Git Bash).
export const isWin = Deno.build.os === 'windows';
const fwd = (p) => p.replace(/\\/g, '/');

export function bashBin() {
  const forced = Deno.env.get('BASH_BIN');
  if (forced) return forced;
  if (!isWin) return 'bash';
  // bash.exe из System32 — это WSL, он не понимает пути Windows; нужен Git Bash
  for (const p of ['C:/Program Files/Git/usr/bin/bash.exe', 'C:/Program Files (x86)/Git/usr/bin/bash.exe']) {
    try { if (Deno.statSync(p).isFile) return p; } catch { /* нет такого */ }
  }
  return 'bash';
}

/** Новый временный каталог, путь с «/» — его понимают и Deno, и bash. */
export async function tempDir() {
  return fwd(await Deno.makeTempDir({ prefix: 'hub-test-' }));
}

export const toPosix = fwd;
