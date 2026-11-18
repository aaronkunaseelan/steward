// Cloudflare Access signs every request it lets through (Cf-Access-Jwt-Assertion, RS256).
// The Worker checks that signature itself, so a deleted or loosened Access app can't open the service.
// Config (not secret): ACCESS_TEAM (https://<team>.cloudflareaccess.com), ACCESS_AUD (the app's audience tag),
// OWNER_EMAIL. Any of them missing, or the certs unreachable, means no identity: fail closed.
const decoder = new TextDecoder();
const CERTS_TTL_MS = 3600_000;
let certs = { team: '', at: 0, keys: [] };

function b64url(text) {
  const s = text.replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(s + '='.repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0));
}

async function accessKeys(team, refresh = false) {
  if (!refresh && certs.team === team && Date.now() - certs.at < CERTS_TTL_MS) return certs.keys;
  const response = await fetch(`${team}/cdn-cgi/access/certs`);
  if (!response.ok) throw new Error('access certs unavailable');
  const { keys } = await response.json();
  if (!Array.isArray(keys)) throw new Error('access certs malformed');
  certs = { team, at: Date.now(), keys };
  return keys;
}

// The owner's email when the request carries a valid Access token for this app, else null.
export async function accessIdentity(request, env) {
  const team = env.ACCESS_TEAM, aud = env.ACCESS_AUD, owner = env.OWNER_EMAIL;
  const token = request.headers.get('cf-access-jwt-assertion') || '';
  const parts = token.split('.');
  if (!team || !aud || !owner || parts.length !== 3 || token.length > 8192) return null;
  let header, claims;
  try {
    header = JSON.parse(decoder.decode(b64url(parts[0])));
    claims = JSON.parse(decoder.decode(b64url(parts[1])));
  } catch { return null; }
  if (header?.alg !== 'RS256' || typeof header.kid !== 'string' || !claims || typeof claims !== 'object') return null;
  let jwk;
  try {
    jwk = (await accessKeys(team)).find(k => k.kid === header.kid);
    // Access rotates its keys; an unknown kid is worth one refresh.
    if (!jwk) jwk = (await accessKeys(team, true)).find(k => k.kid === header.kid);
  } catch { return null; }
  if (!jwk || jwk.kty !== 'RSA') return null;
  let valid = false;
  try {
    const key = await crypto.subtle.importKey('jwk', { kty: 'RSA', n: jwk.n, e: jwk.e }, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
    valid = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, b64url(parts[2]), new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
  } catch { return null; }
  if (!valid) return null;
  const now = Date.now() / 1000, audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (claims.iss !== team || !audiences.includes(aud)) return null;
  if (typeof claims.exp !== 'number' || claims.exp <= now) return null;
  if (typeof claims.nbf === 'number' && claims.nbf > now + 60) return null;
  if (typeof claims.email !== 'string' || claims.email.toLowerCase() !== owner.toLowerCase()) return null;
  return claims.email;
}

export function resetAccessCache() { certs = { team: '', at: 0, keys: [] }; }
