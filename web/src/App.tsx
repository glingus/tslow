import { useCallback, useEffect, useMemo, useState } from "react";
import { curveLinear } from "@visx/curve";
import { Area } from "@/components/charts/area";
import { AreaChart } from "@/components/charts/area-chart";
import { Grid } from "@/components/charts/grid";
import { Line } from "@/components/charts/line";
import { LineChart } from "@/components/charts/line-chart";
import { ChartTooltip } from "@/components/charts/tooltip";
import { XAxis } from "@/components/charts/x-axis";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  DECISION_LABELS,
  type DecisionChoice,
  type DecisionStat,
  displayLabel,
  EXECUTION_STATUS_LABELS,
  fetchDecisionStats,
  fetchDecisionStatus,
  fetchIncidents,
  fetchMetrics,
  fetchNasaComparison,
  fetchOverhead,
  fetchOverheadSeries,
  fetchTopCulprits,
  INCIDENT_TYPE_LABELS,
  type Incident,
  type MetricKey,
  METRIC_KEYS,
  METRIC_LABELS,
  type MetricsResponse,
  type NasaComparison,
  type OverheadPoint,
  type OverheadSummary,
  postIncidentDecision,
  type RangeKey,
  RANGES,
  RESOURCE_LABELS,
  STATUS_LABELS,
  type TopCulprit,
} from "@/lib/api";
import { formatDateTime, formatTrend, formatValue } from "@/lib/format";
import { withGapBreaks } from "@/lib/timeseries";

const RANGE_LABELS: Record<RangeKey, string> = { "1h": "1H", "24h": "24H", "7d": "7D", "30d": "30D" };
const REFRESH_MS = 5000;
// protection.py::ProtectionLevel.L0_INTOCCABILE — an L0 incident is always informational only
// (see cli/resolve.py and web/api.py::post_incident_decision), never actionable from the browser.
const L0_PROTECTION_LEVEL = 0;
const DECISION_POLL_MS = 1000;
const DECISION_POLL_TIMEOUT_MS = 15_000;

function IndicatorCard({
  metricKey,
  data,
  selected,
  onSelect,
}: {
  metricKey: MetricKey;
  data: MetricsResponse | null;
  selected: boolean;
  onSelect: () => void;
}) {
  const column = data?.columns[metricKey] ?? null;
  const last = column ? data?.samples.at(-1) : null;
  const current = last && column ? (last[column] as number | null) : null;
  const trend = data?.trend[metricKey] ?? null;

  return (
    <button
      className={`flex flex-col items-start gap-1 border p-3 text-left uppercase transition-colors ${
        selected ? "border-primary bg-primary text-primary-foreground" : "border-border hover:bg-accent hover:text-accent-foreground"
      }`}
      onClick={onSelect}
      type="button"
    >
      <span className="text-xs tracking-wide opacity-80">{METRIC_LABELS[metricKey]}</span>
      <span className="font-bold text-2xl">{formatValue(current)}</span>
      <span className="text-xs opacity-80">{formatTrend(trend)}</span>
    </button>
  );
}

function MetricChart({ data, metric, loading }: { data: MetricsResponse | null; metric: MetricKey; loading: boolean }) {
  const column = data?.columns[metric] ?? null;
  const samples = data?.samples ?? [];
  const maColumn = column ? `${column}_ma_1h` : null;
  const hasMa = Boolean(maColumn && samples.some((s) => s[maColumn] != null));

  // The PC sleeps, the watcher stops, sampling slows down: without this, a real gap in the
  // data would be drawn as a straight ramp between two far-apart moments, as if the value had
  // actually risen/fallen in between (see lib/timeseries.ts).
  const gapAwareSamples = useMemo(
    () => withGapBreaks(samples, "ts_ms", [column, maColumn].filter((k): k is string => Boolean(k))),
    [samples, column, maColumn],
  );

  // The chart library (visx/d3) computes the X-axis time extent even on an empty screen: on an
  // empty array it produces an invalid Date and Intl.DateTimeFormat throws RangeError. Same
  // guard as the TUI (cli/dashboard.py::_plot_ascii): no chart until there's real data, a text
  // message in its place instead.
  if (loading && samples.length === 0) {
    return <p className="py-16 text-center text-sm uppercase opacity-70">Loading…</p>;
  }
  if (!column || samples.length === 0) {
    return <p className="py-16 text-center text-sm uppercase opacity-70">No data for this range.</p>;
  }

  return (
    <LineChart aspectRatio="3 / 1" data={gapAwareSamples} xDataKey="ts_ms">
      <Grid horizontal strokeDasharray="4,4" />
      <XAxis numTicks={5} />
      <Line curve={curveLinear} dataKey={column} fadeEdges={false} showMarkers={false} stroke="#ffffff" strokeWidth={2} />
      {hasMa && maColumn ? (
        <Line curve={curveLinear} dataKey={maColumn} fadeEdges={false} showHighlight={false} showMarkers={false} stroke="rgba(255,255,255,0.45)" strokeWidth={1.5} />
      ) : null}
      <ChartTooltip />
    </LineChart>
  );
}

function TopCulpritsCard({ rows }: { rows: TopCulprit[] }) {
  return (
    <Card className="border-border">
      <CardHeader>
        <CardTitle className="text-sm uppercase tracking-wide">Top offenders</CardTitle>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm opacity-70">No incidents in this period.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Group</TableHead>
                <TableHead>Resource</TableHead>
                <TableHead className="text-right">Incidents</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={`${row.group_key}-${row.resource}`}>
                  <TableCell className="font-medium">{row.group_key}</TableCell>
                  <TableCell className="uppercase">{displayLabel(RESOURCE_LABELS, row.resource)}</TableCell>
                  <TableCell className="text-right">{row.incidenti}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function IncidentActions({ incident }: { incident: Incident }) {
  const [pending, setPending] = useState<DecisionChoice | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);

  const act = useCallback(
    async (choice: DecisionChoice) => {
      setPending(choice);
      setOutcome(null);
      try {
        const { decision_id } = await postIncidentDecision(incident.id, choice);
        const deadline = Date.now() + DECISION_POLL_TIMEOUT_MS;
        let status = "pending";
        while (status === "pending" && Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, DECISION_POLL_MS));
          status = (await fetchDecisionStatus(decision_id)).execution_status;
        }
        // We don't force a refresh here: the incident closes on the watcher's side as soon as
        // the decision is processed (whatever the outcome), so the existing 5s auto-refresh is
        // enough to make the buttons disappear — forcing it right away would wipe this message
        // before anyone could read it.
        setOutcome(EXECUTION_STATUS_LABELS[status] ?? status);
      } catch (e) {
        setOutcome(e instanceof Error ? e.message : String(e));
      } finally {
        setPending(null);
      }
    },
    [incident.id],
  );

  if (outcome) {
    return <span className="text-xs uppercase opacity-80">{outcome}</span>;
  }

  return (
    <div className="flex flex-wrap gap-1">
      {(Object.keys(DECISION_LABELS) as DecisionChoice[]).map((choice) => (
        <Button
          className="h-6 px-1.5 text-[0.65rem] uppercase"
          disabled={pending !== null}
          key={choice}
          onClick={() => act(choice)}
          size="xs"
          variant="outline"
        >
          {pending === choice ? "…" : DECISION_LABELS[choice]}
        </Button>
      ))}
    </div>
  );
}

function IncidentsCard({ rows }: { rows: Incident[] }) {
  return (
    <Card className="border-border">
      <CardHeader>
        <CardTitle className="text-sm uppercase tracking-wide">Recent incidents</CardTitle>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm opacity-70">No incidents in this period.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>When</TableHead>
                <TableHead>Group</TableHead>
                <TableHead>Resource</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const actionable = row.status === "open" && row.group_key !== null && row.protection_level !== L0_PROTECTION_LEVEL;
                return (
                  <TableRow key={row.id}>
                    <TableCell>{formatDateTime(row.opened_at_ms)}</TableCell>
                    <TableCell className="font-medium">{row.group_key ?? "system"}</TableCell>
                    <TableCell className="uppercase">{displayLabel(RESOURCE_LABELS, row.resource)}</TableCell>
                    <TableCell className="uppercase">{displayLabel(INCIDENT_TYPE_LABELS, row.incident_type)}</TableCell>
                    <TableCell>
                      <Badge variant={row.status === "open" ? "default" : "outline"}>{displayLabel(STATUS_LABELS, row.status)}</Badge>
                    </TableCell>
                    <TableCell>
                      {actionable ? <IncidentActions incident={row} /> : <span className="text-xs opacity-50">—</span>}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function OverheadChart({ title, dataKey, series, loading }: { title: string; dataKey: "daemon_cpu_percent" | "daemon_rss_mb"; series: OverheadPoint[]; loading: boolean }) {
  const hasData = series.some((s) => s[dataKey] != null);
  const gapAwareSeries = useMemo(() => withGapBreaks(series, "ts_ms", [dataKey]), [series, dataKey]);

  return (
    <div>
      <p className="mb-2 text-xs uppercase tracking-wide opacity-70">{title}</p>
      {loading && series.length === 0 ? (
        <p className="py-8 text-center text-sm uppercase opacity-70">Loading…</p>
      ) : !hasData ? (
        <p className="py-8 text-center text-sm uppercase opacity-70">No data for this range.</p>
      ) : (
        <AreaChart aspectRatio="3 / 1" data={gapAwareSeries} xDataKey="ts_ms">
          <Grid horizontal strokeDasharray="4,4" />
          <XAxis numTicks={4} />
          <Area curve={curveLinear} dataKey={dataKey} fadeEdges={false} fillOpacity={0.15} showMarkers={false} stroke="#ffffff" strokeWidth={1.5} />
          <ChartTooltip />
        </AreaChart>
      )}
    </div>
  );
}

function OverheadFooter({ overhead, decisions, nasa }: { overhead: OverheadSummary | null; decisions: DecisionStat[]; nasa: NasaComparison | null }) {
  const decisionSummary = decisions.map((d) => `${d.choice}/${d.source}=${d.conteggio}`).join("  ");
  return (
    <footer className="border border-border p-3 text-xs uppercase tracking-wide opacity-80">
      {overhead && overhead.samples > 0 ? (
        <span>
          Watcher overhead: CPU avg={formatValue(overhead.cpu_avg, 3)}% max={formatValue(overhead.cpu_max, 3)}% · RSS avg=
          {formatValue(overhead.rss_avg)}MB max={formatValue(overhead.rss_max)}MB ({overhead.samples} samples)
        </span>
      ) : (
        <span>Watcher overhead: no data in this period.</span>
      )}
      {decisionSummary ? <span className="ml-4">· Decisions: {decisionSummary}</span> : null}
      {nasa ? <span className="ml-4 normal-case tracking-normal opacity-100">· {nasa.line}</span> : null}
    </footer>
  );
}

export default function App() {
  const [range, setRange] = useState<RangeKey>("24h");
  const [metric, setMetric] = useState<MetricKey>("cpu");
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [topCulprits, setTopCulprits] = useState<TopCulprit[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [overhead, setOverhead] = useState<OverheadSummary | null>(null);
  const [overheadSeries, setOverheadSeries] = useState<OverheadPoint[]>([]);
  const [decisions, setDecisions] = useState<DecisionStat[]>([]);
  const [nasa, setNasa] = useState<NasaComparison | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (r: RangeKey) => {
    try {
      const [m, tc, inc, oh, ohSeries, dec, nasaResult] = await Promise.all([
        fetchMetrics(r),
        fetchTopCulprits(r),
        fetchIncidents(r),
        fetchOverhead(r),
        fetchOverheadSeries(r),
        fetchDecisionStats(r),
        fetchNasaComparison(),
      ]);
      setMetrics(m);
      setTopCulprits(tc);
      setIncidents(inc);
      setOverhead(oh);
      setOverheadSeries(ohSeries);
      setDecisions(dec);
      setNasa(nasaResult);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    refresh(range);
    const interval = setInterval(() => refresh(range), REFRESH_MS);
    return () => clearInterval(interval);
  }, [range, refresh]);

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-4 p-4 md:p-6">
      <header className="flex flex-wrap items-center justify-between gap-4 border-border border-b pb-4">
        <h1 className="font-bold text-2xl uppercase tracking-widest">why ts so slow?</h1>
        <Select onValueChange={(v) => setRange(v as RangeKey)} value={range}>
          <SelectTrigger className="w-28 border-border uppercase">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {RANGES.map((r) => (
              <SelectItem key={r} value={r}>
                {RANGE_LABELS[r]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </header>

      {error ? <div className="border border-border p-3 text-sm uppercase">Error: {error}</div> : null}

      <section className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {METRIC_KEYS.map((key) => (
          <IndicatorCard data={metrics} key={key} metricKey={key} onSelect={() => setMetric(key)} selected={metric === key} />
        ))}
      </section>

      <Card className="border-border">
        <CardHeader>
          <CardTitle className="text-sm uppercase tracking-wide">{METRIC_LABELS[metric]}</CardTitle>
        </CardHeader>
        <CardContent>
          <MetricChart data={metrics} loading={loading} metric={metric} />
        </CardContent>
      </Card>

      <section className="grid gap-4 md:grid-cols-2">
        <TopCulpritsCard rows={topCulprits} />
        <IncidentsCard rows={incidents} />
      </section>

      <Card className="border-border">
        <CardHeader>
          <CardTitle className="text-sm uppercase tracking-wide">Watcher overhead over time</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <OverheadChart dataKey="daemon_cpu_percent" loading={loading} series={overheadSeries} title="CPU %" />
          <OverheadChart dataKey="daemon_rss_mb" loading={loading} series={overheadSeries} title="RSS MB" />
        </CardContent>
      </Card>

      <OverheadFooter decisions={decisions} nasa={nasa} overhead={overhead} />
    </div>
  );
}
