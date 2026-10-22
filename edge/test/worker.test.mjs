import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.mjs';

function fixture() {
  const objects = new Map(), pending = [];
  const env = {PAGE_PASSWORD: 'test-password', SESSION_SECRET: 'session-secret', STEWARD_PAGES_TOKEN: 'test-secret', PAGES: {
    async put(key, body, options = {}) { objects.set(key, {body: typeof body === 'string' ? body : await body.text(), httpMetadata: options.httpMetadata, customMetadata: options.customMetadata}); },
    async get(key) { return objects.get(key); },
    async head(key) { return objects.get(key); },
    async delete(key) { objects.delete(key); },
    async list({prefix, startAfter = '', limit = 1000}) {
      const keys = [...objects.keys()].sort().filter(k => k.startsWith(prefix) && k > startAfter);
      return {objects: keys.slice(0, limit).map(key => ({key})), truncated: keys.length > limit};
    }
  }};
  const request = (path, method = 'GET', body, auth = true, headers = {}) => worker.fetch(new Request('https://test' + path, {
    method, body, headers: {...(auth ? {Authorization: 'Bearer test-secret'} : {}), ...headers}
  }), env, {waitUntil(p) { pending.push(p); }});
  return {objects, pending, request, env};
}
test('auth, slugs and private paths', async () => {
  const {request} = fixture();
  assert.equal((await request('/_api/unknown', 'GET', undefined, false)).status, 404);
  assert.equal((await request('/_api/pages/BAD', 'PUT', 'x', false)).status, 404);
  assert.equal((await request('/_api/opens', 'GET', undefined, false)).status, 401);
  assert.equal((await request('/_api/opens', 'GET', undefined, true, {Authorization: 'Bearer wrong'})).status, 401);
  for (const path of ['/_opens/x/y', '/_private', '/BAD', '/' + 'a'.repeat(81), '/_api/pages/a--b']) assert.equal((await request(path)).status, 404);
});
test('PUT, HEAD without receipt, GET receipt and headers, DELETE', async () => {
  const {request, objects, pending} = fixture();
  assert.equal((await request('/_api/pages/hello', 'PUT', '<p>Hi</p>', true, {'Content-Type':'text/html'})).status, 204);
  const login = await request('/login', 'POST', new URLSearchParams({password:'test-password', slug:'hello'}), false, {Origin:'https://test'});
  const Cookie = login.headers.get('set-cookie').split(';')[0];
  const head = await request('/hello', 'HEAD', undefined, false, {Cookie});
  assert.equal(head.status, 200); assert.equal(await head.text(), ''); assert.equal(pending.length, 0);
  const get = await request('/hello', 'GET', undefined, true, {'User-Agent':'iPhone', Cookie});
  assert.equal(await get.text(), '<p>Hi</p>');
  for (const [key, value] of Object.entries({'content-type':'text/html', 'cache-control':'private, no-store', 'x-robots-tag':'noindex', 'referrer-policy':'no-referrer'})) assert.equal(get.headers.get(key), value);
  assert.match(get.headers.get('content-security-policy'), /default-src 'none'/);
  await Promise.all(pending);
  const keys = [...objects.keys()].filter(k => k.startsWith('_opens/'));
  assert.equal(keys.length, 1); assert.equal(JSON.parse(objects.get(keys[0]).body).device, 'iphone');
  assert.deepEqual(Object.keys(JSON.parse(objects.get(keys[0]).body)).sort(), ['at', 'device']);
  assert.deepEqual(await (await request('/_api/opens')).json(), {keys, next:null});
  await request('/_api/pages/hello', 'DELETE'); assert.equal(objects.size, 0);
});
test('size cap checks both advertised and actual bytes', async () => {
  const {request, objects} = fixture();
  assert.equal((await request('/_api/pages/large', 'PUT', 'x', true, {'Content-Length': String(10*1024*1024+1)})).status, 413);
  assert.equal((await request('/_api/pages/large', 'PUT', 'x'.repeat(10*1024*1024+1))).status, 413);
  assert.equal(objects.size, 0);
});
test('opens pagination uses exclusive keys', async () => {
  const {request, objects} = fixture();
  for (const key of ['_opens/a/1', '_opens/a/2', '_opens/b/1']) objects.set(key, {});
  assert.deepEqual(await (await request('/_api/opens?limit=2')).json(), {keys:['_opens/a/1','_opens/a/2'], next:'_opens/a/2'});
  assert.deepEqual(await (await request('/_api/opens?after=_opens/a/2&limit=2')).json(), {keys:['_opens/b/1'], next:null});
  assert.equal((await request('/_api/opens?limit=0')).status, 400);
});

test('password gate, signed cookies, expiry, missing secrets and API independence', async () => {
  const {request, pending, env} = fixture();
  await request('/_api/pages/hello', 'PUT', 'private');
  for (const method of ['GET', 'HEAD']) {
    const r = await request('/hello', method);
    assert.equal(r.status, 200);
    assert.equal(r.headers.get('cache-control'), 'private, no-store');
    assert.equal((await r.text()).includes('type="password"'), method === 'GET');
  }
  assert.equal(pending.length, 0);
  const post = (password, slug='hello') => request('/login', 'POST', new URLSearchParams({password, slug}), false, {Origin:'https://test'});
  assert.equal((await post('wrong')).status, 401);
  assert.equal((await post('test-password', '//evil.test')).status, 400);
  const login = await post('test-password');
  assert.equal(login.status, 303);
  assert.equal(login.headers.get('location'), '/hello');
  const cookie = login.headers.get('set-cookie');
  for (const flag of ['HttpOnly','Secure','SameSite=Lax','Path=/','Max-Age=2592000']) assert.ok(cookie.includes(flag));
  assert.equal(await (await request('/hello', 'GET', undefined, false, {Cookie:cookie.split(';')[0]})).text(), 'private');
  await Promise.all(pending);
  assert.equal(pending.length, 1);
  const now = Date.now;
  try {
    Date.now = () => now() + 2592001 * 1000;
    assert.match(await (await request('/hello', 'GET', undefined, false, {Cookie:cookie.split(';')[0]})).text(), /type="password"/);
  } finally { Date.now = now; }
  assert.match(await (await request('/hello', 'GET', undefined, false, {Cookie:cookie.split(';')[0]+'0'})).text(), /type="password"/);
  assert.equal(pending.length, 1);
  for (const key of ['PAGE_PASSWORD', 'SESSION_SECRET']) {
    const saved = env[key]; delete env[key];
    assert.equal((await request('/hello')).status, 503);
    assert.equal((await request('/_api/opens')).status, 200);
    env[key] = saved;
  }
});

test('pages are private by default; public opt-in can be flipped both ways', async () => {
  const {request, pending, objects} = fixture();
  await request('/_api/pages/notes', 'PUT', 'private notes');
  assert.match(await (await request('/notes', 'GET', undefined, false)).text(), /type="password"/);
  assert.equal(pending.length, 0);
  assert.equal((await request('/_api/pages/notes', 'PUT', 'attack', false, {'X-Steward-Public':'1'})).status, 401);
  assert.match(await (await request('/notes', 'GET', undefined, false)).text(), /type="password"/);
  for (const publicFlag of [true, false, true]) {
    await request('/_api/pages/notes', 'PUT', 'notes', true, publicFlag ? {'X-Steward-Public':'1'} : {});
    assert.equal(objects.get('notes').customMetadata.public, publicFlag ? '1' : '0');
    const before = pending.length;
    const head = await request('/notes', 'HEAD', undefined, false);
    assert.equal(head.status, 200);
    assert.equal(await head.text(), '');
    assert.equal(pending.length, before);
    const get = await request('/notes', 'GET', undefined, false);
    if (publicFlag) {
      assert.equal(await get.text(), 'notes');
      assert.equal(pending.length, before + 1);
    } else {
      assert.match(await get.text(), /type="password"/);
      assert.equal(pending.length, before);
    }
  }
  await Promise.all(pending);
  assert.equal((await request('/_api/opens', 'GET', undefined, false)).status, 401);
  const opens = await (await request('/_api/opens')).json();
  assert.equal(opens.keys.length, 2);
});

test('the login page lets browsers send its origin back (no-referrer makes them send Origin: null)', async () => {
  const {request} = fixture();
  await request('/_api/pages/hello', 'PUT', 'private');
  const r = await request('/hello', 'GET', undefined, false);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('referrer-policy'), 'same-origin');
});
