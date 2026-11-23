import { FileText } from 'lucide-react';
import { cn } from '@ury/ui';
import { Button } from '@ury/ui';
import { getOrderStatusTypes, OrderStatusType } from '../data/order-types';
import { usePOSStore } from '../store/pos-store';
import { t } from '../i18n';

interface OrderStatusSidebarProps {
  disabled?: boolean;
  selectedStatus: OrderStatusType;
  setSelectedStatus: (status: OrderStatusType) => void;
  /** Open checks in a queue; undefined (not shown) when unknown or not counted. */
  getStatusCount?: (status: OrderStatusType) => number | undefined;
}

const OrderStatusSidebar = ({ 
  disabled,
  selectedStatus,
  setSelectedStatus,
  getStatusCount,
}: OrderStatusSidebarProps) => {
  const { posProfile } = usePOSStore();
  
  // Get the appropriate status types based on POS profile settings
  const statusTypes = getOrderStatusTypes(posProfile?.view_all_status, posProfile?.paid_limit);

  return (
    // Fills whatever column the page gives it: a fixed width here overflowed
    // the narrower Orders column and let the active pill bleed past the card.
    <div className={cn(
      "box-border flex h-full w-full min-w-0 flex-col border-e border-border bg-white",
      disabled && "opacity-50 pointer-events-none"
    )}>
      <nav className="min-w-0 flex-1 overflow-y-auto p-3">
        <div className="box-border w-full min-w-0 rounded-lg border border-border bg-gray-50 p-2">
          {/* Section Title */}
          <h2 className="mb-2 truncate px-2 text-xs font-medium uppercase tracking-wide text-gray-500">
            {t('orders.status_title')}
          </h2>

          {/* Status Items: a column beside the queue on a desktop; under lg,
              where this rail spans the page, a wrapping row of pills. */}
          <div className="flex min-w-0 flex-row flex-wrap gap-1 lg:flex-col lg:flex-nowrap">
            {statusTypes.map((status) => {
              const label = t(`order_status_types.${status.value.toLowerCase().replace(/ /g, '_')}`);
              const isActive = selectedStatus === status.value;
              const count = getStatusCount?.(status.value as OrderStatusType);
              return (
                <Button
                  key={status.value}
                  onClick={() => setSelectedStatus(status.value as OrderStatusType)}
                  variant="ghost"
                  className={cn(
                    'relative box-border flex h-auto min-h-10 w-auto min-w-0 items-center justify-start gap-3 px-3 py-2.5 text-start text-sm font-medium transition-all duration-200 lg:w-full',
                    isActive
                      ? 'bg-white text-gray-900 shadow-sm font-semibold'
                      : 'text-gray-700 hover:bg-white/60 hover:text-gray-900'
                  )}
                  disabled={disabled}
                >
                  {/* Active indicator bar */}
                  {isActive && (
                    <div className="absolute start-0 top-1/2 -translate-y-1/2 w-1 h-6 bg-blue-600 rounded-e-full" />
                  )}
                  {/* The count rides on the icon, so the label keeps the pill's width. */}
                  <span className="relative shrink-0">
                    <FileText className="h-4 w-4 text-gray-500" />
                    {count !== undefined && (
                      <span className="absolute -end-3 -top-2 min-w-4 rounded-full bg-gray-800 px-1 text-center text-[10px] font-semibold leading-4 tabular-nums text-white">
                        {count}
                      </span>
                    )}
                  </span>
                  {/* Wraps rather than an ellipsis: a touch screen has no hover tooltip. */}
                  <span className="min-w-0 whitespace-normal break-words">{label}</span>
                </Button>
              );
            })}
          </div>
        </div>
      </nav>
    </div>
  );
};

export default OrderStatusSidebar; 