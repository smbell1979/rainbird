import type { NextRequest } from "next/server";

/**
 * Proxy to the Python service, which listens on localhost only.
 *
 * The service owns the controller connection and the database; this page never
 * talks to either directly. Status is served from the service's cache, so the
 * page polling it does not translate into requests to the LNK2 module.
 *
 * The base URL is read inside the handler, not at module scope (see the
 * neghiking README "Gotchas": module-scope environment/path work trips the
 * build tracer).
 */
async function forward(req: NextRequest, ctx: RouteContext<"/api/svc/[...path]">) {
  const base = process.env.RAINBIRD_API ?? "http://127.0.0.1:8710";
  const { path } = await ctx.params;
  const url = `${base}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const init: RequestInit = { method: req.method, cache: "no-store", headers: {} };
  if (req.method !== "GET" && req.method !== "DELETE") {
    init.body = await req.text();
    init.headers = { "content-type": "application/json" };
  }
  try {
    const res = await fetch(url, init);
    return new Response(await res.text(), {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json(
      { detail: "The Rain Bird service is not running (or not reachable on localhost)." },
      { status: 502 },
    );
  }
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const DELETE = forward;
