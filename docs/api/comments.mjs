import {Redis} from '@upstash/redis';
import {authorize} from '../scripts/auth.cjs';

const headers = {'Cache-Control': 'private, no-store', 'X-Robots-Tag': 'noindex, nofollow'};
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const maxComments = 200;

// Check the parent and page limit in the same Redis operation as the write.
const insertComment = `
  if ARGV[2] ~= '' then
    local parent = redis.call('HGET', KEYS[1], ARGV[2])
    if not parent then return -1 end
    local root = cjson.decode(parent)
    if root.parentId ~= cjson.null then return -1 end
    if root.resolved == true then return -2 end
  end
  if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 1 end
  if redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[4]) then return 0 end
  return redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
`;

const changeComment = `
  local key, id, action = KEYS[1], ARGV[1], ARGV[2]
  local stored = redis.call('HGET', key, id)
  if not stored then return 0 end
  local comment = cjson.decode(stored)

  if action == 'delete' then
    local ids = {id}
    if comment.parentId == cjson.null then
      local entries = redis.call('HGETALL', key)
      for i = 1, #entries, 2 do
        if cjson.decode(entries[i + 1]).parentId == id then
          table.insert(ids, entries[i])
        end
      end
    end
    redis.call('HDEL', key, unpack(ids))
    return 1
  end

  local update = cjson.decode(ARGV[3])
  if action == 'resolve' then
    if comment.parentId ~= cjson.null then return -1 end
    comment.resolved = update.resolved
  else
    comment.text = update.text
    comment.editedAt = update.editedAt
  end
  local result = cjson.encode(comment)
  redis.call('HSET', key, id, result)
  return result
`;

function json(data, status = 200, extraHeaders) {
  return Response.json(data, {status, headers: {...headers, ...extraHeaders}});
}

function error(status, message, extraHeaders) {
  return json({error: message}, status, extraHeaders);
}

function redisConfig(env) {
  if (env.UPSTASH_REDIS_REST_URL && env.UPSTASH_REDIS_REST_TOKEN) {
    return {url: env.UPSTASH_REDIS_REST_URL, token: env.UPSTASH_REDIS_REST_TOKEN};
  }
  if (env.KV_REST_API_URL && env.KV_REST_API_TOKEN) {
    return {url: env.KV_REST_API_URL, token: env.KV_REST_API_TOKEN};
  }
  return null;
}

async function readBody(request) {
  try {
    const body = await request.text();
    return body.length <= 16_384 ? JSON.parse(body) : null;
  } catch {
    return null;
  }
}

function commentText(data) {
  const text = typeof data?.text === 'string' ? data.text.trim() : '';
  return text.length > 0 && text.length <= 3000 ? text : null;
}

function newComment(data) {
  const name = typeof data?.name === 'string' ? data.name.trim() : '';
  const text = commentText(data);
  if (!name || name.length > 80 || !text) return null;
  const {anchor = null, parentId = null} = data;
  if (anchor !== null && (typeof anchor !== 'string' || !anchor ||
    anchor.length > 240 || /[#\s\u0000-\u001f]/u.test(anchor) || anchor.startsWith('discussion-'))) {
    return null;
  }
  if (parentId !== null && (typeof parentId !== 'string' || !uuid.test(parentId) || anchor !== null)) {
    return null;
  }
  return {id: crypto.randomUUID(), name, text, anchor, parentId, createdAt: new Date().toISOString()};
}

async function mutate(redis, key, id, method, data) {
  if (!uuid.test(id || '')) return error(400, 'Неверный идентификатор комментария.');
  const action = method === 'DELETE' ? 'delete' : data?.action;
  let update = {};
  if (method === 'PATCH') {
    const text = commentText(data);
    if (action === 'edit' && text) {
      update = {text, editedAt: new Date().toISOString()};
    } else if (action === 'resolve' && typeof data.resolved === 'boolean') {
      update = {resolved: data.resolved};
    } else {
      return error(400, 'Проверьте текст или статус обсуждения.');
    }
  }
  const result = await redis.eval(changeComment, [key], [id, action, JSON.stringify(update)]);
  if (result === 0) return error(404, 'Комментарий уже удалён или находится на другой странице.');
  if (result === -1) return error(409, 'Статус можно изменить только у всей ветки.');
  return method === 'DELETE' ? json({deleted: true}) : json({comment: result});
}

async function handleComments(request) {
  const env = process.env;
  const denied = await authorize(request, env);
  if (denied) return denied;

  const {method} = request;
  const methods = ['GET', 'POST', 'PATCH', 'DELETE'];
  if (!methods.includes(method)) {
    return error(405, 'Метод не поддерживается.', {Allow: methods.join(', ')});
  }
  const url = new URL(request.url);
  const requestedPage = url.searchParams.get('page') || '';
  const page = requestedPage === '/' ? '/' : requestedPage.replace(/\/+$/, '');
  if (page.length > 300 || !/^\/(?:[\p{L}\p{N}_-]+(?:\/[\p{L}\p{N}_-]+)*)?$/u.test(page)) {
    return error(400, 'Неверный адрес документа.');
  }
  const config = redisConfig(env);
  if (!config) return error(503, 'Хранилище комментариев ещё не настроено.');
  const scope = env.VERCEL_ENV || 'development';
  // The team sync previously occupied the home route. Keep its existing discussion.
  const discussionPage = page === '/faq/drafts/architecture-team-sync' ? '/' : page;
  const key = `docs-bots:comments:v1:${scope}:${discussionPage}`;
  const redis = new Redis({...config, signal: () => AbortSignal.timeout(5000)});

  try {
    if (method === 'GET') {
      const comments = Object.values(await redis.hgetall(key) || {}).sort(
        (a, b) => a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id),
      );
      return json({comments});
    }
    if (request.headers.get('origin') !== url.origin) {
      return error(403, 'Выполните действие со страницы документации.');
    }
    const id = url.searchParams.get('id');
    if (method === 'DELETE') return await mutate(redis, key, id, method);
    if (request.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') {
      return error(415, 'Ожидается JSON.');
    }
    const data = await readBody(request);
    if (method === 'PATCH') return await mutate(redis, key, id, method, data);
    const comment = newComment(data);
    if (!comment) return error(400, 'Проверьте имя, текст и ссылку на заголовок.');

    const inserted = await redis.eval(
      insertComment, [key], [comment.id, comment.parentId || '', JSON.stringify(comment), maxComments],
    );
    if (inserted === -1) return error(409, 'Ветка обсуждения недоступна на этой странице.');
    if (inserted === -2) return error(409, 'Ветка закрыта. Откройте её снова, чтобы ответить.');
    if (inserted === 0) return error(409, 'Достигнут лимит комментариев на этой странице.');
    return json({comment}, 201);
  } catch (cause) {
    console.error('Comments storage unavailable:', cause?.name);
    return error(503, 'Хранилище комментариев временно недоступно.');
  }
}

export default {fetch: handleComments};
