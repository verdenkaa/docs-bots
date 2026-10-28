import {test} from 'node:test';
import assert from 'node:assert/strict';
import {authorize} from '../scripts/auth.mjs';

const env = {DOCS_AUTH_USER: 'reader', DOCS_AUTH_PASSWORD: 'long:test-password'};
function request(path, credentials) {
  return new Request(`https://docs.example${path}`, {headers: credentials === undefined ? {} :
    {authorization: `Basic ${Buffer.from(credentials).toString('base64')}`}});
}
test('all resource types deny unauthenticated access', async () => {
  for (const path of ['/', '/assets/js/main.js', '/image.png', '/unknown']) {
    const response = await authorize(request(path), env);
    assert.equal(response.status, 401);
    assert.match(response.headers.get('www-authenticate'), /^Basic /);
    assert.equal(response.headers.get('cache-control'), 'private, no-store');
  }
});
test('correct credentials pass; incorrect and malformed credentials fail', async () => {
  assert.equal(await authorize(request('/', 'reader:long:test-password'), env), null);
  for (const value of ['reader:wrong', 'wrong:long:test-password', 'missing-colon']) {
    assert.equal((await authorize(request('/', value), env)).status, 401);
  }
  const malformed = new Request('https://docs.example', {headers: {authorization: 'Basic !!!'}});
  assert.equal((await authorize(malformed, env)).status, 401);
});
test('missing secrets fail closed', async () => {
  for (const config of [{}, {DOCS_AUTH_USER: 'reader'}, {DOCS_AUTH_PASSWORD: 'secret'}]) {
    assert.equal((await authorize(request('/'), config)).status, 503);
  }
});
