// Matches the API's HTTP surface (api/main.py):
// POST /ask { text, customer_id } -> { run_id, prose, replay_url, validated }

export interface AskResponse {
  run_id: string;
  prose: string;
  replay_url: string | null;
  validated: boolean;
}

export class AskTimeoutError extends Error {}
export class AskClarificationError extends Error {}

export async function askOrchestrator(
  apiBaseUrl: string,
  text: string,
  customerId: string,
  timeoutMs: number
): Promise<AskResponse> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(`${apiBaseUrl}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, customer_id: customerId }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      if (res.status === 422) {
        let detail: unknown;
        try { detail = JSON.parse(body).detail; } catch { /* Use the generic error below. */ }
        if (typeof detail === "string" && detail.trim()) {
          throw new AskClarificationError(detail);
        }
      }
      throw new Error(`POST /ask failed: ${res.status} ${body}`);
    }

    return (await res.json()) as AskResponse;
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new AskTimeoutError(`POST /ask timed out after ${timeoutMs}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

export function resolveReplayUrl(
  result: Pick<AskResponse, "run_id" | "replay_url">,
  replayBaseUrl: string
): string {
  if (result.replay_url && /^https?:\/\//.test(result.replay_url)) {
    return result.replay_url;
  }
  return `${replayBaseUrl}/r/${result.run_id}`;
}
