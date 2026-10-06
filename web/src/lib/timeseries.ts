// Stesso principio del demone per i buchi di campionamento (monitor.py: un tick conta come
// "buco" se il tempo trascorso supera 3 volte il periodo atteso — es. sospensione del PC).
// Qui la cadenza attesa non e' nota a priori (varia per intervallo/tabella), quindi si usa
// la mediana dei delta della serie stessa: robusta a qualche buco senza bisogno di
// hardcodare una cadenza per intervallo.
const GAP_THRESHOLD_MULTIPLIER = 3;

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2 : (sorted[mid] ?? 0);
}

/**
 * Inserisce una riga con valori `null` nei punti dove il tempo tra due campioni consecutivi
 * e' molto piu' grande del solito, cosi' il grafico interrompe davvero la linea invece di
 * disegnare un segmento retto che collega due momenti lontani (es. PC in sospensione) come
 * se fosse un dato reale.
 */
export function withGapBreaks<T extends Record<string, unknown>>(
  rows: T[],
  xKey: string,
  valueKeys: string[],
): Record<string, unknown>[] {
  if (rows.length < 3) {
    return rows;
  }

  const deltas: number[] = [];
  for (let i = 1; i < rows.length; i++) {
    deltas.push((rows[i][xKey] as number) - (rows[i - 1][xKey] as number));
  }
  const typicalDelta = median(deltas);
  if (!(typicalDelta > 0)) {
    return rows;
  }
  const threshold = typicalDelta * GAP_THRESHOLD_MULTIPLIER;

  const result: Record<string, unknown>[] = [rows[0] as Record<string, unknown>];
  for (let i = 1; i < rows.length; i++) {
    const prevTs = rows[i - 1][xKey] as number;
    const currTs = rows[i][xKey] as number;
    if (currTs - prevTs > threshold) {
      const gapRow: Record<string, unknown> = { [xKey]: prevTs + (currTs - prevTs) / 2 };
      for (const key of valueKeys) {
        gapRow[key] = null;
      }
      result.push(gapRow);
    }
    result.push(rows[i] as Record<string, unknown>);
  }
  return result;
}
