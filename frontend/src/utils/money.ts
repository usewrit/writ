/**
 * Money formatting (self-host build).
 *
 * The self-host coordinator has no wallet/credit ledger — costs are never
 * charged. These helpers survive only so cost-estimate labels (e.g. an AI
 * step's rough per-run cost, still emitted by the shared wizard/detail
 * components) render as plain USD. All amounts are MICRO-USD (1,000,000 = $1.00).
 */

export function formatMoneyMicros(
  micros: number | null | undefined,
  opts?: { short?: boolean },
): string {
  const m = typeof micros === 'number' && Number.isFinite(micros) ? micros : 0;
  const dollars = m / 1_000_000;
  if (opts?.short) {
    return `$${dollars.toFixed(2)}`;
  }
  // Sub-cent amounts keep extra precision so they don't collapse to "$0.00".
  if (dollars !== 0 && Math.abs(dollars) < 0.01) {
    return `$${dollars.toFixed(4)}`;
  }
  return `$${dollars.toFixed(2)}`;
}

/** Alias kept for call sites that imported `formatMicros`. */
export const formatMicros = (micros: number | null | undefined): string =>
  formatMoneyMicros(micros);
