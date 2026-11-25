import { type MouseEvent } from 'react';
import { AlertTriangle, CheckCircle2, CreditCard, Eye, Loader2, Printer, Sparkles, Users } from 'lucide-react';
import { cn } from '@ury/ui';
import { formatInvoiceTime } from '@ury/core';
import type { Table, TableReservation } from '../lib/table-api';
import { Badge } from '@ury/ui';
import { TableShapeIcon } from './TableShapeIcon';
import TableActionsMenu from './TableActionsMenu';
import { formatReservationTime, isReservationLockWindowActive } from '../lib/table-utils';
import { t } from '../i18n';

export const TABLE_STATE_STYLES = {
  available: 'border-emerald-300 bg-emerald-50 text-emerald-900 hover:border-emerald-400 dark:bg-emerald-950 dark:text-emerald-100',
  occupied: 'border-amber-400 bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100',
  printed: 'border-indigo-500 bg-indigo-50 text-indigo-950 dark:bg-indigo-950 dark:text-indigo-100',
  dirty: 'border-rose-500 bg-rose-50 text-rose-950 dark:bg-rose-950 dark:text-rose-100',
  reserved: 'border-primary-400 bg-primary-50 text-primary-950 hover:border-primary-500',
} as const;

interface TableCardProps {
  table: Table;
  isReserved?: boolean;
  upcomingReservation?: TableReservation | null;
  activeReservation?: TableReservation | null;
  reservationEnabled?: boolean;
  mergeGroupLabel?: string;
  className?: string;
  menuOpen: boolean;
  onMenuOpenChange: (open: boolean) => void;
  onMerge: () => void;
  onUnmerge: () => void;
  onTransferTable?: () => void;
  onTransferCaptain?: () => void;
  showCaptainTransfer?: boolean;
  onNavigate: () => void;
  onPreview: (event: MouseEvent<HTMLButtonElement>) => void;
  onPrint: (event: MouseEvent<HTMLButtonElement>) => void;
  isPrinting: boolean;
  onReserve?: () => void;
  onMarkClean?: () => void;
  onPay?: () => void;
  allowDirtySeating?: boolean;
  serviceRequest?: { request_type: string; escalated: boolean; status?: string } | null;
  onResolveServiceRequest?: () => void;
}

const TableCard = ({
  table,
  isReserved = false,
  activeReservation = null,
  reservationEnabled = true,
  mergeGroupLabel,
  className,
  menuOpen,
  onMenuOpenChange,
  onMerge,
  onUnmerge,
  onTransferTable,
  onTransferCaptain,
  showCaptainTransfer = false,
  onNavigate,
  onPreview,
  onPrint,
  isPrinting,
  onReserve,
  onMarkClean,
  onPay,
  allowDirtySeating,
  serviceRequest = null,
  onResolveServiceRequest,
}: TableCardProps) => {
  const callingNow = serviceRequest && serviceRequest.status !== 'Acknowledged';
  const dirty = table.custom_table_state === 'Dirty';
  const printed = table.custom_table_state === 'Check Printed';
  const isOccupied = !dirty && (table.occupied === 1 || printed);
  const canNavigate = !isOccupied && !dirty;
  const isLockedByReservation =
    isReserved &&
    !!activeReservation &&
    (activeReservation.is_lock_window_active || isReservationLockWindowActive(activeReservation)) &&
    activeReservation.status === 'Confirmed';

  return (
    <div
      data-service-request={serviceRequest ? serviceRequest.request_type : undefined}
      role={canNavigate ? 'button' : 'group'}
      tabIndex={canNavigate ? 0 : -1}
      onKeyDown={(event) => {
        if (canNavigate && event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          onNavigate();
        }
      }}
      onClick={() => {
        if (canNavigate) {
          onNavigate();
        }
      }}
      className={cn(
        'relative flex min-h-[15.5rem] flex-col rounded-lg border-2 bg-white p-4 transition-colors',
        dirty ? TABLE_STATE_STYLES.dirty
          : printed ? TABLE_STATE_STYLES.printed
          : isOccupied ? TABLE_STATE_STYLES.occupied
          : isLockedByReservation
            ? cn(TABLE_STATE_STYLES.reserved, 'cursor-pointer hover:shadow-md')
            : cn(TABLE_STATE_STYLES.available, 'cursor-pointer hover:shadow-md'),
        menuOpen ? 'z-20' : 'z-0',
        callingNow && cn('animate-pulse ring-4', serviceRequest.escalated ? 'ring-red-600' : 'ring-amber-500'),
        className
      )}
    >
      <div className="flex flex-1 flex-col">
        <div className="mb-3 flex items-start justify-between gap-1">
          <div className="flex min-w-0 items-center gap-2">
            <div className="shrink-0">
              <TableShapeIcon
                shape={table.table_shape || 'Rectangle'}
                className={cn(
                  'h-5 w-5',
                  isOccupied ? 'text-amber-600' : isLockedByReservation ? 'text-primary-600' : 'text-gray-600'
                )}
              />
            </div>
            <span className="shrink-0 text-lg font-semibold text-current" title={mergeGroupLabel ?? table.name}>
              {table.name}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {dirty || printed ? (
              <Badge variant="outline" className="gap-1 text-current">
                {dirty ? <Sparkles size={14} aria-hidden /> : <Printer size={14} aria-hidden />}
                {t(dirty ? 'floor.dirty' : 'floor.check_printed')}
              </Badge>
            ) : isOccupied ? (
              <>
                <Badge variant="warning" className="whitespace-nowrap">
                  <Users size={14} className="me-1" aria-hidden />
                  {t(table.custom_table_state === 'Seated' ? 'floor.seated' : 'tables.occupied')}
                </Badge>
                {isLockedByReservation && (
                  <Badge
                    variant="outline"
                    className="whitespace-nowrap text-xs border-primary-200 bg-primary-100 text-primary-800 font-medium"
                  >
                    {t('tables.reserved_badge')}
                  </Badge>
                )}
              </>
            ) : isLockedByReservation ? (
              <Badge
                variant="outline"
                className="whitespace-nowrap text-xs border-primary-200 bg-primary-100 text-primary-800 font-medium"
              >
                {t('tables.reserved_badge')}
              </Badge>
            ) : (
              <Badge variant="success" className="whitespace-nowrap">
                <CheckCircle2 size={14} className="me-1" aria-hidden />
                {t('tables.available')}
              </Badge>
            )}
            <TableActionsMenu
              table={table}
              isOpen={menuOpen}
              onOpenChange={onMenuOpenChange}
              onMerge={onMerge}
              onUnmerge={onUnmerge}
              onTransferTable={onTransferTable}
              onTransferCaptain={onTransferCaptain}
              showCaptainTransfer={showCaptainTransfer}
              onReserve={reservationEnabled ? onReserve : undefined}
            />
          </div>
        </div>

        <p
          className={cn(
            'mb-2 min-h-[1.25rem] truncate text-xs font-medium',
            mergeGroupLabel && mergeGroupLabel !== table.name
              ? 'text-primary-700'
              : 'invisible'
          )}
          title={mergeGroupLabel ?? undefined}
        >
          {mergeGroupLabel && mergeGroupLabel !== table.name
            ? t('tables.merged_with_list', { tables: mergeGroupLabel })
            : ' '}
        </p>

        <div className="space-y-2 text-sm text-current">
          <div className="flex items-center justify-between">
            <span className="font-medium">{t('tables.room')}</span>
            <span>{table.restaurant_room}</span>
          </div>
          <div className="flex min-h-[1.25rem] items-center justify-between">
            <span className="font-medium">{t('tables.started_at')}</span>
            <span>{isOccupied ? formatInvoiceTime(table.latest_invoice_time) : '—'}</span>
          </div>
          {typeof table.no_of_seats === 'number' && (
            <div className="flex items-center justify-between">
              <span className="font-medium">{t('tables.seats')}</span>
              <span className="flex items-center gap-1">
                <Users className="h-3 w-3" />
                {table.no_of_seats}
              </span>
            </div>
          )}
          {table.is_take_away === 1 && (
            <Badge variant="pending" className="mt-2">
              {t('pos_ui.take_away')}
            </Badge>
          )}
          {isLockedByReservation && (
            <div className="mt-2 flex items-center gap-1.5 rounded-md border border-primary-200 bg-primary-50 px-2.5 py-1.5 text-xs font-medium text-primary-950">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-primary-600" />
              <span className="truncate">
                {t('tables.reserved_for', { time: formatReservationTime(activeReservation?.reserved_at) })}
              </span>
            </div>
          )}
        </div>
      </div>

      <div
        className={cn(
          'mt-auto flex min-h-[2.75rem] gap-2 border-t pt-3',
          isOccupied ? 'border-amber-200' : 'border-transparent'
        )}
      >
        {dirty && onMarkClean ? (
          <div className="flex flex-1 flex-wrap gap-2">
            <button type="button" className="min-h-11 flex-1 rounded-md border bg-background px-2 text-sm font-semibold"
              onClick={onMarkClean}>{t('floor.mark_clean')}</button>
            {allowDirtySeating && <button type="button" className="min-h-11 flex-1 rounded-md border bg-background px-2 text-sm font-semibold"
              onClick={onNavigate}>{t('floor.seat_party')}</button>}
          </div>
        ) : printed && onPay ? (
          <button type="button" className="flex min-h-11 flex-1 items-center justify-center gap-2 rounded-md border bg-background px-2 text-sm font-semibold"
            onClick={onPay}><CreditCard size={16} aria-hidden />{t('floor.pay')}</button>
        ) : isOccupied ? (
          <>
            <button
              onClick={onPreview}
              className="flex flex-1 items-center justify-center gap-2 rounded bg-background py-2 text-xs font-semibold text-foreground transition hover:bg-accent"
            >
              <Eye className="h-3 w-3" />
              {t('pos_ui.preview')}
            </button>
            <button
              onClick={onPrint}
              disabled={isPrinting}
              className="flex flex-1 items-center justify-center gap-2 rounded bg-background py-2 text-xs font-semibold text-foreground transition hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isPrinting ? (
                <>
                  <Loader2 className="h-3 w-3 animate-spin" />
                  {t('pos_ui.printing')}
                </>
              ) : (
                <>
                  <Printer className="h-3 w-3" />
                  {t('pos_ui.print')}
                </>
              )}
            </button>
          </>
        ) : null}
      </div>
      {serviceRequest?.status === 'Acknowledged' && onResolveServiceRequest && (
        <button
          type="button"
          onClick={(event) => { event.stopPropagation(); onResolveServiceRequest(); }}
          className="mt-2 min-h-11 rounded-md bg-emerald-600 px-3 text-sm font-bold text-white"
        >
          {t('service_requests.resolve')}
        </button>
      )}
    </div>
  );
};

export default TableCard;
