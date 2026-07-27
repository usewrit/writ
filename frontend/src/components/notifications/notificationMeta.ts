import {
  CheckBadgeIcon,
  XCircleIcon,
  BanknotesIcon,
  ArrowUturnLeftIcon,
  ExclamationTriangleIcon,
  WalletIcon,
  ArrowPathIcon,
  ShieldCheckIcon,
  BellIcon,
  ArchiveBoxXMarkIcon,
  CpuChipIcon,
} from '@heroicons/react/24/outline';

// Type → monochrome icon mapping for the inbox. Strings mirror the
// backend NotificationType values; unknown types fall back to a generic bell so
// new server-side types never render blank.
type IconType = typeof BellIcon;

const ICONS: Record<string, IconType> = {
  review_approved: CheckBadgeIcon,
  review_rejected: XCircleIcon,
  payout_paid: BanknotesIcon,
  payout_failed: ExclamationTriangleIcon,
  refund_issued: ArrowUturnLeftIcon,
  clawback_applied: ArrowUturnLeftIcon,
  run_failed: ExclamationTriangleIcon,
  low_balance: WalletIcon,
  update_available: ArrowPathIcon,
  account_security: ShieldCheckIcon,
  workflow_removed: ArchiveBoxXMarkIcon,
  agent_connected: CpuChipIcon,
};

export function iconForNotification(type: string): IconType {
  return ICONS[type] ?? BellIcon;
}
