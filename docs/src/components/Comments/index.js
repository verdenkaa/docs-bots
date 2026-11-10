import React, {useEffect, useRef, useState} from 'react';
import {createPortal} from 'react-dom';
import {useLocation} from '@docusaurus/router';
import styles from './styles.module.css';

const commentId = id => `discussion-${id}`;
const fragment = id => `#${encodeURIComponent(id)}`;
const pagePath = path => decodeURIComponent(path).replace(/\/+$/, '') || '/';
const jsonHeaders = {'Content-Type': 'application/json'};
const smallButton = 'button button--secondary button--sm';

function resolveAnchor(value, page, headings) {
  if (!value.trim()) return null;
  const url = new URL(value.trim(), window.location.href);
  const anchor = decodeURIComponent(url.hash.slice(1).split(':~:')[0]);
  if (!/^https?:$/.test(url.protocol) || pagePath(url.pathname) !== pagePath(page) ||
      !headings.some(heading => heading.id === anchor)) {
    throw new Error('Укажите ссылку на заголовок этой страницы, например #имена-модулей.');
  }
  return anchor;
}

async function requestComments(endpoint, options) {
  const response = await fetch(endpoint, options);
  if (!response.headers.get('content-type')?.includes('application/json')) {
    throw new Error(response.status === 401
      ? 'Обновите страницу, чтобы войти в документацию.'
      : 'API комментариев недоступен. Локально используйте Vercel Dev.');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  return data;
}

function CommentForm({onSubmit, headings, onCancel, editText}) {
  const editing = editText !== undefined;
  const [name, setName] = useState('');
  const [text, setText] = useState(editText ?? '');
  const [link, setLink] = useState('');
  const [error, setError] = useState('');
  const [sending, setSending] = useState(false);
  const label = editing ? 'Текст сообщения' : onCancel ? 'Ответ' : 'Комментарий';
  const submitLabel = editing ? 'Сохранить' : onCancel ? 'Отправить ответ' : 'Оставить комментарий';

  async function submit(event) {
    event.preventDefault();
    setSending(true);
    setError('');
    try {
      await onSubmit({name, text, link});
      setText('');
      setLink('');
    } catch (cause) {
      setError(cause.message);
    } finally {
      setSending(false);
    }
  }

  return (
    <form onSubmit={submit} className={styles.form}>
      <fieldset disabled={sending} className={styles.fields}>
        {!editing && <label>Имя <input required maxLength={80} autoComplete="name" value={name}
          onChange={event => setName(event.target.value)} /></label>}
        <label>{label}
          <textarea required maxLength={3000} rows={3} value={text}
            onChange={event => setText(event.target.value)} />
        </label>
        {headings && (
          <label>Ссылка на заголовок (необязательно)
            <input list="discussion-headings" maxLength={2000} placeholder="#имена-модулей"
              value={link} onChange={event => setLink(event.target.value)} />
            <datalist id="discussion-headings">
              {headings.map(({id, label}) => <option key={id} value={`#${id}`} label={label} />)}
            </datalist>
          </label>
        )}
        {error && <p role="alert" className={styles.error}>{error}</p>}
        <div className={styles.actions}>
          <button className="button button--primary" type="submit">
            {sending ? 'Сохраняем…' : submitLabel}
          </button>
          {onCancel && <button className="button button--secondary" type="button"
            onClick={onCancel}>Отмена</button>}
        </div>
      </fieldset>
    </form>
  );
}

function Comment({comment, heading, onEdit, onRemove, onResolve}) {
  const {id, name, text, anchor, createdAt, editedAt, resolved} = comment;
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function run(action) {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div id={commentId(id)} className={styles.comment}>
      <div className={styles.meta}>
        <strong>{name}</strong>
        <time dateTime={createdAt}>{new Date(createdAt).toLocaleString('ru-RU', {
          dateStyle: 'short', timeStyle: 'short',
        })}</time>
        {editedAt && <span>изменено</span>}
        {resolved && <strong className={styles.resolved}>Решено</strong>}
        <a href={fragment(commentId(id))} aria-label="Прямая ссылка на сообщение">#</a>
        {heading && <a href={fragment(heading.id)}>К разделу «{heading.label}»</a>}
        {anchor && !heading && <span>Якорь больше недоступен · #{anchor}</span>}
      </div>
      {editing ? (
        <CommentForm editText={text} onCancel={() => setEditing(false)} onSubmit={async draft => {
          await onEdit(id, draft.text);
          setEditing(false);
        }} />
      ) : (
        <>
          <p className={styles.text}>{text}</p>
          <div className={styles.actions}>
            <button type="button" className={smallButton} disabled={busy}
              onClick={() => { setError(''); setEditing(true); }}>Редактировать</button>
            <button type="button" className={smallButton} disabled={busy}
              onClick={() => {
                const prompt = comment.parentId ? 'Удалить этот ответ?' : 'Удалить комментарий вместе со всеми ответами?';
                if (window.confirm(prompt)) run(() => onRemove(id));
              }}>Удалить</button>
            {onResolve && <button type="button" className={smallButton} disabled={busy}
              onClick={() => run(() => onResolve(id, !resolved))}>
              {resolved ? 'Открыть снова' : 'Отметить решённым'}
            </button>}
          </div>
          {error && <p role="alert" className={styles.error}>{error}</p>}
        </>
      )}
    </div>
  );
}

function HeadingDiscussion({threads}) {
  const [open, setOpen] = useState(false);
  return (
    <span className={styles.marker} onKeyDown={event => {
      if (event.key === 'Escape') {
        event.currentTarget.querySelector('button').focus();
        setOpen(false);
      }
    }}>
      <button type="button" aria-expanded={open} className={styles.badge}
        onClick={() => setOpen(!open)}>
        Ветки <span>{threads.length}</span>
      </button>
      {open && (
        <span className={styles.popup}>
          {threads.map(({root, replies}) => (
            <a key={root.id} href={fragment(commentId(root.id))} onClick={() => setOpen(false)}>
              <strong>{root.name}</strong> · {root.text.slice(0, 110)}
              {root.text.length > 110 && '…'}
              {root.resolved && <small>Решено</small>}
              {replies.length > 0 && <small>Ответов: {replies.length}</small>}
            </a>
          ))}
        </span>
      )}
    </span>
  );
}

export default function Comments({page}) {
  const section = useRef(null);
  const [headings, setHeadings] = useState([]);
  const [comments, setComments] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [replyTo, setReplyTo] = useState(null);
  const {hash} = useLocation();
  const endpoint = `/api/comments?page=${encodeURIComponent(pagePath(page))}`;

  useEffect(() => {
    const selector = '.theme-doc-markdown :is(h1,h2,h3,h4,h5,h6)[id]';
    const nodes = section.current.closest('article')?.querySelectorAll(selector) || [];
    const entries = Array.from(nodes, node => {
      const mount = document.createElement('span');
      const label = node.textContent.replace(/#$/, '').trim();
      node.append(mount);
      return {id: node.id, label, mount};
    });
    setHeadings(entries);
    return () => entries.forEach(({mount}) => mount.remove());
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    requestComments(endpoint, {signal: controller.signal})
      .then(data => { if (!controller.signal.aborted) setComments(data.comments); })
      .catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [endpoint, attempt]);

  const loaded = comments !== null;
  useEffect(() => {
    if (loaded && hash.startsWith('#discussion-')) {
      document.getElementById(hash.slice(1))?.scrollIntoView();
    }
  }, [loaded, hash]);

  const threads = new Map();
  for (const root of comments || []) {
    if (!root.parentId) threads.set(root.id, {root, replies: []});
  }
  for (const reply of comments || []) {
    if (reply.parentId) threads.get(reply.parentId)?.replies.push(reply);
  }
  const threadList = Array.from(threads.values());
  const byHeading = new Map();
  for (const thread of threadList) {
    const {anchor} = thread.root;
    if (!byHeading.has(anchor)) byHeading.set(anchor, []);
    byHeading.get(anchor).push(thread);
  }
  const headingById = new Map(headings.map(heading => [heading.id, heading]));

  async function post({name, text, link}, parentId = null) {
    if (!name.trim() || !text.trim()) throw new Error('Заполните имя и текст.');
    const anchor = parentId ? null : resolveAnchor(link, page, headings);
    const {comment} = await requestComments(endpoint, {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify({name, text, anchor, parentId}),
    });
    setComments(current => [...current, comment]);
    if (parentId) setReplyTo(null);
  }

  async function change(id, data) {
    const {comment} = await requestComments(`${endpoint}&id=${id}`, {
      method: 'PATCH', headers: jsonHeaders, body: JSON.stringify(data),
    });
    setComments(current => current.map(item => item.id === id ? comment : item));
  }

  async function resolve(id, resolved) {
    await change(id, {action: 'resolve', resolved});
    if (resolved) setReplyTo(current => current === id ? null : current);
  }

  async function remove(id) {
    await requestComments(`${endpoint}&id=${id}`, {method: 'DELETE'});
    setComments(current => current.filter(item => item.id !== id && item.parentId !== id));
    setReplyTo(current => current === id ? null : current);
  }

  const edit = (id, text) => change(id, {action: 'edit', text});

  return (
    <section ref={section} className={styles.section} aria-labelledby="page-discussion">
      <h2 id="page-discussion">Обсуждение страницы</h2>
      {error && (
        <p role="status">{error}{' '}
          <button type="button" className="button button--secondary button--sm"
            onClick={() => setAttempt(value => value + 1)}>Повторить</button>
        </p>
      )}
      {!loaded && !error && <p role="status">Загружаем комментарии…</p>}
      {loaded && (
        <>
          {threads.size === 0 && <p>Комментариев пока нет.</p>}
          {threadList.map(({root, replies}) => (
            <div className={styles.thread} key={root.id}>
              <Comment comment={root} heading={headingById.get(root.anchor)}
                onEdit={edit} onRemove={remove} onResolve={resolve} />
              {replies.length > 0 && <div className={styles.replies}>
                {replies.map(reply => <Comment key={reply.id} comment={reply} onEdit={edit} onRemove={remove} />)}
              </div>}
              {!root.resolved && (replyTo === root.id
                ? <CommentForm onSubmit={draft => post(draft, root.id)} onCancel={() => setReplyTo(null)} />
                : <button type="button" className={smallButton}
                    onClick={() => setReplyTo(root.id)}>Ответить</button>)}
            </div>
          ))}
          <CommentForm onSubmit={post} headings={headings} />
        </>
      )}
      {headings.map(heading => byHeading.has(heading.id) && createPortal(
        <HeadingDiscussion threads={byHeading.get(heading.id)} />,
        heading.mount, heading.id,
      ))}
    </section>
  );
}
