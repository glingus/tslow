export const shortDateFmt = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
});

export const weekdayDateFmt = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  month: "short",
  day: "numeric",
});

export const shortTimeFmt = new Intl.DateTimeFormat("en-US", {
  hour: "2-digit",
  minute: "2-digit",
});

export const shortDateTimeFmt = new Intl.DateTimeFormat("en-US", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export const weekdayDateTimeFmt = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export const hmsTimeFmt = new Intl.DateTimeFormat("en-US", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

// `Intl.NumberFormat.prototype.format` is a bound getter — safe to extract.
export const intFmt = new Intl.NumberFormat("en-US").format;

const TWO_DAYS_MS = 2 * 24 * 60 * 60 * 1000;

/**
 * Sotto ~2 giorni di ampiezza visibile mostra solo l'orario, senza la data: mettere anche la
 * data ("Sep 19, 09:46 PM") raddoppiava la larghezza dell'etichetta e le faceva sovrapporre
 * appena la finestra del browser non era larghissima. Il solo orario basta a leggere il
 * grafico (la data si vede nel tooltip al passaggio del mouse, dove non c'e' affollamento).
 * Su un intervallo di settimane/mesi l'orario sarebbe rumore, resta solo la data.
 */
export function formatAxisLabel(date: Date, spanMs: number): string {
  return spanMs <= TWO_DAYS_MS ? shortTimeFmt.format(date) : shortDateFmt.format(date);
}

export function formatTooltipTitle(date: Date, spanMs: number): string {
  return spanMs <= TWO_DAYS_MS ? weekdayDateTimeFmt.format(date) : weekdayDateFmt.format(date);
}
