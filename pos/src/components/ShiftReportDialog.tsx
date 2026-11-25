import { useCallback, useEffect, useState } from 'react';
import { ArrowLeftRight, Printer, RefreshCw } from 'lucide-react';
import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Spinner,
} from '@ury/ui';
import { formatCurrency } from '@ury/core';
import { getShiftXReport, type ShiftReportResponse } from '../lib/outlet-api';
import { t } from '../i18n';

interface ShiftReportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  posProfile?: string | null;
  posOpeningEntry?: string | null;
}

const ShiftReportDialog = ({
  open,
  onOpenChange,
  posProfile,
  posOpeningEntry,
}: ShiftReportDialogProps) => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<ShiftReportResponse | null>(null);

  const loadReport = useCallback(async () => {
    if (!posProfile) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getShiftXReport(posProfile, posOpeningEntry);
      setReport(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('shift_report.load_failed'));
    } finally {
      setLoading(false);
    }
  }, [posProfile, posOpeningEntry]);

  useEffect(() => {
    if (!open) return;
    void loadReport();
  }, [open, loadReport]);

  const columns = report?.columns ?? [];
  const rows = report?.rows ?? [];
  const summary = report?.report_summary ?? [];
  const reportCurrency = report?.currency || undefined;

  const renderCell = (value: unknown, fieldtype?: string) => {
    if (value === null || value === undefined || value === '') return '—';
    if (fieldtype === 'Currency' && typeof value === 'number') {
      return formatCurrency(value, reportCurrency);
    }
    return String(value);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        variant="xlarge"
        size="4xl"
        onClose={() => onOpenChange(false)}
        className="flex max-h-[90vh] flex-col overflow-hidden"
      >
        <DialogHeader className="shrink-0">
          <DialogTitle>
            {report?.report_type === 'Z'
              ? t('shift_report.z_report_title')
              : t('shift_report.x_report_title')}
          </DialogTitle>
          <DialogDescription>{t('shift_report.description')}</DialogDescription>
        </DialogHeader>

        <div
          data-testid="shift-report-scroll-body"
          className="flex-1 min-h-0 overflow-y-auto px-6 pb-4 space-y-4"
        >
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {report?.pos_profile && (
              <Badge variant="secondary" size="sm">
                {report.pos_profile}
              </Badge>
            )}
            {report?.business_date && (
              <Badge variant="info" size="sm">
                {t('header.business_date_badge', { date: report.business_date })}
              </Badge>
            )}
            {report?.pos_opening_entry && (
              <Badge variant="secondary" size="sm">
                {report.pos_opening_entry}
              </Badge>
            )}
          </div>

          {loading ? (
            <div className="py-12 flex justify-center">
              <Spinner message={t('shift_report.loading')} />
            </div>
          ) : error ? (
            <p className="py-6 text-center text-sm text-red-600">{error}</p>
          ) : !report?.available ? (
            <p className="py-6 text-center text-sm text-gray-500">
              {t('shift_report.unavailable')}
            </p>
          ) : report.has_open_shift === false ? (
            <p className="py-6 text-center text-sm text-gray-500">
              {t('shift_report.no_open_shift')}
            </p>
          ) : (
            <>
              {summary.length > 0 && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                  {summary.map((item, idx) => (
                    <div
                      key={`${item.label ?? idx}`}
                      className="rounded-lg border border-border bg-gray-50 p-3"
                    >
                      <p className="text-xs text-gray-500 truncate">{item.label}</p>
                      <p className="text-base font-semibold text-gray-900 mt-0.5">
                        {item.datatype === 'Currency' && typeof item.value === 'number'
                          ? formatCurrency(item.value, item.currency || reportCurrency)
                          : String(item.value ?? '—')}
                      </p>
                    </div>
                  ))}
                </div>
              )}

              {columns.length > 0 && rows.length > 0 ? (
                <div className="space-y-1.5">
                  {columns.length > 4 && (
                    <p
                      data-testid="shift-report-scroll-hint"
                      className="flex items-center justify-end gap-1.5 text-xs text-gray-500"
                    >
                      <ArrowLeftRight className="w-3.5 h-3.5 shrink-0" />
                      <span>{t('shift_report.scroll_hint')}</span>
                    </p>
                  )}
                  <div
                    data-testid="shift-report-table-scroll"
                    className="w-full overflow-x-auto rounded-lg border border-border"
                  >
                    <table className="min-w-max w-full border-collapse text-sm">
                      <thead>
                        <tr className="border-b border-border bg-gray-50">
                          {columns.map((col) => (
                            <th
                              key={col.fieldname}
                              className="py-2.5 px-3.5 text-start font-semibold text-gray-800 whitespace-nowrap"
                            >
                              {col.label || col.fieldname}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((row, rowIdx) => (
                          <tr key={rowIdx} className="border-b border-border last:border-b-0">
                            {columns.map((col, colIdx) => {
                              const val = Array.isArray(row)
                                ? row[colIdx]
                                : (row as Record<string, unknown>)[col.fieldname];
                              return (
                                <td
                                  key={col.fieldname}
                                  className="py-2 px-3.5 text-gray-700 whitespace-nowrap"
                                >
                                  {renderCell(val, col.fieldtype)}
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : (
                <p className="py-4 text-center text-sm text-gray-500">
                  {t('shift_report.no_rows')}
                </p>
              )}
            </>
          )}
        </div>

        <DialogFooter className="shrink-0 border-t border-border bg-card px-4 py-3 sm:px-6 sm:py-4 flex-wrap">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={() => void loadReport()}
          >
            <RefreshCw className="w-3.5 h-3.5 me-1.5" />
            {t('common.refresh')}
          </Button>
          {report?.available && report.has_open_shift !== false && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => window.print()}
            >
              <Printer className="w-3.5 h-3.5 me-1.5" />
              {t('shift_report.print')}
            </Button>
          )}
          <Button type="button" onClick={() => onOpenChange(false)}>
            {t('common.dismiss')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default ShiftReportDialog;
