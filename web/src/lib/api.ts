// Client for the FastAPI backend (src/tslow/web/api.py). Same shape as the analytics.py
// DataFrames converted to JSON — see _df_to_records() on the backend.

export const RANGES = ["1h", "24h", "7d", "30d"] as const;
export type RangeKey = (typeof RANGES)[number];

export const METRIC_KEYS = ["cpu", "ram", "disk", "network", "gpu"] as const;
export type MetricKey = (typeof METRIC_KEYS)[number];

export const METRIC_LABELS: Record<MetricKey, string> = {
  cpu: "CPU %",
  ram: "RAM %",
  disk: "DISK ms",
  network: "NETWORK B/s (in)",
  gpu: "GPU %",
};

export interface MetricsResponse {
  range: RangeKey;
  columns: Record<MetricKey, string | null>;
  trend: Record<MetricKey, number | null>;
  samples: Record<string, number | string | null>[];
}

export interface TopCulprit {
  group_key: string;
  resource: string;
  incidenti: number;
  ultimo_ms: number;
}

export interface DecisionStat {
  choice: string;
  source: string;
  conteggio: number;
}

export interface Incident {
  id: number;
  opened_at_ms: number;
  resource: string;
  incident_type: string;
  severity: string;
  group_key: string | null;
  status: string;
  protection_level: number | null;
  proposed_action: string;
  quota_percent: number | null;
}

export interface OverheadSummary {
  cpu_avg: number | null;
  cpu_max: number | null;
  rss_avg: number | null;
  rss_max: number | null;
  samples: number;
}

export interface OverheadPoint {
  ts_ms: number;
  daemon_cpu_percent: number | null;
  daemon_rss_mb: number | null;
  // AreaChart (Bklit) wants `Record<string, unknown>[]` for raw data, same as MetricsResponse samples.
  [key: string]: unknown;
}

export interface NasaComparison {
  worse_percent: number;
  line: string;
}

// Display labels for the enum values stored in the DB.
export const RESOURCE_LABELS: Record<string, string> = {
  cpu: "CPU",
  ram: "RAM",
  disk: "disk",
  network: "network",
  gpu: "GPU",
  system: "system",
  app: "app",
};

export const INCIDENT_TYPE_LABELS: Record<string, string> = {
  anomaly: "anomaly",
  critical: "critical",
  leak: "leak",
  hung: "hung",
  throttling: "throttling",
};

export const STATUS_LABELS: Record<string, string> = {
  open: "open",
  resolved: "resolved",
  expired: "expired",
  ignored: "ignored",
};

export function displayLabel(mapping: Record<string, string>, value: string | null | undefined): string {
  if (value == null) {
    return "n/a";
  }
  return mapping[value] ?? value;
}

// Same values as `decisions.choice` in the DB (see rules.py): the API uses them directly,
// without the numeric [1]/[2]/[3] mapping that's only needed by `tslow resolve`'s text prompt.
export type DecisionChoice = "deny" | "allow_once" | "allow_always";

export const DECISION_LABELS: Record<DecisionChoice, string> = {
  deny: "Deny",
  allow_once: "Allow once",
  allow_always: "Always allow",
};

export interface DecisionResult {
  decision_id: number;
  execution_status: string;
}

export interface DecisionStatus {
  id: number;
  execution_status: string;
  executed_at_ms: number | null;
}

export const EXECUTION_STATUS_LABELS: Record<string, string> = {
  pending: "pending",
  ok: "done",
  access_denied: "access denied",
  no_such_process: "process no longer there",
  identity_mismatch: "process changed, id mismatch",
  protected_refused: "refused, protected process",
  dry_run: "simulated (dry-run)",
};

async function getJson<T>(path: string, params: Record<string, string | number>): Promise<T> {
  const query = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
  const resp = await fetch(`/api/${path}?${query}`);
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    throw new Error(body?.detail ?? `${path}: HTTP ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

export const fetchMetrics = (range: RangeKey) => getJson<MetricsResponse>("metrics", { range });
export const fetchTopCulprits = (range: RangeKey, limit = 10) => getJson<TopCulprit[]>("top-culprits", { range, limit });
export const fetchDecisionStats = (range: RangeKey) => getJson<DecisionStat[]>("decisions/stats", { range });
export const fetchIncidents = (range: RangeKey, limit = 20) => getJson<Incident[]>("incidents", { range, limit });
export const fetchOverhead = (range: RangeKey) => getJson<OverheadSummary>("overhead", { range });
export const fetchOverheadSeries = (range: RangeKey) => getJson<OverheadPoint[]>("overhead/series", { range });
export const fetchNasaComparison = () => getJson<NasaComparison>("nasa", {});

export async function postIncidentDecision(incidentId: number, choice: DecisionChoice): Promise<DecisionResult> {
  const resp = await fetch(`/api/incidents/${incidentId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ choice }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    throw new Error(body?.detail ?? `decision: HTTP ${resp.status}`);
  }
  return resp.json();
}

export const fetchDecisionStatus = (decisionId: number) => getJson<DecisionStatus>(`decisions/${decisionId}`, {});
