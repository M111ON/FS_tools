/**
 * AI Relay Worker — Proxy relay สำหรับส่งต่อคำขอไปยัง AI providers ทั้งหมดผ่านเครือข่าย Cloudflare
 *
 * รองรับ providers:
 *   - OpenAI        → /openai/v1/chat/completions, /openai/v1/responses, ...
 *   - Anthropic     → /anthropic/v1/messages, ...
 *   - Google Gemini → /gemini/v1beta/models/...
 *   - DeepSeek      → /deepseek/chat/completions, ...
 *   - Groq          → /groq/openai/v1/chat/completions, ...
 *
 * วิธีใช้: ส่ง request มาที่ https://<worker-name>.<account>.workers.dev/{provider}/{path}
 * ตัวอย่าง: POST https://ai-relay-worker.xxx.workers.dev/openai/v1/chat/completions
 *           Header: Authorization: Bearer sk-... (API key ของ provider)
 *           Body: { "model": "gpt-4o", "messages": [...] }
 *
 * หรือถ้าตั้งค่า AI_GATEWAY_ID ใน vars จะ route ผ่าน AI Gateway โดยอัตโนมัติ
 */

// ─── Provider routing table ───────────────────────────────────────────────────
// แต่ละ entry มี: prefix ที่ Worker รับ, upstream base URL ของ provider, และ header transforms
const PROVIDERS = {
  openai: {
    upstream: "https://api.openai.com",
    // ถ้าใช้ AI Gateway: gateway path = /v1/{account_id}/{gateway_id}/openai
    gatewayPath: "openai",
    authHeader: "Authorization", // Bearer token
  },
  anthropic: {
    upstream: "https://api.anthropic.com",
    gatewayPath: "anthropic",
    authHeader: "x-api-key", // Anthropic ใช้ x-api-key + anthropic-version
    extraHeaders: { "anthropic-version": "2023-06-01" },
  },
  gemini: {
    upstream: "https://generativelanguage.googleapis.com",
    gatewayPath: "google-ai-studio",
    authHeader: "x-goog-api-key",
  },
  deepseek: {
    upstream: "https://api.deepseek.com",
    gatewayPath: "deepseek",
    authHeader: "Authorization",
  },
  groq: {
    upstream: "https://api.groq.com/openai",
    gatewayPath: "groq",
    authHeader: "Authorization",
  },
  openrouter: {
    upstream: "https://api.openrouter.ai",
    gatewayPath: "openrouter",
    authHeader: "Authorization",
  },
  cerebras: {
    upstream: "https://api.cerebras.ai",
    gatewayPath: "cerebras",
    authHeader: "Authorization",
  },
  mistral: {
    upstream: "https://api.mistral.ai",
    gatewayPath: "mistral",
    authHeader: "Authorization",
  },
  xai: {
    upstream: "https://api.x.ai",
    gatewayPath: "xai",
    authHeader: "Authorization",
  },
};

// ─── Generic token-gated passthrough ─────────────────────────────────────────
// ใช้เมื่อ provider ยังไม่อยู่ในตาราง: /g/<RELAY_TOKEN>/<host>/<path...>
// ปิดใช้ถ้าไม่ได้ตั้ง secret RELAY_TOKEN (fail-closed)
const HOST_RE = /^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$/;

function cleanHeaders(request) {
  const h = new Headers(request.headers);
  h.delete("host");
  h.delete("cf-connecting-ip");
  h.delete("cf-ipcountry");
  h.delete("cf-ray");
  h.delete("cf-visitor");
  return h;
}

async function genericRelay(request, url, env, pathParts) {
  const token = env.RELAY_TOKEN;
  if (!token) {
    return new Response("not found", { status: 404 });
  }
  if (pathParts.length < 3) {
    return new Response("usage: /g/<token>/<host>/<path>", { status: 400 });
  }
  if (pathParts[1] !== token) {
    return new Response("forbidden", { status: 403 });
  }
  const host = pathParts[2].toLowerCase();
  if (!HOST_RE.test(host)) {
    return new Response("bad host", { status: 400 });
  }
  const target = `https://${host}/${pathParts.slice(3).join("/")}${url.search || ""}`;
  try {
    const upstreamResp = await fetch(target, {
      method: request.method,
      headers: cleanHeaders(request),
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
    });
    const respHeaders = new Headers(upstreamResp.headers);
    for (const [k, v] of Object.entries(CORS_HEADERS)) respHeaders.set(k, v);
    return new Response(upstreamResp.body, {
      status: upstreamResp.status,
      statusText: upstreamResp.statusText,
      headers: respHeaders,
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: "upstream failed", message: err.message }),
      { status: 502, headers: { "Content-Type": "application/json" } }
    );
  }
}

// ─── CORS ────────────────────────────────────────────────────────────────────
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "*",
  "Access-Control-Max-Age": "86400",
};

function corsify(response) {
  const newResp = new Response(response.body, response);
  for (const [k, v] of Object.entries(CORS_HEADERS)) {
    newResp.headers.set(k, v);
  }
  return newResp;
}

// ─── Main handler ────────────────────────────────────────────────────────────
export default {
  async fetch(request, env, ctx) {
    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);

    // Health check
    if (url.pathname === "/" || url.pathname === "/health") {
      return corsify(
        new Response(
          JSON.stringify({
            status: "ok",
            service: "ai-relay-worker",
            providers: Object.keys(PROVIDERS),
            timestamp: new Date().toISOString(),
          }),
          { headers: { "Content-Type": "application/json" } }
        )
      );
    }

    // Parse provider from path: /{provider}/{rest...}
    const pathParts = url.pathname.split("/").filter(Boolean); // ["openai", "v1", "chat", "completions"]

    // Generic token-gated passthrough: /g/<token>/<host>/<path...>
    if (pathParts[0] === "g") {
      return genericRelay(request, url, env, pathParts);
    }

    if (pathParts.length < 2) {
      return corsify(
        new Response(
          JSON.stringify({
            error: "Path must be /{provider}/{path} — e.g. /openai/v1/chat/completions",
            supported_providers: Object.keys(PROVIDERS),
          }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        )
      );
    }

    const providerKey = pathParts[0].toLowerCase();
    const provider = PROVIDERS[providerKey];

    if (!provider) {
      return corsify(
        new Response(
          JSON.stringify({
            error: `Unknown provider: ${providerKey}`,
            supported_providers: Object.keys(PROVIDERS),
          }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        )
      );
    }

    // Reconstruct upstream path (everything after /{provider}/)
    const upstreamPath = "/" + pathParts.slice(1).join("/");
    const queryString = url.search || "";
    const accountId = env.AI_GATEWAY_ACCOUNT_ID || env.CLOUDFLARE_ACCOUNT_ID;
    const gatewayId = env.AI_GATEWAY_ID;

    // ─── Build target URL ─────────────────────────────────────────────────
    let targetUrl;
    if (gatewayId && accountId) {
      // Route ผ่าน AI Gateway เพื่อรับ logging, caching, rate limiting, guardrails
      targetUrl = `https://gateway.ai.cloudflare.com/v1/${accountId}/${gatewayId}/${provider.gatewayPath}${upstreamPath}${queryString}`;
    } else {
      // Direct proxy ไปยัง provider
      targetUrl = `${provider.upstream}${upstreamPath}${queryString}`;
    }

    // ─── Build forwarded request ──────────────────────────────────────────
    const reqHeaders = new Headers(request.headers);

    // เพิ่ม extra headers ของ provider (เช่น anthropic-version)
    if (provider.extraHeaders) {
      for (const [k, v] of Object.entries(provider.extraHeaders)) {
        reqHeaders.set(k, v);
      }
    }

    // ลบ headers ที่ไม่ควรส่งต่อ
    reqHeaders.delete("host");
    reqHeaders.delete("cf-connecting-ip");
    reqHeaders.delete("cf-ipcountry");
    reqHeaders.delete("cf-ray");
    reqHeaders.delete("cf-visitor");

    // สร้าง request ใหม่ไปยัง upstream
    const upstreamReq = new Request(targetUrl, {
      method: request.method,
      headers: reqHeaders,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
    });

    // ─── Forward request & stream response back ───────────────────────────
    try {
      const upstreamResp = await fetch(upstreamReq);

      // สร้าง response ใหม่เพื่อเพิ่ม CORS headers
      const respHeaders = new Headers(upstreamResp.headers);
      for (const [k, v] of Object.entries(CORS_HEADERS)) {
        respHeaders.set(k, v);
      }

      // ส่งกลับแบบ stream (รองรับ streaming responses เช่น SSE จาก AI providers)
      return new Response(upstreamResp.body, {
        status: upstreamResp.status,
        statusText: upstreamResp.statusText,
        headers: respHeaders,
      });
    } catch (err) {
      return corsify(
        new Response(
          JSON.stringify({
            error: "Upstream request failed",
            message: err.message,
            provider: providerKey,
            target: targetUrl,
          }),
          { status: 502, headers: { "Content-Type": "application/json" } }
        )
      );
    }
  },
};
