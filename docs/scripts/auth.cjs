async function equal(a, b) {
  const encode = new TextEncoder();
  const [left, right] = await Promise.all([a, b].map(value =>
    crypto.subtle.digest('SHA-256', encode.encode(value))));
  const x = new Uint8Array(left);
  const y = new Uint8Array(right);
  let difference = 0;
  for (let i = 0; i < x.length; i++) difference |= x[i] ^ y[i];
  return difference === 0;
}

async function authorize(request, env) {
  const headers = {'Cache-Control': 'private, no-store',
    'X-Robots-Tag': 'noindex, nofollow, noarchive'};
  if (!env.DOCS_AUTH_USER || !env.DOCS_AUTH_PASSWORD) {
    return new Response('Documentation access is not configured.', {status: 503, headers});
  }
  const header = request.headers.get('authorization') || '';
  if (/^Basic /i.test(header)) {
    try {
      const bytes = Uint8Array.from(atob(header.slice(6).trim()), c => c.charCodeAt(0));
      const credentials = new TextDecoder('utf-8', {fatal: true}).decode(bytes);
      const colon = credentials.indexOf(':');
      if (colon >= 0) {
        const userOk = await equal(credentials.slice(0, colon), env.DOCS_AUTH_USER);
        const passwordOk = await equal(credentials.slice(colon + 1), env.DOCS_AUTH_PASSWORD);
        if (userOk && passwordOk) return null;
      }
    } catch { /* Malformed credentials are denied. */ }
  }
  return new Response('Authentication required.', {status: 401, headers: {
    ...headers, 'WWW-Authenticate': 'Basic realm="Documentation", charset="UTF-8"',
  }});
}

// Vercel compiles middleware.js to CommonJS. Keep this dependency require-compatible.
exports.authorize = authorize;
