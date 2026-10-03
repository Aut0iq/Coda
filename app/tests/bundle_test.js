// Весь настоящий пакет → скрипт загрузки → bash → сравнение файлов с исходниками.
// deno run --no-prompt --allow-read --allow-write --allow-run --allow-env tests/bundle_test.js
import { bashBin, tempDir } from './_sh.js';
import bundle from '../src/bundle.generated.js';
import { buildUploadScript } from '../src/protocol.js';

const T = await tempDir();
const DIR = `${T}/hub-bundle-roundtrip`;
const script = buildUploadScript(bundle.files, DIR);
// как на сервере: скрипт приходит в stdin у `bash -s`
const p = new Deno.Command(bashBin(), { args: ['-s'], stdin: 'piped', stdout: 'piped', stderr: 'piped' }).spawn();
const w = p.stdin.getWriter(); await w.write(new TextEncoder().encode(script)); await w.close();
const out = await p.output();
let bad = 0;
if (out.code !== 0) { bad++; console.error('✗ bash завершился с', out.code, new TextDecoder().decode(out.stderr)); }
for (const [path, content] of Object.entries(bundle.files)) {
  let got = null;
  try { got = await Deno.readTextFile(`${DIR}/${path}`); } catch { /* нет файла */ }
  if (got !== content) { bad++; console.error('✗ не совпал', path); }
}
const n = Object.keys(bundle.files).length;
for (const must of ['install.sh', 'preflight.sh', 'docker-compose.yml', 'Caddyfile', 'api/Dockerfile', 'api/main.py', 'api/webapp/app.js']) {
  if (!(must in bundle.files)) { bad++; console.error('✗ в пакете нет', must); }
}
for (const tests of Object.keys(bundle.files).filter((k) => k.includes('/tests/') || k.endsWith('.pyc'))) { bad++; console.error('✗ в пакете лишнее', tests); }
// install.sh делает `. /etc/os-release`, а там своя VERSION: наша не должна называться так же
// (из-за этого в ::result попадала версия Ubuntu вместо версии Music Hub)
if (/^VERSION=|\$\{?VERSION\b/m.test(bundle.files['install.sh'])) { bad++; console.error('✗ install.sh использует переменную VERSION — её затирает /etc/os-release'); }
// установщик сам задаёт umask 022 (при umask 077 на сервере файлы пакета станут 600 от root и контейнер их не прочитает)
if (!/^umask 022$/m.test(bundle.files['install.sh'])) { bad++; console.error('✗ в install.sh нет umask 022'); }
// секреты в пакет попасть не должны
for (const [path, c] of Object.entries(bundle.files)) {
  if (/BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|TELEGRAM_BOT_TOKEN\s*=\s*\d/.test(c)) { bad++; console.error('✗ похоже на секрет в', path); }
}
const run = await new Deno.Command(bashBin(), { args: ['-n', `${DIR}/install.sh`] }).output();
if (run.code !== 0) { bad++; console.error('✗ install.sh после заливки не проходит bash -n'); }
await Deno.remove(T, { recursive: true });
if (bad) { console.error(`провалено: ${bad}`); Deno.exit(1); }
console.log(`✓ ${n} файлов доехали без искажений, install.sh цел, секретов и тестов в пакете нет`);
