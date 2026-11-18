import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.mjs';
import {AUD, OWNER, TEAM, access, accessToken} from './access-fixture.mjs';

function fixture() {
  const objects = new Map(), pending = [];
  const env = {STEWARD_PAGES_TOKEN: 'test-secret', CANONICAL_HOST: 'test', ACCESS_TEAM: TEAM, ACCESS_AUD: AUD, OWNER_EMAIL: OWNER, PAGES: {
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
  const head = await request('/hello', 'HEAD', undefined, false, access);
  assert.equal(head.status, 200); assert.equal(await head.text(), ''); assert.equal(pending.length, 0);
  const get = await request('/hello', 'GET', undefined, true, {'User-Agent':'iPhone', ...access});
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

test('Access gate: owner token opens, anything else is refused, missing config fails closed, API independent', async () => {
  const {request, pending, env} = fixture();
  await request('/_api/pages/hello', 'PUT', 'private');
  for (const method of ['GET', 'HEAD']) {
    const r = await request('/hello', method, undefined, false);
    assert.equal(r.status, 403);
    assert.equal(r.headers.get('cache-control'), 'private, no-store');
  }
  assert.equal(pending.length, 0);
  assert.equal(await (await request('/hello', 'GET', undefined, false, access)).text(), 'private');
  await Promise.all(pending);
  assert.equal(pending.length, 1);
  const now = Math.floor(Date.now() / 1000);
  for (const claims of [{exp: now - 1}, {aud: ['other']}, {email: 'someone@example.com'}, {iss: 'https://evil.cloudflareaccess.com'}]) {
    assert.equal((await request('/hello', 'GET', undefined, false, {'cf-access-jwt-assertion': await accessToken(claims)})).status, 403);
  }
  assert.equal((await request('/hello', 'GET', undefined, false, {'cf-access-jwt-assertion': access['cf-access-jwt-assertion'] + 'x'})).status, 403);
  assert.equal(pending.length, 1);
  for (const key of ['ACCESS_TEAM', 'ACCESS_AUD', 'OWNER_EMAIL']) {
    const saved = env[key]; delete env[key];
    assert.equal((await request('/hello', 'GET', undefined, false, access)).status, 403);
    env[key] = saved;
  }
  const saved = env.CANONICAL_HOST; delete env.CANONICAL_HOST;
  assert.equal((await request('/hello', 'GET', undefined, false, access)).status, 503);
  assert.equal((await request('/_api/opens')).status, 200);
  env.CANONICAL_HOST = saved;
});

test('pages are private by default; public opt-in can be flipped both ways', async () => {
  const {request, pending, objects} = fixture();
  await request('/_api/pages/notes', 'PUT', 'private notes');
  assert.equal((await request('/notes', 'GET', undefined, false)).status, 403);
  assert.equal(pending.length, 0);
  assert.equal((await request('/_api/pages/notes', 'PUT', 'attack', false, {'X-Steward-Public':'1'})).status, 401);
  assert.equal((await request('/notes', 'GET', undefined, false)).status, 403);
  for (const publicFlag of [true, false, true]) {
    await request('/_api/pages/notes', 'PUT', 'notes', true, publicFlag ? {'X-Steward-Public':'1'} : {});
    assert.equal(objects.get('notes').customMetadata.public, publicFlag ? '1' : '0');
    const before = pending.length;
    const head = await request('/notes', 'HEAD', undefined, false);
    assert.equal(head.status, publicFlag ? 200 : 403);
    assert.equal(await head.text(), '');
    assert.equal(pending.length, before);
    const get = await request('/notes', 'GET', undefined, false);
    if (publicFlag) {
      assert.equal(await get.text(), 'notes');
      assert.equal(pending.length, before + 1);
    } else {
      assert.equal(get.status, 403);
      assert.equal(pending.length, before);
    }
  }
  await Promise.all(pending);
  assert.equal((await request('/_api/opens', 'GET', undefined, false)).status, 401);
  const opens = await (await request('/_api/opens')).json();
  assert.equal(opens.keys.length, 2);
});

test('other hosts redirect people to the canonical host; the API still answers there with its bearer', async () => {
  const {env} = fixture();
  const at = (url, headers = {}) => worker.fetch(new Request(url, {headers}), env, {waitUntil() {}});
  const r = await at('https://steward.05802.workers.dev/hello?x=1', access);
  assert.equal(r.status, 301);
  assert.equal(r.headers.get('location'), 'https://test/hello?x=1');
  assert.equal((await at('https://steward.05802.workers.dev/_api/opens', {Authorization: 'Bearer test-secret'})).status, 200);
  assert.equal((await at('https://steward.05802.workers.dev/_api/opens')).status, 401);
});
