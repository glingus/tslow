export function formatValue(value: number | null | undefined, digits = 1): string {
  return value == null || Number.isNaN(value) ? "n/a" : value.toFixed(digits);
}

export function formatTrend(perHour: number | null | undefined): string {
  if (perHour == null || Number.isNaN(perHour)) return "";
  const arrow = perHour > 0.01 ? "↑" : perHour < -0.01 ? "↓" : "→";
  return `${arrow} ${perHour >= 0 ? "+" : ""}${perHour.toFixed(2)}/h`;
}

export function formatDateTime(ms: number): string {
  return new Date(ms).toLocaleString("en-US", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
