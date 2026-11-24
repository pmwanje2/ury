import React from 'react';
import { ClosingPaymentSummary } from '../lib/pos-closing-api';
import { Input } from '@ury/ui';
import { formatCurrency } from '@ury/core';
import { cn } from '@ury/ui';
import { t } from '../i18n';

interface ClosingPaymentTableProps {
  rows: ClosingPaymentSummary[];
  /** Modes the cashier has explicitly entered a closing amount for (see Fix 2). */
  touchedModes: Set<string>;
  onChange: (modeOfPayment: string, closingAmount: number) => void;
  /** Physical count in native foreign tender currency for FX modes (G22). */
  onForeignChange?: (modeOfPayment: string, closingForeignAmount: number) => void;
  /** Mask Expected and Difference (display only; values are still computed and submitted). */
  hideExpected?: boolean;
  /** Blind cash count: omit the Expected and Difference columns entirely. */
  blindCashCount?: boolean;
  /** POS Profile currency code for formatting foreign-currency tills accurately. */
  currency?: string;
}

function fxStepForCurrency(code?: string | null): string {
  if (!code) return '0.01';
  try {
    const digits = new Intl.NumberFormat('en', { style: 'currency', currency: code }).resolvedOptions()
      .maximumFractionDigits ?? 2;
    return digits === 0 ? '1' : digits === 3 ? '0.001' : '0.01';
  } catch {
    return '0.01';
  }
}

const ClosingPaymentTable: React.FC<ClosingPaymentTableProps> = ({
  rows,
  touchedModes,
  onChange,
  onForeignChange,
  hideExpected = false,
  blindCashCount = false,
  currency,
}) => {
  const handleClosingAmountChange = (modeOfPayment: string, value: string) => {
    const parsed = parseFloat(value);
    // Clamp to non-negative in JS -- the HTML `min="0"` attribute alone does
    // not stop programmatic or pasted negative input.
    const closingAmount = Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
    onChange(modeOfPayment, closingAmount);
  };

  const handleForeignAmountChange = (row: ClosingPaymentSummary, value: string) => {
    const parsed = parseFloat(value);
    const fxAmount = Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
    if (onForeignChange) {
      onForeignChange(row.mode_of_payment, fxAmount);
    } else {
      const rate =
        row.effective_exchange_rate ||
        (row.custom_expected_foreign_amount && row.custom_expected_foreign_amount > 0
          ? row.expected_amount / row.custom_expected_foreign_amount
          : 1);
      onChange(row.mode_of_payment, fxAmount * rate);
    }
  };

  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-border bg-gray-50">
            <th className="text-start py-3 px-4 font-semibold text-gray-900">
              {t('pos_closing.payment_mode')}
            </th>
            <th className="text-end py-3 px-4 font-semibold text-gray-900">
              {t('pos_closing.opening')}
            </th>
            {!blindCashCount && (
            <th className="text-end py-3 px-4 font-semibold text-gray-900">
              <span
                title={t('pos_closing.help_expected')}
                className="inline-flex items-center gap-1 cursor-help"
              >
                {t('pos_closing.expected')}
                <span
                  aria-hidden="true"
                  className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-gray-400 text-[10px] leading-none text-gray-500"
                >
                  i
                </span>
              </span>
            </th>
            )}
            <th className="text-center py-3 px-4 font-semibold text-gray-900">
              <span
                title={t('pos_closing.help_closing')}
                className="inline-flex items-center gap-1 cursor-help"
              >
                {t('pos_closing.closing')}
                <span
                  aria-hidden="true"
                  className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-gray-400 text-[10px] leading-none text-gray-500"
                >
                  i
                </span>
              </span>
            </th>
            {!blindCashCount && (
            <th className="text-end py-3 px-4 font-semibold text-gray-900">
              <span
                title={t('pos_closing.help_difference')}
                className="inline-flex items-center gap-1 cursor-help"
              >
                {t('pos_closing.difference')}
                <span
                  aria-hidden="true"
                  className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-gray-400 text-[10px] leading-none text-gray-500"
                >
                  i
                </span>
              </span>
            </th>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            // Positive = overage (cashier has more than expected), negative
            // = shortage. Must match the sign convention used in the submit
            // payload built by POSClosingDialog.handleSubmit.
            const fxCurrency =
              row.custom_tender_currency && row.custom_tender_currency !== currency
                ? row.custom_tender_currency
                : null;
            const expectedFx = Number(row.custom_expected_foreign_amount) || 0;
            const closingFx = Number(row.custom_closing_foreign_amount) || 0;
            const foreignDiff = closingFx - expectedFx;
            const difference = row.closing_amount - row.expected_amount;
            const hasDifference = fxCurrency ? Math.abs(foreignDiff) > 0.001 : Math.abs(difference) > 0.001;
            const isTouched = touchedModes.has(row.mode_of_payment);

            return (
              <tr
                key={row.mode_of_payment}
                className={cn(
                  'border-b border-border hover:bg-gray-50 transition-colors',
                  !isTouched && 'bg-amber-50/60'
                )}
              >
                <td className="py-3 px-4 text-gray-900 font-medium">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span>{row.mode_of_payment}</span>
                    {fxCurrency && (
                      <span className="inline-flex items-center rounded bg-blue-50 px-1.5 py-0.5 text-xs font-semibold text-blue-700 border border-blue-200">
                        {fxCurrency}
                      </span>
                    )}
                  </div>
                </td>
                <td className="py-3 px-4 text-end text-gray-700">
                  {formatCurrency(row.opening_amount, currency)}
                </td>
                {!blindCashCount && (
                <td className="py-3 px-4 text-end text-gray-700">
                  {hideExpected ? (
                    '—'
                  ) : fxCurrency ? (
                    <div className="tabular-nums">
                      <div className="font-medium">{formatCurrency(expectedFx, fxCurrency)}</div>
                      <div className="text-xs text-gray-500">
                        {t('pos_closing.fx_base_equivalent', { amount: formatCurrency(row.expected_amount, currency) })}
                      </div>
                    </div>
                  ) : (
                    formatCurrency(row.expected_amount, currency)
                  )}
                </td>
                )}
                <td className="py-3 px-4">
                  {fxCurrency ? (
                    <div className="space-y-1">
                      <Input
                        data-testid={`closing-fx-input-${row.mode_of_payment}`}
                        aria-label={t('pos_closing.fx_native_count', { currency: fxCurrency })}
                        type="number"
                        min="0"
                        step={fxStepForCurrency(fxCurrency)}
                        value={closingFx > 0 ? String(closingFx) : ''}
                        onChange={(e) => handleForeignAmountChange(row, e.target.value)}
                        placeholder={t('pos_closing.fx_native_count', { currency: fxCurrency })}
                        className={cn('w-full text-center', !isTouched && 'border-amber-400')}
                        size="sm"
                      />
                      <div className="text-center text-xs text-gray-500 tabular-nums">
                        {formatCurrency(row.closing_amount, currency)}
                      </div>
                    </div>
                  ) : (
                    <Input
                      type="number"
                      min="0"
                      step="0.01"
                      value={row.closing_amount > 0 ? String(row.closing_amount) : ''}
                      onChange={(e) =>
                        handleClosingAmountChange(row.mode_of_payment, e.target.value)
                      }
                      placeholder="0.00"
                      className={cn('w-full text-center', !isTouched && 'border-amber-400')}
                      size="sm"
                    />
                  )}
                </td>
                {!blindCashCount && (
                <td
                  className={cn(
                    'py-3 px-4 text-end font-medium',
                    hideExpected
                      ? 'text-gray-700'
                      : hasDifference
                        ? 'text-red-600'
                        : 'text-green-600'
                  )}
                >
                  {hideExpected ? (
                    '—'
                  ) : fxCurrency ? (
                    <div className="tabular-nums">
                      <div>{formatCurrency(foreignDiff, fxCurrency)}</div>
                      <div className="text-xs opacity-80">{formatCurrency(difference, currency)}</div>
                    </div>
                  ) : (
                    formatCurrency(difference, currency)
                  )}
                </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};

export default ClosingPaymentTable;
