import {accessIdentity} from './access.mjs';
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
    // People: only the canonical host, behind Cloudflare Access, whose signed token is checked here as well.
    // Old workers.dev page links move over; /_api/ above answers on any host with its bearer.
    if (!env.CANONICAL_HOST) return reply('Not configured', 503);
    if (url.hostname !== env.CANONICAL_HOST) return Response.redirect(`https://${env.CANONICAL_HOST}${path}${url.search}`, 301);
    const slug = path.slice(1);
    if (!slugOK(slug) || path.startsWith('/_')) return reply(null, 404);
    if (!['HEAD', 'GET'].includes(request.method)) return reply(null, 405);
    const object = await bucket[request.method === 'HEAD' ? 'head' : 'get'](slug);
    if (object?.customMetadata?.public !== '1' && !await accessIdentity(request, env)) return new Response(request.method === 'HEAD' ? null : 'Cloudflare Access required', {status: 403, headers: {'Cache-Control': 'private, no-store'}});
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
