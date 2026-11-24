import TillLogout from './TillLogout';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  messageToPlainText,
} from '@ury/ui';
import { Button, Input } from '@ury/ui';
import { Spinner } from '@ury/ui';
import { call, db, formatCurrency, shouldMaskAmounts } from '@ury/core';
import { t } from '../i18n';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import ClosingPaymentTable from './ClosingPaymentTable';
import { isBlindCashCount } from '../lib/pos-closing-visibility';
import ChecklistGateDialog from './ChecklistGateDialog';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';
import { getChecklist } from '../lib/checklist-api';
import { getShiftCashMovementTotals } from '../lib/till-cash-api';
import {
  closeTillWithControls,
  forceCloseTill,
  getTillClosePreview,
  listOpenTills,
  loadActivePosReasonForClaim,
  type OpenTillRow,
  type TillClosePreview,
} from '../lib/till-controls-api';
import {
  getOpenPosOpeningEntries,
  getSubCashierPosInvoices,
  getMainCashierPosInvoices,
  createSubPosClosing,
  submitSubPosClosing,
  createPosClosingEntry,
  submitPosClosingEntry,
  type POSClosingInvoice,
  type POSClosingPayment,
  type ClosingPaymentSummary,
} from '../lib/pos-closing-api';

interface POSClosingDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * Extension point for the CLOSING checklist gate (task E3). Called after
   * the cashier clears the Closing checklist (post successful POS close
   * submission), right before the dialog itself closes.
   */
  onClosingSubmitted?: () => void | Promise<void>;
}

interface OpeningBalanceDetail {
  mode_of_payment: string;
  opening_amount: number;
}

interface OpeningEntryDoc {
  name: string;
  balance_details?: OpeningBalanceDetail[];
}

const MASKED = '—';
const NO_PAYMENT_MODES: string[] = [];

const pad = (value: number) => String(value).padStart(2, '0');

/** Formats a Date as 'YYYY-MM-DD HH:mm:ss', matching the backend's expected format. */
function formatDateTime(date: Date): string {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

function formatDate(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function formatTime(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * Fetches the Frappe server's current time via the whitelisted
 * `ury.ury.api.ury_server_time.get_server_time` endpoint, so the closing
 * period's end boundary is anchored to the same clock as `period_start_date`
 * (set server-side by POS Opening Entry). Falls back to the browser's local
 * clock if the call fails, so closing isn't blocked by an unrelated network
 * hiccup.
 *
 * The server returns `now_datetime().isoformat()`, e.g.
 * "2026-09-08T10:15:23.123456" -- already in the site's system timezone, no
 * client-side tz conversion needed. Microseconds are dropped before parsing
 * (mirrors pos_closing_entry.js) since `Date` parsing of
 * fractional seconds beyond milliseconds is inconsistent across browsers.
 */
async function getServerNow(): Promise<Date> {
  try {
    const response = await call.get<{ message: string }>(
      'ury.ury.api.ury_server_time.get_server_time'
    );
    const [datePart, timePartWithMicros] = response.message.split('T');
    const timePart = (timePartWithMicros || '00:00:00').split('.')[0];
    const parsed = new Date(`${datePart}T${timePart}`);
    if (Number.isNaN(parsed.getTime())) {
      throw new Error(`Unparseable server time: ${response.message}`);
    }
    return parsed;
  } catch (error) {
    console.error('Failed to fetch server time, falling back to local clock:', error);
    return new Date();
  }
}

/** Generic frappe-js-sdk fallbacks that carry no actionable detail. */
const GENERIC_ERROR_MESSAGES = new Set([
  'there was an error while updating the document.',
  'there was an error while creating the document.',
  'request failed with status code 500',
  'there was an error.',
  'internal server error',
]);

function readErrorProp(source: unknown, key: string): unknown {
  if (source && typeof source === 'object' && key in source) {
    return (source as Record<string, unknown>)[key];
  }
  return undefined;
}

/** Returns the substring after the first colon, or null when there is none. */
function detailAfterColon(text: string): string | null {
  const idx = text.indexOf(':');
  if (idx === -1) return null;
  return text.slice(idx + 1).trim() || null;
}

function combineWithFallback(fallback: string, detail: string): string {
  if (fallback && !detail.includes(fallback)) return `${fallback}: ${detail}`;
  return detail;
}

/**
 * Extracts a human-readable message from a Frappe API error. Frappe answers
 * errors in several shapes depending on where the exception was raised:
 * `_server_messages` (frappe.throw), `exception` / `exc` (uncaught exceptions
 * such as NegativeStockError, returned on HTTP 500 without _server_messages),
 * or a plain `message`. Each shape is checked on the error object directly
 * and on its axios `response.data` envelope. When a specific detail is found
 * it is prefixed with the fallback (e.g. "Failed to close POS: ..."), unless
 * the detail already contains it.
 */
function extractServerErrorMessage(error: unknown, fallback: string): string {
  const response = readErrorProp(error, 'response');
  const responseData =
    response && typeof response === 'object' ? readErrorProp(response, 'data') : undefined;
  const sources = [error, responseData];

  // a. _server_messages: JSON array of JSON-encoded { message: ... } entries.
  for (const source of sources) {
    const raw = readErrorProp(source, '_server_messages');
    if (typeof raw !== 'string') continue;
    try {
      const messages = JSON.parse(raw);
      const first = JSON.parse(messages[0]);
      if (first?.message) {
        return combineWithFallback(fallback, messageToPlainText(first.message as string));
      }
    } catch {
      // fall through to other extraction strategies
    }
  }

  // b. exception: "module.ClassName: human readable detail".
  for (const source of sources) {
    const raw = readErrorProp(source, 'exception');
    if (typeof raw !== 'string' || !raw.trim()) continue;
    const detail = detailAfterColon(raw);
    if (detail) return combineWithFallback(fallback, messageToPlainText(detail));
  }

  // c. exc: JSON array or newline-separated traceback; use its last line.
  for (const source of sources) {
    const raw = readErrorProp(source, 'exc');
    if (typeof raw !== 'string' || !raw.trim()) continue;
    let lastLine = raw;
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) {
        lastLine = String(parsed[parsed.length - 1]);
      }
    } catch {
      const lines = raw.trim().split('\n');
      lastLine = lines[lines.length - 1];
    }
    const detail = detailAfterColon(lastLine);
    if (detail) return combineWithFallback(fallback, messageToPlainText(detail));
  }

  // d. message: usable only when it isn't a generic SDK fallback string.
  for (const source of sources) {
    const raw = readErrorProp(source, 'message');
    if (typeof raw !== 'string' || !raw.trim()) continue;
    if (GENERIC_ERROR_MESSAGES.has(raw.trim().toLowerCase())) continue;
    return combineWithFallback(fallback, messageToPlainText(raw));
  }

  return fallback;
}

interface AggregatedTotals {
  onAccount: number;
  grandTotal: number;
  netTotal: number;
  totalQty: number;
}

/**
 * Aggregates raw POS Invoice dicts into closing totals and per-mode expected
 * amounts. When `paymentsByMode` is provided (pre-aggregated shape), those
 * payments already have change_amount deducted and are summed verbatim.
 * Otherwise expected amounts are accumulated per invoice, subtracting the
 * invoice's change_amount when the payment account matches
 * account_for_change_amount (mirroring sub_pos_closing.js). A card-only
 * invoice that paid change has no row on the change account, so its change
 * comes out of the profile mode the server maps to that account.
 */
function aggregateInvoices(
  invoices: POSClosingInvoice[],
  paymentsByMode?: POSClosingPayment[],
  paymentModes: string[] = [],
  paymentModeAccounts: Record<string, string | null> = {},
): {
  totals: AggregatedTotals;
  expectedByMode: Record<string, number>;
} {
  const totals: AggregatedTotals = { grandTotal: 0, netTotal: 0, totalQty: 0, onAccount: 0 };
  const expectedByMode: Record<string, number> = {};

  invoices.forEach((invoice) => {
    totals.onAccount += Number(invoice.outstanding_amount) || 0;
    totals.grandTotal += Number(invoice.grand_total) || 0;
    totals.netTotal += Number(invoice.net_total) || 0;
    totals.totalQty += Number(invoice.total_qty) || 0;

    if (!paymentsByMode) {
      const payments = invoice.payments || [];
      const hasChangePayment = payments.some(
        (payment) => payment.account === invoice.account_for_change_amount,
      );
      if (Number(invoice.change_amount) > 0 && !hasChangePayment) {
        const changeMode = paymentModes.find(
          (mode) => paymentModeAccounts[mode] === invoice.account_for_change_amount,
        );
        if (changeMode) {
          expectedByMode[changeMode] =
            (expectedByMode[changeMode] || 0) - Number(invoice.change_amount);
        }
      }
      payments.forEach((payment) => {
        let amount = Number(payment.amount) || 0;
        if (payment.account === invoice.account_for_change_amount) {
          amount -= Number(invoice.change_amount) || 0;
        }
        expectedByMode[payment.mode_of_payment] =
          (expectedByMode[payment.mode_of_payment] || 0) + amount;
      });
    }
  });

  if (paymentsByMode) {
    paymentsByMode.forEach((payment) => {
      expectedByMode[payment.mode_of_payment] =
        (expectedByMode[payment.mode_of_payment] || 0) + (Number(payment.amount) || 0);
    });
  }

  return { totals, expectedByMode };
}

/**
 * Blind count seeds rows only from the opening and the profile's cached
 * payment modes, so which rows appear never reveals sales activity.
 * Sales-only modes are returned separately for reconciliation.
 */
function buildRows(
  openingBalances: OpeningBalanceDetail[],
  expectedByMode: Record<string, number>,
  blindCashCount = false,
  profilePaymentModes: string[] = []
): { visible: ClosingPaymentSummary[]; hidden: ClosingPaymentSummary[] } {
  const rows = new Map<string, ClosingPaymentSummary>();
  const hidden = new Map<string, ClosingPaymentSummary>();
  const rowBalances = blindCashCount
    ? [
        ...openingBalances,
        ...profilePaymentModes.map((mode) => ({ mode_of_payment: mode, opening_amount: 0 })),
      ]
    : openingBalances;

  rowBalances.forEach((detail) => {
    if (rows.has(detail.mode_of_payment)) return;
    const opening = Number(detail.opening_amount) || 0;
    rows.set(detail.mode_of_payment, {
      mode_of_payment: detail.mode_of_payment,
      opening_amount: opening,
      // Matches ERPNext Desk / Sub POS Closing: expected_amount =
      // opening_amount + sales_amount.
      expected_amount: opening,
      closing_amount: 0,
      difference: 0,
    });
  });

  Object.entries(expectedByMode).forEach(([mode, sales]) => {
    const existing = rows.get(mode);
    if (existing) {
      existing.expected_amount += sales;
    } else {
      (blindCashCount ? hidden : rows).set(mode, {
        mode_of_payment: mode,
        opening_amount: 0,
        expected_amount: sales,
        closing_amount: 0,
        difference: 0,
      });
    }
  });

  return { visible: Array.from(rows.values()), hidden: Array.from(hidden.values()) };
}

const POSClosingDialog = ({ open, onOpenChange, onClosingSubmitted }: POSClosingDialogProps) => {
  const { posProfile, currency, paymentModes = NO_PAYMENT_MODES } = usePOSStore();
  const tillCurrency = posProfile?.currency || currency;
  const { user } = useRootStore();
  const blindCashCount = isBlindCashCount(posProfile);
  // Roles configured on the POS Profile do not see totals/expected amounts (display only).
  const maskAmounts = shouldMaskAmounts(user, posProfile);

  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [availableOpenTills, setAvailableOpenTills] = useState<OpenTillRow[]>([]);
  const [openingEntry, setOpeningEntry] = useState<OpenTillRow | null>(null);
  const [closePreview, setClosePreview] = useState<TillClosePreview | null>(null);
  const [isSubCashier, setIsSubCashier] = useState(false);
  const [periodEndDate, setPeriodEndDate] = useState<Date>(new Date());
  const [invoiceCount, setInvoiceCount] = useState(0);
  const [totals, setTotals] = useState<AggregatedTotals>({ grandTotal: 0, netTotal: 0, totalQty: 0, onAccount: 0 });
  const [rows, setRows] = useState<ClosingPaymentSummary[]>([]);
  // Sales-only tenders hidden by blind count; reconciled at zero count on submit.
  const [hiddenRows, setHiddenRows] = useState<ClosingPaymentSummary[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [pendingCashMovements, setPendingCashMovements] = useState(0);
  const [overShortReason, setOverShortReason] = useState('');
  const [blindVarianceMessage, setBlindVarianceMessage] = useState<string | null>(null);
  const [showForceClose, setShowForceClose] = useState(false);
  const [forceCloseReason, setForceCloseReason] = useState('');
  const [discardPendingOnForce, setDiscardPendingOnForce] = useState(false);
  const [closeOtherToken, setCloseOtherToken] = useState<string | null>(null);
  const [closeOtherReason, setCloseOtherReason] = useState<string | null>(null);
  const [pendingCloseApproval, setPendingCloseApproval] = useState<null | {
    kind: 'over_short_close' | 'close_other_till' | 'force_close';
    claimReason: string;
    needsPicker: boolean;
  }>(null);
  // Set once the close submission (Sub POS Closing / POS Closing Entry) has
  // succeeded. Per plan decision D8, the Closing checklist gate hangs off
  // this success -- not off validatePOSClose() -- so it renders in place of
  // the closing form until the cashier clears it.
  const [showClosingChecklist, setShowClosingChecklist] = useState(false);
  // True when the Closing checklist gate fired BEFORE the close submission.
  // The grillax validations reject POS Closing Entry creation while
  // role-based Closing goals are pending, so in this mode completing the
  // checklist must resume the close submission (and the post-close skip
  // hatch must not render -- the checklist is not optional here).
  const [preCloseGate, setPreCloseGate] = useState(false);
  // Modes the cashier has explicitly entered a closing amount for (Fix 2:
  // a blank input must not silently count as "reviewed and zero").
  const [touchedModes, setTouchedModes] = useState<Set<string>>(new Set());
  // Tracks a create-but-not-yet-submitted Sub POS Closing / POS Closing
  // Entry doc name so a retry after a failed submit re-submits the same
  // doc instead of creating a duplicate draft (Fix 3). Cleared only on
  // final success or explicit cancel/dismiss of the dialog.
  const [pendingCloseDoc, setPendingCloseDoc] = useState<{
    name: string;
    kind: 'sub' | 'main';
  } | null>(null);
  // Lightweight "Are you sure?" step before the irreversible submit call
  // actually fires (Fix 6).
  const [showConfirm, setShowConfirm] = useState(false);

  const loadEntryDetails = useCallback(
    async (selectedEntry: OpenTillRow) => {
      if (!posProfile || !user) return;
      setOpeningEntry(selectedEntry);
      setBlindVarianceMessage(null);
      setCloseOtherToken(null);
      setCloseOtherReason(null);
      setOverShortReason('');
      setShowForceClose(false);
      setForceCloseReason('');
      setDiscardPendingOnForce(false);

      const closingOther = selectedEntry.user !== user.name;
      let subCashier = false;
      if (!closingOther && posProfile.multiple_cashier === 1) {
        try {
          const profileStatus = await call.get<{
            message: { owner: string; multiple_cashier: number };
          }>('ury.ury_pos.api.getPosProfile');
          subCashier =
            profileStatus.message.multiple_cashier === 1 &&
            profileStatus.message.owner !== user.name;
        } catch (statusError) {
          console.error('Failed to resolve main-cashier status, falling back to cached profile:', statusError);
          subCashier = posProfile.owner !== user.name;
        }
      }
      setIsSubCashier(subCashier);

      const now = await getServerNow();
      setPeriodEndDate(now);
      const end = formatDateTime(now);

      const [invoiceResult, openingDoc, cashMovements, preview] = await Promise.all([
        subCashier
          ? getSubCashierPosInvoices(selectedEntry.period_start_date, end, posProfile.name, selectedEntry.user)
          : getMainCashierPosInvoices(selectedEntry.period_start_date, end, posProfile.name, selectedEntry.user),
        db.getDoc<OpeningEntryDoc>('POS Opening Entry', selectedEntry.name),
        subCashier
          ? Promise.resolve({ by_mode: {}, pending_count: 0 })
          : getShiftCashMovementTotals(selectedEntry.name),
        subCashier
          ? Promise.resolve(null)
          : getTillClosePreview(posProfile.name, selectedEntry.name),
      ]);

      setClosePreview(preview);
      const invoices = Array.isArray(invoiceResult) ? invoiceResult : (invoiceResult?.invoices || []);
      const payments = Array.isArray(invoiceResult) ? undefined : invoiceResult?.payments;
      setInvoiceCount(invoices.length);
      setPendingCashMovements(cashMovements.pending_count);
      const { totals: aggregatedTotals, expectedByMode } = aggregateInvoices(
        invoices,
        payments,
        paymentModes,
        posProfile.payment_mode_accounts,
      );
      Object.entries(cashMovements.by_mode).forEach(([mode, delta]) => {
        const movementAmount = Number(delta) || 0;
        if (movementAmount !== 0) {
          expectedByMode[mode] = (expectedByMode[mode] || 0) + movementAmount;
        }
      });
      setTotals(aggregatedTotals);
      const { visible, hidden } = buildRows(
        openingDoc?.balance_details ?? [],
        expectedByMode,
        blindCashCount,
        paymentModes,
      );
      setRows(visible);
      setHiddenRows(hidden);
      setTouchedModes(new Set());
    },
    [posProfile, user, blindCashCount, paymentModes],
  );

  const loadClosingDetails = useCallback(async () => {
    if (!posProfile || !user) return;

    setIsLoading(true);
    setLoadError(null);
    setSubmitError(null);

    try {
      const listedTills = await listOpenTills(posProfile.name);
      const openEntries: OpenTillRow[] =
        listedTills && listedTills.length > 0
          ? listedTills
          : (await getOpenPosOpeningEntries(posProfile.name)).map((e) => ({
              ...e,
              is_own: e.user === user.name,
            }));

      setAvailableOpenTills(openEntries);
      const ownEntry = openEntries.find((entry) => entry.user === user.name) ?? null;
      const chosenEntry = ownEntry ?? openEntries[0] ?? null;

      if (!chosenEntry) {
        setOpeningEntry(null);
        setClosePreview(null);
        setLoadError(t('pos_closing.no_open_entry'));
        return;
      }

      await loadEntryDetails(chosenEntry);
    } catch (error) {
      console.error('Failed to load POS closing details:', error);
      setLoadError(extractServerErrorMessage(error, t('pos_closing.load_failed')));
    } finally {
      setIsLoading(false);
    }
  }, [posProfile, user, loadEntryDetails]);

  useEffect(() => {
    if (!open) return;
    setSubmitError(null);
    loadClosingDetails();
  }, [open, loadClosingDetails]);

  const handleRowChange = (modeOfPayment: string, closingAmount: number) => {
    setRows((prev) =>
      prev.map((row) =>
        row.mode_of_payment === modeOfPayment ? { ...row, closing_amount: closingAmount } : row
      )
    );
    setTouchedModes((prev) => {
      if (prev.has(modeOfPayment)) return prev;
      const next = new Set(prev);
      next.add(modeOfPayment);
      return next;
    });
  };

  const handleOpenChange = (next: boolean) => {
    if (isSubmitting) return;
    if (!next) {
      // Explicit cancel/dismiss (Fix 3): a create-but-not-submitted draft
      // is intentionally left as-is server-side, but we stop tracking it
      // here since the user is walking away from this attempt.
      setPendingCloseDoc(null);
      setShowConfirm(false);
      setPreCloseGate(false);
      setPendingCloseApproval(null);
      setBlindVarianceMessage(null);
      setShowForceClose(false);
    }
    onOpenChange(next);
  };

  const isClosingOther = Boolean(
    openingEntry && user && openingEntry.user !== user.name,
  );

  const effectiveTolerance: number | null = useMemo(() => {
    if (typeof closePreview?.over_short_tolerance === 'number') {
      return closePreview.over_short_tolerance;
    }
    if (typeof posProfile?.over_short_tolerance === 'number') {
      return posProfile.over_short_tolerance;
    }
    return null;
  }, [closePreview?.over_short_tolerance, posProfile?.over_short_tolerance]);

  const cashVarianceInfo = useMemo(() => {
    if (blindCashCount || effectiveTolerance === null) {
      return { exceeds: false, maxAbsDiff: 0 };
    }
    const modeMeta = posProfile?.payment_mode_meta ?? {};
    const hasMeta = Object.keys(modeMeta).length > 0;
    let maxAbsDiff = 0;
    for (const row of rows) {
      const metaType = modeMeta[row.mode_of_payment]?.type;
      const isCash = hasMeta
        ? metaType
          ? metaType === 'Cash'
          : /^cash$/i.test(row.mode_of_payment)
        : true;
      if (!isCash) continue;
      const absDiff = Math.abs(row.closing_amount - row.expected_amount);
      if (absDiff > maxAbsDiff) maxAbsDiff = absDiff;
    }
    return {
      exceeds: maxAbsDiff > effectiveTolerance + 1e-6,
      maxAbsDiff,
    };
  }, [blindCashCount, effectiveTolerance, posProfile?.payment_mode_meta, rows]);

  const countedPaymentsPayload = useMemo(
    () =>
      [...rows, ...hiddenRows].map((row) => ({
        mode_of_payment: row.mode_of_payment,
        closing_amount: row.closing_amount,
      })),
    [rows, hiddenRows],
  );

  const useControlledClose =
    !isSubCashier && (effectiveTolerance !== null || blindCashCount || isClosingOther);

  // Closing may be submitted with blank or zero amount fields. Blank inputs
  // count as 0; rows that had activity but a zero close only show a warning.
  const validation = useMemo(() => {
    if (rows.length === 0) {
      return { isValid: false, blockingMessage: null as string | null, warningMessage: null as string | null };
    }

    const zeroRowsWithActivity = rows.filter(
      (row) =>
        touchedModes.has(row.mode_of_payment) &&
        row.closing_amount === 0 &&
        // Blind count never consults sales-derived expected amounts.
        (row.opening_amount !== 0 || (!blindCashCount && row.expected_amount !== 0))
    );

    const warningMessage =
      zeroRowsWithActivity.length > 0 ? t('pos_closing.validation_zero_row_warning') : null;

    return { isValid: true, blockingMessage: null as string | null, warningMessage };
  }, [rows, touchedModes, blindCashCount]);

  const openCloseApproval = async (
    kind: 'close_other_till' | 'over_short_close' | 'force_close',
  ) => {
    if (kind === 'close_other_till') {
      setPendingCloseApproval({ kind, claimReason: '', needsPicker: false });
      return;
    }
    const privilege = kind === 'over_short_close' ? 'over_short_close' : 'reopen_till';
    const raw = kind === 'over_short_close' ? overShortReason.trim() : forceCloseReason.trim();
    const resolved = await loadActivePosReasonForClaim(privilege, raw);
    setPendingCloseApproval({
      kind,
      claimReason: resolved.claimReason,
      needsPicker: resolved.needsPicker,
    });
  };

  const handleRequestSubmit = () => {
    if (!validation.isValid) return;
    setSubmitError(null);
    if (isClosingOther && !closeOtherToken) {
      void openCloseApproval('close_other_till');
      return;
    }
    if (cashVarianceInfo.exceeds) {
      if (!overShortReason.trim()) {
        setSubmitError(t('pos_closing.over_short_reason_required'));
        return;
      }
      void openCloseApproval('over_short_close');
      return;
    }
    setShowConfirm(true);
  };

  const executeControlledClose = async (tokens?: {
    overShortToken?: string;
    overShortReasonOverride?: string;
    closeOtherToken?: string;
    closeOtherReasonOverride?: string;
  }) => {
    if (!openingEntry || !posProfile || !user) return;
    if (posProfile.name) {
      try {
        const { logStatus } = await getChecklist(posProfile.name, 'Closing');
        if (logStatus !== 'Complete') {
          setPreCloseGate(true);
          setShowClosingChecklist(true);
          return;
        }
      } catch (error) {
        console.error('Failed to check closing checklist status:', error);
        setPreCloseGate(true);
        setShowClosingChecklist(true);
        return;
      }
    }

    setShowConfirm(false);
    setIsSubmitting(true);
    setSubmitError(null);
    setBlindVarianceMessage(null);

    try {
      await closeTillWithControls({
        pos_profile: posProfile.name,
        pos_opening_entry: openingEntry.name,
        counted_payments: countedPaymentsPayload,
        over_short_reason: tokens?.overShortReasonOverride || overShortReason.trim() || undefined,
        over_short_approval_token: tokens?.overShortToken,
        close_other_approval_token: tokens?.closeOtherToken ?? closeOtherToken ?? undefined,
        close_other_reason: tokens?.closeOtherReasonOverride ?? closeOtherReason ?? undefined,
        period_end_date: formatDateTime(periodEndDate),
      });
      setPendingCloseDoc(null);
      setCloseOtherToken(null);
      setCloseOtherReason(null);

      if (posProfile?.name) {
        try {
          const { logStatus } = await getChecklist(posProfile.name, 'Closing');
          if (logStatus !== 'Complete') {
            setShowClosingChecklist(true);
          } else {
            await onClosingSubmitted?.();
            onOpenChange(false);
          }
        } catch (error) {
          console.error('Failed to check closing checklist status:', error);
          setShowClosingChecklist(true);
        }
      } else {
        await onClosingSubmitted?.();
        onOpenChange(false);
      }
    } catch (error) {
      const rawMsg = extractServerErrorMessage(error, t('pos_closing.submit_failed'));
      const rawText = String(readErrorProp(error, 'message') ?? rawMsg);
      if (
        rawText.includes('POS-CLOSE-VARIANCE-APPROVAL') ||
        rawMsg.includes('POS-CLOSE-VARIANCE-APPROVAL') ||
        /outside this till's allowed variance/i.test(rawMsg)
      ) {
        setBlindVarianceMessage(
          rawText.includes('POS-CLOSE-VARIANCE-APPROVAL')
            ? messageToPlainText(rawText)
            : rawMsg,
        );
      } else {
        setSubmitError(rawMsg);
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleConfirmSubmit = async () => {
    if (!openingEntry || !posProfile || !user) return;
    if (useControlledClose) {
      await executeControlledClose();
      return;
    }

    // Gate on the Closing checklist BEFORE creating the closing doc: the
    // role-based Dependent Checklist validations (grillax port) throw on
    // POS Closing Entry creation while pending goals have no Quality
    // Review, so collecting the checklist first is the only UI path that
    // lets the cashier actually clear the block.
    if (posProfile.name) {
      try {
        const { logStatus } = await getChecklist(posProfile.name, 'Closing');
        if (logStatus !== 'Complete') {
          setPreCloseGate(true);
          setShowClosingChecklist(true);
          return;
        }
      } catch (error) {
        console.error('Failed to check closing checklist status:', error);
        setPreCloseGate(true);
        setShowClosingChecklist(true);
        return;
      }
    }

    setShowConfirm(false);
    setIsSubmitting(true);
    setSubmitError(null);

    const paymentReconciliation = [...rows, ...hiddenRows].map((row) => ({
      mode_of_payment: row.mode_of_payment,
      opening_amount: row.opening_amount,
      expected_amount: row.expected_amount,
      closing_amount: row.closing_amount,
      // Positive = overage, negative = shortage. Must match the display
      // convention in ClosingPaymentTable (Fix 1).
      difference: row.closing_amount - row.expected_amount,
    }));

    // Fix 3: if a previous attempt already created the draft doc but the
    // submit call then failed, reuse that doc instead of creating another
    // draft on retry.
    const kind: 'sub' | 'main' = pendingCloseDoc?.kind ?? (isSubCashier ? 'sub' : 'main');

    try {
      let docName = pendingCloseDoc?.name;

      if (!docName) {
        if (kind === 'sub') {
          const doc = await createSubPosClosing({
            pos_profile: posProfile.name,
            pos_opening_entry: openingEntry.name,
            user: user.name,
            period_start_date: openingEntry.period_start_date,
            period_end_date: formatDateTime(periodEndDate),
            payment_reconciliation: paymentReconciliation,
            grand_total: totals.grandTotal,
            net_total: totals.netTotal,
            total_quantity: totals.totalQty,
          });
          docName = doc.name;
        } else {
          const doc = await createPosClosingEntry({
            pos_profile: posProfile.name,
            pos_opening_entry: openingEntry.name,
            user: user.name,
            company: posProfile.company,
            period_start_date: openingEntry.period_start_date,
            period_end_date: formatDateTime(periodEndDate),
            posting_date: formatDate(periodEndDate),
            posting_time: formatTime(periodEndDate),
            payment_reconciliation: paymentReconciliation,
            grand_total: totals.grandTotal,
            net_total: totals.netTotal,
            total_quantity: totals.totalQty,
          });
          docName = doc.name;
        }
        // Persist immediately -- if the submit call below throws, a retry
        // must skip straight to submitting this doc rather than creating
        // a second draft.
        setPendingCloseDoc({ name: docName, kind });
      }

      if (kind === 'sub') {
        await submitSubPosClosing(docName);
      } else {
        await submitPosClosingEntry(docName);
      }

      // Final success -- stop tracking the draft.
      setPendingCloseDoc(null);

      // Close submission succeeded -- gate on the Closing checklist before
      // notifying the parent and dismissing the dialog.
      if (posProfile?.name) {
        try {
          const { logStatus } = await getChecklist(posProfile.name, 'Closing');
          if (logStatus !== 'Complete') {
            setShowClosingChecklist(true);
          } else {
            await onClosingSubmitted?.();
            onOpenChange(false);
          }
        } catch (error) {
          console.error('Failed to check closing checklist status:', error);
          setShowClosingChecklist(true);
        }
      } else {
        await onClosingSubmitted?.();
        onOpenChange(false);
      }
    } catch (error) {
      console.error('Failed to submit POS closing:', error);
      setSubmitError(extractServerErrorMessage(error, t('pos_closing.submit_failed')));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleClosingChecklistComplete = async () => {
    setShowClosingChecklist(false);
    await onClosingSubmitted?.();
    onOpenChange(false);
  };

  // Pre-close gate: the close submission hasn't fired yet, so finishing the
  // checklist resumes it instead of dismissing the dialog.
  const handlePreCloseChecklistComplete = () => {
    setShowClosingChecklist(false);
    setPreCloseGate(false);
    void handleConfirmSubmit();
  };

  // Fix 5: the underlying POS Closing doc is already submitted by the time
  // the checklist gate renders -- completing the checklist itself is just
  // post-close housekeeping, so give the cashier a way out instead of
  // trapping them if they can't finish it right now.
  const handleSkipClosingChecklist = async () => {
    setShowClosingChecklist(false);
    await onClosingSubmitted?.();
    onOpenChange(false);
  };

  const handleManagerApprovedClose = async (result: ManagerApprovalResult) => {
    const pending = pendingCloseApproval;
    setPendingCloseApproval(null);
    if (!pending || !openingEntry || !posProfile) return;

    if (pending.kind === 'close_other_till') {
      if (cashVarianceInfo.exceeds) {
        setCloseOtherToken(result.token);
        setCloseOtherReason(result.reason || null);
        if (!overShortReason.trim()) {
          setSubmitError(t('pos_closing.over_short_reason_required'));
          return;
        }
        await openCloseApproval('over_short_close');
        return;
      }
      await executeControlledClose({
        closeOtherToken: result.token,
        closeOtherReasonOverride: result.reason,
      });
      return;
    }

    if (pending.kind === 'over_short_close') {
      await executeControlledClose({
        overShortToken: result.token,
        overShortReasonOverride: result.reason || pending.claimReason || overShortReason.trim(),
      });
      return;
    }

    if (pending.kind === 'force_close') {
      setIsSubmitting(true);
      setSubmitError(null);
      try {
        await forceCloseTill({
          pos_profile: posProfile.name,
          pos_opening_entry: openingEntry.name,
          counted_payments: countedPaymentsPayload,
          reason: result.reason || pending.claimReason || forceCloseReason.trim(),
          discard_pending_movements: discardPendingOnForce,
          approval_token: result.token,
          over_short_reason: overShortReason.trim() || undefined,
          period_end_date: formatDateTime(periodEndDate),
        });
        setShowForceClose(false);
        await onClosingSubmitted?.();
        onOpenChange(false);
      } catch (error) {
        setSubmitError(extractServerErrorMessage(error, t('pos_closing.submit_failed')));
      } finally {
        setIsSubmitting(false);
      }
    }
  };

  const closeApprovalProps = useMemo(() => {
    if (!pendingCloseApproval || !openingEntry || !posProfile) return null;
    if (pendingCloseApproval.kind === 'close_other_till') {
      return {
        privilege: 'close_other_till',
        reference: { doctype: 'POS Opening Entry', name: openingEntry.name },
        requireReason: pendingCloseApproval.needsPicker ? true : undefined,
        summaryLines: [
          t('pos_closing.other_till_notice', {
            cashier: openingEntry.user_name || openingEntry.user,
          }),
          openingEntry.name,
        ],
        claim: {
          action: 'close_other_till',
          pos_profile: posProfile.name,
          pos_opening_entry: openingEntry.name,
          opening_user: openingEntry.user,
        },
      };
    }
    if (pendingCloseApproval.kind === 'over_short_close') {
      const effectiveReason = pendingCloseApproval.claimReason;
      return {
        privilege: 'over_short_close',
        reference: { doctype: 'POS Opening Entry', name: openingEntry.name },
        amount: cashVarianceInfo.maxAbsDiff || undefined,
        requireReason: pendingCloseApproval.needsPicker ? true : undefined,
        summaryLines: [
          t('pos_closing.blind_variance_title'),
          `${t('pos_closing.over_short_reason')}: ${effectiveReason || overShortReason.trim()}`,
        ],
        claim: {
          action: 'over_short_close',
          pos_profile: posProfile.name,
          pos_opening_entry: openingEntry.name,
          counted_payments: countedPaymentsPayload,
          reason: effectiveReason,
        },
      };
    }
    const effectiveForceReason = pendingCloseApproval.claimReason;
    return {
      privilege: 'reopen_till',
      reference: { doctype: 'POS Opening Entry', name: openingEntry.name },
      requireReason: pendingCloseApproval.needsPicker ? true : undefined,
      summaryLines: [
        t('pos_closing.force_close_title'),
        `${t('pos_closing.force_close_reason')}: ${effectiveForceReason || forceCloseReason.trim()}`,
      ],
      claim: {
        action: 'force_close',
        pos_profile: posProfile.name,
        pos_opening_entry: openingEntry.name,
        reason: effectiveForceReason,
        discard_pending_movements: discardPendingOnForce,
      },
    };
  }, [
    pendingCloseApproval,
    openingEntry,
    posProfile,
    cashVarianceInfo.maxAbsDiff,
    overShortReason,
    countedPaymentsPayload,
    forceCloseReason,
    discardPendingOnForce,
  ]);

  const hasRows = useMemo(() => rows.length > 0, [rows]);

  if (showClosingChecklist && posProfile?.name) {
    return (
      <>
        <ChecklistGateDialog
          posProfile={posProfile.name}
          checklistType="Closing"
          onComplete={
            preCloseGate ? handlePreCloseChecklistComplete : handleClosingChecklistComplete
          }
        />
        {/*
          Fix 5: the POS Closing doc is already submitted at this point --
          ChecklistGateDialog is otherwise a non-dismissible full-screen
          overlay (z-50) with no way out except finishing every mandatory
          item. Render an escape hatch above it so the cashier isn't
          stranded if they can't complete the checklist right now. Not
          rendered for the pre-close gate: there the checklist is the
          hard precondition for creating the closing doc at all.
        */}
        {!preCloseGate && (
          <div className="fixed inset-x-0 bottom-6 z-[60] flex justify-center px-4">
            <div className="flex flex-wrap items-center justify-center gap-3 rounded-lg border border-border bg-white px-4 py-3 shadow-xl">
              <p className="text-sm text-gray-700">{t('pos_closing.checklist_skip_hint')}</p>
              <Button variant="outline" size="sm" onClick={handleSkipClosingChecklist}>
                {t('pos_closing.checklist_skip')}
              </Button>
            </div>
          </div>
        )}
      </>
    );
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        variant="xlarge"
        size="4xl"
        onClose={() => handleOpenChange(false)}
        className="max-h-[90vh] overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle>{t('pos_closing.title')}</DialogTitle>
          <DialogDescription>{t('pos_closing.description')}</DialogDescription>
        </DialogHeader>

        <div className="px-6 pb-4">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Spinner message={t('pos_closing.loading')} />
            </div>
          ) : loadError ? (
            <p className="py-8 text-center text-sm text-red-600">{loadError}</p>
          ) : (
            <>
              {(availableOpenTills.length > 1 || isClosingOther) && openingEntry && (
                <div className="mb-4 rounded-lg border border-border bg-gray-50 p-3 space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <label htmlFor="pos-closing-till-select" className="text-xs font-semibold text-gray-700">
                      {t('pos_closing.select_till')}
                    </label>
                    <span className="text-sm font-medium text-gray-900">
                      {openingEntry.user_name || openingEntry.user} · {openingEntry.name}
                    </span>
                  </div>
                  {availableOpenTills.length > 1 && (
                    <select
                      id="pos-closing-till-select"
                      value={openingEntry.name}
                      disabled={isSubmitting}
                      onChange={(e) => {
                        const found = availableOpenTills.find((entry) => entry.name === e.target.value);
                        if (found) void loadEntryDetails(found);
                      }}
                      className="min-h-11 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900"
                    >
                      {availableOpenTills.map((entry) => (
                        <option key={entry.name} value={entry.name}>
                          {(entry.user_name || entry.user)} — {entry.name}
                        </option>
                      ))}
                    </select>
                  )}
                  {isClosingOther && (
                    <p className="text-xs font-medium text-amber-800">
                      {t('pos_closing.other_till_notice', {
                        cashier: openingEntry.user_name || openingEntry.user,
                      })}
                    </p>
                  )}
                </div>
              )}

              {!blindCashCount && (
              <div className="mb-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
                <div className="rounded-lg border border-border p-3">
                  <p className="text-xs text-gray-500">{t('pos_closing.on_account')}</p>
                  <p>{maskAmounts ? MASKED : formatCurrency(totals.onAccount, tillCurrency)}</p>
                </div>
                <div className="rounded-lg border border-border p-3">
                  <p className="text-xs text-gray-500">{t('pos_closing.grand_total')}</p>
                  <p className="text-lg font-semibold text-gray-900">
                    {maskAmounts ? MASKED : formatCurrency(totals.grandTotal, tillCurrency)}
                  </p>
                </div>
                <div className="rounded-lg border border-border p-3">
                  <p className="text-xs text-gray-500">{t('pos_closing.net_total')}</p>
                  <p className="text-lg font-semibold text-gray-900">
                    {maskAmounts ? MASKED : formatCurrency(totals.netTotal, tillCurrency)}
                  </p>
                </div>
                <div className="rounded-lg border border-border p-3">
                  <p className="text-xs text-gray-500">{t('pos_closing.total_qty')}</p>
                  <p className="text-lg font-semibold text-gray-900">{totals.totalQty}</p>
                </div>
                <div className="rounded-lg border border-border p-3">
                  <p className="text-xs text-gray-500">{t('pos_closing.total_invoices')}</p>
                  <p className="text-lg font-semibold text-gray-900">{invoiceCount}</p>
                </div>
              </div>
              )}

              {hasRows ? (
                <ClosingPaymentTable
                  currency={tillCurrency}
                  rows={rows}
                  touchedModes={touchedModes}
                  onChange={handleRowChange}
                  hideExpected={maskAmounts}
                  blindCashCount={blindCashCount}
                />
              ) : (
                <p className="py-8 text-center text-sm text-gray-500">
                  {t('pos_closing.no_invoices')}
                </p>
              )}

              {hasRows && pendingCashMovements > 0 && (
                <p className="mt-3 text-sm font-medium text-amber-700">
                  {t('pos_closing.pending_cash_movements', { count: pendingCashMovements })}
                </p>
              )}
              {hasRows && validation.blockingMessage && (
                <p className="mt-3 text-sm font-medium text-amber-700">
                  {validation.blockingMessage}
                </p>
              )}
              {hasRows && !validation.blockingMessage && validation.warningMessage && (
                <p className="mt-3 text-sm font-medium text-amber-700">
                  {validation.warningMessage}
                </p>
              )}

              {closePreview && closePreview.no_sale_count > 0 && (
                <p className="mt-2 text-xs text-gray-600">
                  {t('pos_closing.no_sale_summary', { count: closePreview.no_sale_count })}
                </p>
              )}

              {hasRows && cashVarianceInfo.exceeds && !blindCashCount && (
                <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3.5 space-y-2.5">
                  <p className="text-sm font-medium text-amber-900">
                    {t('pos_closing.variance_exceeds_tolerance', {
                      tolerance: formatCurrency(effectiveTolerance ?? 0, posProfile?.currency || undefined),
                    })}
                  </p>
                  <div className="space-y-1">
                    <label
                      htmlFor="pos-closing-over-short-reason"
                      className="text-xs font-medium text-amber-950"
                    >
                      {t('pos_closing.over_short_reason')}
                    </label>
                    <Input
                      id="pos-closing-over-short-reason"
                      type="text"
                      value={overShortReason}
                      onChange={(e) => setOverShortReason(e.target.value)}
                      placeholder={t('pos_closing.over_short_reason_placeholder')}
                      disabled={isSubmitting}
                    />
                  </div>
                </div>
              )}

              {blindVarianceMessage && (
                <div
                  role="alert"
                  className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4 space-y-3"
                >
                  <p className="text-sm font-medium text-amber-950">{blindVarianceMessage}</p>
                  <div className="space-y-1">
                    <label
                      htmlFor="pos-closing-over-short-reason"
                      className="text-xs font-medium text-amber-950"
                    >
                      {t('pos_closing.over_short_reason')}
                    </label>
                    <Input
                      id="pos-closing-over-short-reason"
                      type="text"
                      value={overShortReason}
                      onChange={(e) => setOverShortReason(e.target.value)}
                      placeholder={t('pos_closing.over_short_reason_placeholder')}
                      disabled={isSubmitting}
                    />
                  </div>
                  <div className="flex flex-wrap gap-2 justify-end">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="min-h-11"
                      onClick={() => setBlindVarianceMessage(null)}
                    >
                      {t('pos_closing.recount')}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      className="min-h-11"
                      disabled={!overShortReason.trim() || isSubmitting}
                      onClick={() => void openCloseApproval('over_short_close')}
                    >
                      {t('pos_closing.approve_variance_pin')}
                    </Button>
                  </div>
                </div>
              )}

              {showForceClose && openingEntry && !isSubCashier && (
                <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 space-y-3">
                  <h4 className="text-sm font-semibold text-red-900">
                    {t('pos_closing.force_close_title')}
                  </h4>
                  {closePreview && closePreview.open_checks_count > 0 && (
                    <p className="text-sm text-red-800">
                      {t('pos_closing.force_close_open_checks', {
                        count: closePreview.open_checks_count,
                      })}
                    </p>
                  )}
                  {(pendingCashMovements > 0 || (closePreview?.pending_movements_count ?? 0) > 0) && (
                    <label className="flex items-center gap-2 text-xs text-red-900">
                      <input
                        type="checkbox"
                        checked={discardPendingOnForce}
                        onChange={(e) => setDiscardPendingOnForce(e.target.checked)}
                      />
                      <span>
                        {t('pos_closing.force_close_discard_pending', {
                          count: Math.max(
                            pendingCashMovements,
                            closePreview?.pending_movements_count ?? 0,
                          ),
                        })}
                      </span>
                    </label>
                  )}
                  <div className="space-y-1">
                    <label
                      htmlFor="pos-closing-force-reason"
                      className="text-xs font-medium text-red-950"
                    >
                      {t('pos_closing.force_close_reason')}
                    </label>
                    <Input
                      id="pos-closing-force-reason"
                      type="text"
                      value={forceCloseReason}
                      onChange={(e) => setForceCloseReason(e.target.value)}
                      placeholder={t('pos_closing.force_close_reason_placeholder')}
                      disabled={isSubmitting}
                    />
                  </div>
                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="min-h-11"
                      onClick={() => setShowForceClose(false)}
                    >
                      {t('common.cancel')}
                    </Button>
                    <Button
                      type="button"
                      variant="destructive"
                      size="sm"
                      className="min-h-11"
                      disabled={!forceCloseReason.trim() || isSubmitting}
                      onClick={() => void openCloseApproval('force_close')}
                    >
                      {t('pos_closing.approve_force_close')}
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {submitError && (
          <p className="px-6 pb-2 text-sm font-medium text-red-600 break-words">{submitError}</p>
        )}

        <DialogFooter>
          <TillLogout />
          {!isSubCashier && openingEntry && (
            <Button
              type="button"
              variant="outline"
              onClick={() => setShowForceClose((prev) => !prev)}
              disabled={isSubmitting || isLoading}
            >
              {t('pos_closing.force_close')}
            </Button>
          )}
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={isSubmitting}>
            {t('common.cancel')}
          </Button>
          <Button
            onClick={handleRequestSubmit}
            disabled={isSubmitting || isLoading || !!loadError || !openingEntry || !hasRows || !validation.isValid}
          >
            {isSubmitting ? t('pos_closing.submitting') : t('pos_closing.submit')}
          </Button>
        </DialogFooter>
      </DialogContent>

      {closeApprovalProps && posProfile?.name && (
        <ManagerApproval
          open={Boolean(pendingCloseApproval)}
          posProfile={posProfile.name}
          privilege={closeApprovalProps.privilege}
          summaryLines={closeApprovalProps.summaryLines}
          reference={closeApprovalProps.reference}
          claim={closeApprovalProps.claim}
          amount={'amount' in closeApprovalProps ? closeApprovalProps.amount : undefined}
          requireReason={closeApprovalProps.requireReason}
          onApproved={(res) => void handleManagerApprovedClose(res)}
          onCancel={() => setPendingCloseApproval(null)}
        />
      )}

      {showConfirm && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/50 px-4">
          <div className="w-full max-w-sm rounded-lg bg-white p-6 shadow-xl">
            <h3 className="text-lg font-semibold text-gray-900">
              {t('pos_closing.confirm_title')}
            </h3>
            <p className="mt-2 text-sm text-gray-600">{t('pos_closing.confirm_message')}</p>
            <div className="mt-6 flex justify-end gap-3">
              <Button
                variant="outline"
                onClick={() => setShowConfirm(false)}
                disabled={isSubmitting}
              >
                {t('pos_closing.confirm_back')}
              </Button>
              <Button onClick={handleConfirmSubmit} disabled={isSubmitting}>
                {isSubmitting ? t('pos_closing.submitting') : t('pos_closing.confirm_submit')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </Dialog>
  );
};

export default POSClosingDialog;
