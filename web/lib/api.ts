import type { AskResponse, CashflowModel, Customer, RecentRun } from "./types";
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/backend/${path}`, { ...options, cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new ApiError(typeof body.detail === "string" ? body.detail : "The simulation service could not complete this request.", response.status);
  return body as T;
}
export const fetchCustomers = (signal?: AbortSignal) => request<Customer[]>("customers", { signal });
export const fetchModel = (id: string, signal?: AbortSignal) => request<CashflowModel>(`model/${encodeURIComponent(id)}`, { signal });
export const fetchRecentRuns = (customerId: string, signal?: AbortSignal) =>
  request<RecentRun[]>(`customers/${encodeURIComponent(customerId)}/runs`, { signal });
export const askDecision = (text: string, customer_id: string) => request<AskResponse>("ask", {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, customer_id }),
});
