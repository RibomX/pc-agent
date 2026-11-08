// Stable address for PC Agent, on Cloudflare Workers' free plan.
// A PC's tunnel address changes on every restart. The app reports the current one here, and
// connectors and phone shortcuts use https://<worker>/<id>/... which never changes.
//   /<id>/mcp/...  is forwarded to the PC (AI connectors do not follow redirects reliably)
//   /<id>/app/...  redirects to the PC, so the live view does not use up the Worker's daily quota

const ID = /^[\w-]{16,64}$/;
const TUNNEL_HOST = /^[a-z0-9-]+\.trycloudflare\.com$/; // never forward anywhere else
const KEEP_SECONDS = 60 * 60 * 24 * 90;

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// At most this many registrations per hour from one IP address. A PC registers once per app or
// tunnel start, so this only stops someone from using up the free plan's 1,000 writes a day.
// Counted in the edge cache (free, no KV writes); a determined attacker using many IPs or data
// centres can still get around it.
const REGISTRATIONS_PER_HOUR = 20;

async function tooManyRegistrations(request) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  // A fixed window per clock hour: every hour starts from zero, however often a blocked PC retries.
  const hour = Math.floor(Date.now() / 3600000);
  const key = new Request(`https://ratelimit.invalid/${hour}/${encodeURIComponent(ip)}`);
  const cache = caches.default;
  const hit = await cache.match(key);
  const count = hit ? Number(await hit.text()) + 1 : 1;
  await cache.put(key, new Response(String(count), { headers: { "Cache-Control": "max-age=3600" } }));
  return count > REGISTRATIONS_PER_HOUR;
}

async function register(request, env) {
  if (await tooManyRegistrations(request)) return new Response("Too many requests", { status: 429 });
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("Bad request", { status: 400 });
  }
  const { id, token, host } = body;
  if (!ID.test(id || "") || !TUNNEL_HOST.test(host || "") || typeof token !== "string" || token.length < 32) {
    return new Response("Bad request", { status: 400 });
  }
  const tokenHash = await sha256(token);
  const current = await env.DEVICES.get(id, { type: "json" });
  // The first registration claims the id; afterwards only the same PC may change it.
  if (current && current.tokenHash !== tokenHash) return new Response("Forbidden", { status: 403 });
  if (current && current.host === host) return new Response("ok"); // nothing changed, save a write
  await env.DEVICES.put(id, JSON.stringify({ tokenHash, host }), { expirationTtl: KEEP_SECONDS });
  return new Response("ok");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/register" && request.method === "POST") return register(request, env);

    const match = url.pathname.match(/^\/([^/]+)(\/(app|mcp)\/.*)$/);
    if (!match || !ID.test(match[1])) return new Response("Not found", { status: 404 });
    const device = await env.DEVICES.get(match[1], { type: "json", cacheTtl: 30 });
    const offline = () => new Response("This PC is off or PC Agent is not running.", { status: 503 });
    if (!device) return offline();

    const target = `https://${device.host}${match[2]}${url.search}`;
    if (match[3] === "app") return Response.redirect(target, 302);
    try {
      return await fetch(new Request(target, request));
    } catch {
      return offline();
    }
  },
};
