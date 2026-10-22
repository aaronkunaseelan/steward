const slugOK = s => s.length <= 80 && /^[a-z0-9]+(-[a-z0-9]+)*$/.test(s);
const cap = 10 * 1024 * 1024;
const device = ua => /ipad/i.test(ua) ? 'ipad' : /iphone/i.test(ua) ? 'iphone' : /android/i.test(ua) ? 'android' : /macintosh|mac os/i.test(ua) ? 'mac' : 'other';
async function authorized(request, token) {
  // Hash both sides to fixed-length buffers before constant-time comparison.
  const hash = s => crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  const [a, b] = await Promise.all([hash(request.headers.get('Authorization') || ''), hash('Bearer ' + (token || ''))]);
  let diff = 0;
  const aa = new Uint8Array(a), bb = new Uint8Array(b);
  for (let i = 0; i < aa.length; i++) diff |= aa[i] ^ bb[i];
  return !!token && diff === 0;
}
const encoder = new TextEncoder();
const sessionAge = 2592000;
const sessionKey = secret => crypto.subtle.importKey('raw', encoder.encode(secret), {name:'HMAC', hash:'SHA-256'}, false, ['sign', 'verify']);
async function passwordMatches(input, expected) {
  const hash = s => crypto.subtle.digest('SHA-256', encoder.encode(s));
  const [a, b] = await Promise.all([hash(input), hash(expected)]);
  // Fixed-size digest comparison, independent of password length and matching prefix.
  let diff = 0;
  const aa = new Uint8Array(a), bb = new Uint8Array(b);
  for (let i = 0; i < aa.length; i++) diff |= aa[i] ^ bb[i];
  return diff === 0;
}
async function signedSession(secret) {
  const payload = `${Math.floor(Date.now()/1000) + sessionAge}.${crypto.randomUUID()}`;
  const signature = await crypto.subtle.sign('HMAC', await sessionKey(secret), encoder.encode(payload));
  return payload + '.' + Array.from(new Uint8Array(signature), b => b.toString(16).padStart(2, '0')).join('');
}
async function authenticated(request, secret) {
  const value = request.headers.get('cookie')?.match(/(?:^|;\s*)steward_session=([^;]+)/)?.[1] || '';
  const parts = value.split('.');
  if (parts.length !== 3 || !/^\d+$/.test(parts[0]) || Number(parts[0]) <= Date.now()/1000 || Number(parts[0]) > Date.now()/1000 + sessionAge || !/^[\da-f-]{36}$/.test(parts[1]) || !/^[\da-f]{64}$/.test(parts[2])) return false;
  return crypto.subtle.verify('HMAC', await sessionKey(secret), Uint8Array.from(parts[2].match(/../g), x => parseInt(x, 16)), encoder.encode(`${parts[0]}.${parts[1]}`));
}
function loginPage(slug, bad = false, head = false) {
  // slug is validated before interpolation.
  return new Response(head ? null : `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Steward · sign in</title><h1>Steward</h1><form method="post" action="/login"><input type="hidden" name="slug" value="${slug}"><label>Password <input name="password" type="password" autocomplete="current-password" required autofocus></label><button>Open page</button>${bad ? '<p role="alert">That password did not match.</p>' : ''}</form></html>`, {
    status: bad ? 401 : 200,
    headers: {'Content-Type':'text/html; charset=utf-8', 'Cache-Control':'private, no-store', 'X-Robots-Tag':'noindex', 'Referrer-Policy':'same-origin', 'X-Frame-Options':'DENY', 'X-Content-Type-Options':'nosniff', 'Content-Security-Policy':"default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}
  });
}
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url), path = url.pathname, bucket = env.PAGES;
    const reply = (body, status = 200) => new Response(body, {status});
    if (path.startsWith('/_api/')) {
      if (path !== '/_api/opens' && !(path.startsWith('/_api/pages/') && slugOK(path.slice('/_api/pages/'.length)))) return reply(null, 404);
      if (!await authorized(request, env.STEWARD_PAGES_TOKEN)) return reply(null, 401);
      if (path === '/_api/opens' && request.method === 'GET') {
        const n = Number(url.searchParams.get('limit') || 1000);
        if (!Number.isInteger(n) || n < 1 || n > 1000) return reply(null, 400);
        const after = url.searchParams.get('after') || '';
        if (after && !after.startsWith('_opens/')) return reply(null, 400);
        const list = await bucket.list({prefix: '_opens/', startAfter: after, limit: n});
        const keys = list.objects.map(o => o.key);
        return Response.json({keys, next: list.truncated ? keys.at(-1) : null});
      }
      const slug = path.slice('/_api/pages/'.length);
      if (!path.startsWith('/_api/pages/') || !slugOK(slug)) return reply(null, 404);
      if (request.method === 'PUT') {
        if (Number(request.headers.get('Content-Length')) > cap) return reply(null, 413);
        const reader = request.body?.getReader(), chunks = []; let size = 0;
        if (reader) while (true) {
          const {done, value} = await reader.read(); if (done) break;
          size += value.length; if (size > cap) { await reader.cancel(); return reply(null, 413); }
          chunks.push(value);
        }
        await bucket.put(slug, new Blob(chunks), {httpMetadata: {contentType: request.headers.get('Content-Type') || 'application/octet-stream'}, customMetadata: {public: request.headers.get('X-Steward-Public') === '1' ? '1' : '0'}});
        return reply(null, 204);
      }
      if (request.method === 'GET') {
        const object = await bucket.get(slug);
        if (!object) return reply(null, 404);
        return new Response(object.body, {headers: {'Content-Type': object.httpMetadata?.contentType || 'application/octet-stream', 'Cache-Control': 'no-store'}});
      }
      if (request.method === 'DELETE') {
        await bucket.delete(slug);
        let cursor;
        do {
          const list = await bucket.list({prefix: `_opens/${slug}/`, cursor});
          await Promise.all(list.objects.map(o => bucket.delete(o.key)));
          cursor = list.truncated ? list.cursor : undefined;
        } while (cursor);
        return reply(null, 204);
      }
      return reply(null, 405);
    }
    if (!env.PAGE_PASSWORD || !env.SESSION_SECRET) return reply('Not configured', 503);
    if (path === '/login' && request.method === 'POST') {
      if (request.headers.get('origin') !== url.origin) return reply(null, 403);
      let form;
      try { form = await request.formData(); } catch { return reply(null, 400); }
      const slug = form.get('slug'), password = form.get('password');
      if (typeof slug !== 'string' || !slugOK(slug)) return reply(null, 400);
      if (typeof password !== 'string' || password.length > 256 || !await passwordMatches(password, env.PAGE_PASSWORD)) return loginPage(slug, true);
      return new Response(null, {status:303, headers:{Location:'/' + slug, 'Cache-Control':'no-store', 'Set-Cookie':`steward_session=${await signedSession(env.SESSION_SECRET)}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${sessionAge}`}});
    }
    const slug = path.slice(1);
    if (!slugOK(slug) || path.startsWith('/_')) return reply(null, 404);
    if (!['HEAD', 'GET'].includes(request.method)) return reply(null, 405);
    const object = await bucket[request.method === 'HEAD' ? 'head' : 'get'](slug);
    if (object?.customMetadata?.public !== '1' && !await authenticated(request, env.SESSION_SECRET)) return loginPage(slug, false, request.method === 'HEAD');
    if (!object) return reply(null, 404);
    const type = object.httpMetadata?.contentType || 'application/octet-stream';
    const headers = {'Content-Type': type, 'Cache-Control': 'private, no-store', 'X-Robots-Tag': 'noindex', 'Referrer-Policy': 'no-referrer'};
    if (type.split(';')[0].trim().toLowerCase() === 'text/html') headers['Content-Security-Policy'] = "default-src 'none'; img-src data:; style-src 'unsafe-inline'";
    if (request.method === 'GET') {
      const at = new Date().toISOString();
      // Extra fractional digits prevent simultaneous opens from sharing a marker key.
      const keyTime = at.replace('Z', crypto.getRandomValues(new Uint32Array(1))[0].toString().padStart(10, '0') + 'Z');
      ctx.waitUntil(bucket.put(`_opens/${slug}/${keyTime}`, JSON.stringify({at, device: device(request.headers.get('User-Agent') || '')})));
    }
    return new Response(request.method === 'HEAD' ? null : object.body, {headers});
  }
};
