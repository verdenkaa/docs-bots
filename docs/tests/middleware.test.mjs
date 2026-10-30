import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {runInNewContext} from 'node:vm';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const entry = new URL('../middleware.js', import.meta.url);
const require = createRequire(entry);

test('auth dependency loads without Node experimental require(ESM) support', () => {
  const result = spawnSync(process.execPath, ['--no-experimental-require-module', '-e',
    'require(process.argv[1])', fileURLToPath(new URL('../scripts/auth.cjs', import.meta.url))],
  {encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
});

test('CommonJS-compiled middleware loads and protects pages and assets', async () => {
  // Use the Babel toolchain installed by Docusaurus to reproduce the ESM -> CJS boundary.
  const {transformSync} = require('@babel/core');
  const {code} = transformSync(readFileSync(entry, 'utf8'), {
    babelrc: false, configFile: false,
    plugins: [require.resolve('@babel/plugin-transform-modules-commonjs')],
  });
  const env = {DOCS_AUTH_USER: 'reader', DOCS_AUTH_PASSWORD: 'test-secret'};
  const exports = {};
  runInNewContext(code, {require, exports, process: {env}}, {filename: entry.pathname});
  assert.equal(exports.config.matcher, '/:path*');
  for (const path of ['/', '/assets/js/main.js', '/favicon.ico']) {
    const url = `https://docs.example${path}`;
    assert.equal((await exports.default(new Request(url))).status, 401);
    const headers = {authorization: `Basic ${Buffer.from('reader:test-secret').toString('base64')}`};
    const allowed = await exports.default(new Request(url, {headers}));
    assert.equal(allowed.headers.get('x-middleware-next'), '1');
    assert.equal(allowed.headers.get('cache-control'), 'private, no-store');
  }
  delete env.DOCS_AUTH_PASSWORD;
  assert.equal((await exports.default(new Request('https://docs.example/'))).status, 503);
});
