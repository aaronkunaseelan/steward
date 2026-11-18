// A test Access team: our own RSA key behind a mocked certs endpoint, and tokens signed with it (same as mech-d).
export const TEAM = 'https://test-team.cloudflareaccess.com', AUD = 'test-aud', OWNER = 'owner@example.com';
const pair = await crypto.subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify']);
const other = await crypto.subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify']);
const jwk = { ...(await crypto.subtle.exportKey('jwk', pair.publicKey)), kid: 'test-kid', alg: 'RS256', use: 'sig' };
const realFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => String(input?.url ?? input) === `${TEAM}/cdn-cgi/access/certs`
  ? new Response(JSON.stringify({ keys: [jwk] }), { headers: { 'content-type': 'application/json' } }) : realFetch(input, init);
const part = value => Buffer.from(JSON.stringify(value)).toString('base64url');
export async function accessToken(claims = {}, { kid = 'test-kid', key = pair.privateKey } = {}) {
  const now = Math.floor(Date.now() / 1000);
  const body = `${part({ alg: 'RS256', kid, typ: 'JWT' })}.${part({ iss: TEAM, aud: [AUD], email: OWNER, iat: now, nbf: now, exp: now + 600, ...claims })}`;
  return `${body}.${Buffer.from(await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(body))).toString('base64url')}`;
}
export const otherKey = other.privateKey;
export const access = { 'cf-access-jwt-assertion': await accessToken() };
