import {test} from 'node:test';
import assert from 'node:assert/strict';
import handler from '../api/comments.mjs';

const {fetch: comments} = handler;
const origin = 'https://docs.example';
const authorization = `Basic ${Buffer.from('reader:test-secret').toString('base64')}`;

function request({page = '/architecture/', auth = authorization, data, from = origin,
  method = data === undefined ? 'GET' : 'POST', id} = {}) {
  const url = new URL('/api/comments', origin);
  url.searchParams.set('page', page);
  if (id) url.searchParams.set('id', id);
  const headers = auth ? {authorization: auth} : {};
  if (method === 'GET') return new Request(url, {headers});
  return new Request(url, {
    method,
    headers: {...headers, origin: from, 'content-type': 'application/json'},
    body: JSON.stringify(data),
  });
}

test('protected page discussions round-trip through the Upstash REST client', async t => {
  Object.assign(process.env, {
    DOCS_AUTH_USER: 'reader',
    DOCS_AUTH_PASSWORD: 'test-secret',
    UPSTASH_REDIS_REST_URL: 'https://redis.example',
    UPSTASH_REDIS_REST_TOKEN: 'redis-secret',
    VERCEL_ENV: 'production',
  });

  const pages = new Map();
  const transport = t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(new URL(url).origin, 'https://redis.example');
    const headers = new Headers(options.headers);
    assert.equal(headers.get('authorization'), 'Bearer redis-secret');
    const encode = value => headers.get('upstash-encoding') === 'base64'
      ? Buffer.from(value).toString('base64') : value;
    function run([command, ...args]) {
      if (command === 'hgetall') {
        const result = [...pages.get(args[0]) || []].flat();
        return {result: result.map(encode)};
      }
      assert.equal(command, 'eval');
      const [, keyCount, key, id, actionOrParentId, data, limit] = args;
      assert.equal(keyCount, 1);
      const page = pages.get(key) || new Map();
      if (limit === undefined) {
        if (!page.has(id)) return {result: 0};
        const comment = JSON.parse(page.get(id));
        if (actionOrParentId === 'delete') {
          for (const [key, value] of page) {
            if (key === id || JSON.parse(value).parentId === id) page.delete(key);
          }
          return {result: 1};
        }
        if (actionOrParentId === 'resolve' && comment.parentId) return {result: -1};
        const updated = JSON.stringify({...comment, ...JSON.parse(data)});
        page.set(id, updated);
        return {result: encode(updated)};
      }
      if (actionOrParentId) {
        const parent = JSON.parse(page.get(actionOrParentId) || 'null');
        if (!parent || parent.parentId) return {result: -1};
        if (parent.resolved) return {result: -2};
      }
      page.set(id, data);
      pages.set(key, page);
      return {result: 1};
    }
    const payload = JSON.parse(options.body);
    return Response.json(new URL(url).pathname === '/pipeline' ? payload.map(run) : run(payload));
  });

  const draft = {name: ' Маша ', text: 'Уточним поток? <b>Текст</b>', anchor: 'имена-модулей'};
  assert.equal((await comments(request({auth: null}))).status, 401);
  assert.equal((await comments(request({auth: null, data: draft}))).status, 401);
  assert.equal((await comments(request({page: '//wrong/path'}))).status, 400);
  assert.equal((await comments(request({data: draft, from: 'https://other.example'}))).status, 403);
  assert.equal((await comments(request({data: {...draft, text: ' '}}))).status, 400);
  assert.equal((await comments(request({data: {...draft, anchor: 'https://external.example/#anchor'}}))).status, 400);
  assert.equal(transport.mock.callCount(), 0);

  const created = await comments(request({data: draft}));
  assert.equal(created.status, 201);
  assert.equal(created.headers.get('cache-control'), 'private, no-store');
  const {comment: root} = await created.json();
  assert.equal(root.name, 'Маша');
  assert.equal(root.text, draft.text);
  assert.equal(root.anchor, draft.anchor);

  const replyDraft = {name: 'Рома', text: 'Да', parentId: root.id};
  assert.equal((await comments(request({page: '/faq', data: replyDraft}))).status, 409);
  const response = await comments(request({data: replyDraft}));
  assert.equal(response.status, 201);
  const {comment: reply} = await response.json();
  assert.equal(reply.parentId, root.id);
  assert.equal((await comments(request({data: {...replyDraft, parentId: reply.id}}))).status, 409);
  const listed = await comments(request({page: '/architecture'}));
  assert.deepEqual((await listed.json()).comments, [root, reply].sort(
    (a, b) => a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id),
  ));
  const legacySync = await comments(request({page: '/', data: draft}));
  const {comment: syncComment} = await legacySync.json();
  const sync = await comments(request({page: '/faq/drafts/architecture-team-sync/'}));
  assert.deepEqual((await sync.json()).comments, [syncComment]);

  process.env.VERCEL_ENV = 'preview';
  assert.deepEqual((await (await comments(request())).json()).comments, []);
  process.env.VERCEL_ENV = 'production';

  const edit = {method: 'PATCH', id: reply.id, data: {action: 'edit', text: ' Обновили. '}};
  assert.equal((await comments(request({...edit, auth: null}))).status, 401);
  assert.equal((await comments(request({...edit, from: 'https://other.example'}))).status, 403);
  assert.equal((await comments(request({...edit, page: '/faq'}))).status, 404);
  assert.equal((await comments(request({...edit, data: {...edit.data, text: ' '}}))).status, 400);
  const {comment: updated} = await (await comments(request(edit))).json();
  const {editedAt, ...rest} = updated;
  assert.deepEqual(rest, {...reply, text: 'Обновили.'});
  assert.ok(editedAt);

  const resolve = {method: 'PATCH', id: root.id, data: {action: 'resolve', resolved: true}};
  assert.equal((await comments(request({...resolve, id: reply.id}))).status, 409);
  const closed = await comments(request(resolve));
  assert.equal((await closed.json()).comment.resolved, true);
  assert.equal((await comments(request({data: replyDraft}))).status, 409);
  const reopened = await comments(request({...resolve, data: {...resolve.data, resolved: false}}));
  assert.equal((await reopened.json()).comment.resolved, false);
  assert.equal((await comments(request({data: replyDraft}))).status, 201);

  assert.equal((await comments(request({method: 'DELETE', id: reply.id, auth: null}))).status, 401);
  assert.equal((await comments(request({method: 'DELETE', id: reply.id, from: 'https://other.example'}))).status, 403);
  assert.equal((await comments(request({method: 'DELETE', id: reply.id}))).status, 200);
  const {comments: remaining} = await (await comments(request())).json();
  assert.equal(remaining.length, 2);
  assert.ok(remaining.some(comment => comment.id === root.id));
  assert.ok(!remaining.some(comment => comment.id === reply.id));
  assert.equal((await comments(request({method: 'DELETE', id: root.id}))).status, 200);
  assert.deepEqual((await (await comments(request())).json()).comments, []);
});
