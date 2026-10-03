// Кладёт серверную часть (../server) в src/bundle.generated.js: приложение заливает эти файлы
// на сервер по SSH. Запуск: `npm run bundle` (в CI — автоматически, см. postinstall).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const server = path.resolve(here, '../../server');
const out = path.resolve(here, '../src/bundle.generated.js');

const files = {};
// CRLF (git на Windows) ломает bash на сервере — приводим к LF
const add = (rel, abs) => { files[rel] = fs.readFileSync(abs, 'utf8').replace(/\r\n/g, '\n'); };

// установщик и compose — в корень пакета
for (const f of ['install.sh', 'uninstall.sh', 'preflight.sh', 'docker-compose.yml', 'docker-compose.remote.yml', 'Caddyfile', 'Caddyfile.remote', 'VERSION']) {
  add(f, path.join(server, 'deploy', f));
}
// исходники API — для сборки образа на месте, если готового образа не скачать
const api = path.join(server, 'api');
const skip = new Set(['tests', '__pycache__', '.pytest_cache']);
(function walk(dir, rel) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (skip.has(e.name) || e.name.endsWith('.pyc')) continue;
    const abs = path.join(dir, e.name);
    const r = rel ? `${rel}/${e.name}` : e.name;
    if (e.isDirectory()) walk(abs, r);
    else add(`api/${r}`, abs);
  }
})(api, '');

const names = Object.keys(files).sort();
const sorted = Object.fromEntries(names.map((n) => [n, files[n]]));
const bytes = names.reduce((n, k) => n + Buffer.byteLength(files[k]), 0);
fs.writeFileSync(out, `// СГЕНЕРИРОВАНО scripts/make-bundle.mjs — не править руками\nexport default ${JSON.stringify({ version: files.VERSION.trim(), files: sorted }, null, 1)};\n`);
console.log(`bundle: ${names.length} файлов, ${(bytes / 1024).toFixed(0)} КБ → ${path.relative(process.cwd(), out)}`);
