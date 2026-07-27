import { formatMoneyMicros } from '../utils/money';

/**
 * Credit / wallet shim (self-host build).
 *
 * The self-host coordinator has NO wallet, credits, or plan metering — runs are
 * never charged. The full cloud CreditProvider (balance polling, 402 upgrade
 * modals, spend-confirm) is gone. This module keeps only the tiny surface that
 * shared wizard/detail components still import:
 *   - `formatUsd` / `formatUsdShort` — plain USD formatters for cost-estimate
 *     labels (e.g. an AI step's rough per-run cost).
 *   - `AI_COSTS` — a static per-run estimate map (display only).
 *   - `useCredits` — always null (no provider), which every consumer already
 *     handles gracefully.
 */

export const formatUsd = (micros: number | null | undefined): string =>
  formatMoneyMicros(micros);

export const formatUsdShort = (micros: number | null | undefined): string =>
  formatMoneyMicros(micros, { short: true });

// Per-run AI cost estimates in MICRO-USD (display only — never charged).
export const AI_COSTS = {
  standard: 250_000,       // $0.25
  intelligent: 1_000_000,  // $1.00
  api_discovery: 2_000_000, // $2.00
  ai_assist: 50_000,       // $0.05
} as const;

// Context shape kept only so the (few) consumers that read
// `ctx.credits.aiCosts` / call `ctx.confirmSpend(...)` typecheck. In the
// self-host build there is no wallet, so `useCredits()` is always null and
// those branches never run.
interface CreditState {
  balance: number;
  aiCosts: Record<string, number>;
  loaded: boolean;
}
interface CreditContextValue {
  credits: CreditState;
  refresh: () => Promise<void>;
  confirmSpend: (cost: number, label: string) => Promise<boolean>;
}

export function useCredits(): CreditContextValue | null {
  return null;
}
