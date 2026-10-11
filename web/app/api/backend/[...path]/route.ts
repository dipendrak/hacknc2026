import { backendOrigin } from "@/lib/backend";

export const dynamic = "force-dynamic";
async function forward(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const allowed = request.method === "POST"
    ? path.length === 1 && path[0] === "ask"
    : (path.length === 1 && ["health", "customers"].includes(path[0]))
      || (path.length === 2 && ["model", "run", "txn"].includes(path[0]))
      || (path.length === 3 && path[0] === "run" && path[2] === "inputs")
      || (path.length === 3 && path[0] === "customers" && path[2] === "runs");
  if (!allowed || path.some(segment => !/^[A-Za-z0-9_-]+$/.test(segment))) return Response.json({ detail: "Route not found" }, { status: 404 });
  try {
    const upstream = await fetch(`${backendOrigin()}/${path.map(encodeURIComponent).join("/")}`, {
      method: request.method, headers: { "Content-Type": "application/json" },
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store", signal: AbortSignal.timeout(45000),
    });
    const headers = new Headers({ "Content-Type": upstream.headers.get("Content-Type") ?? "application/json", "Cache-Control": "no-store" });
    const unit = upstream.headers.get("X-Monetary-Unit");
    if (unit) headers.set("X-Monetary-Unit", unit);
    return new Response(await upstream.text(), { status: upstream.status, headers });
  } catch {
    return Response.json({ detail: "The simulation service is unavailable or timed out. Your question is preserved. Try again when it is ready." }, { status: 503 });
  }
}
export const GET = forward;
export const POST = forward;
