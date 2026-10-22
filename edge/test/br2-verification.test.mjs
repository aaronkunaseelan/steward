import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.mjs';

test('BR2 upload read-back distinguishes missing objects from the login response', async () => {
  const objects = new Map();
  const env = {PAGE_PASSWORD: 'fixture', SESSION_SECRET: 'fixture',
    STEWARD_PAGES_TOKEN: 'fixture', PAGES: {
      async put(key, body, options) { objects.set(key, {body: await body.arrayBuffer(), ...options}); },
      async get(key) { return objects.get(key); },
      async head(key) { return objects.get(key); }
    }};
  const request = (slug, method='GET', body, auth=true) => worker.fetch(
    new Request('https://fixture/_api/pages/' + slug, {method, body,
      headers: auth ? {Authorization: 'Bearer fixture'} : {}}), env,
    {waitUntil() { assert.fail('API read-back must not count as an owner open'); }});
  assert.equal((await request('missing-page')).status, 404);
  assert.equal((await request('missing-page', 'GET', undefined, false)).status, 401);
  const data = new Uint8Array([0, 1, 128, 255]);
  assert.equal((await request('saved', 'PUT', data)).status, 204);
  const read = await request('saved');
  assert.equal(read.status, 200);
  assert.deepEqual(new Uint8Array(await read.arrayBuffer()), data);
});
