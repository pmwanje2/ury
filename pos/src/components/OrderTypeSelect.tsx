import { useState } from 'react';
import { usePOSStore } from '../store/pos-store';
import { useRootStore } from '../store/root-store';
import { cn } from '@ury/ui';
import { Button, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@ury/ui';
import TableSelectionDialog from './TableSelectionDialog';
import { DEFAULT_ORDER_TYPE, DINE_IN, ORDER_TYPES , type OrderType} from '../data/order-types';
import { HandPlatter } from 'lucide-react';
import { isUserRestrictedFromTableOrders } from '@ury/core';
import { formatMergedTableLabel } from '../lib/table-utils';
import { t } from '../i18n';

interface OrderTypeSelectProps {
  disabled?: boolean;
}

const OrderTypeSelect = ({ disabled }: OrderTypeSelectProps) => {
  const { selectedOrderType, setSelectedOrderType, selectedTable, tableOrder, posProfile, isUpdatingOrder, activeOrders } = usePOSStore();
  const { user } = useRootStore();
  const [showTableDialog, setShowTableDialog] = useState(false);
  const [pendingType, setPendingType] = useState<OrderType | null>(null);

  // Check if user is restricted from table orders
  const isRestrictedFromTableOrders = isUserRestrictedFromTableOrders(user, posProfile);

  const applyOrderType = (type: OrderType) => {
    setSelectedOrderType(type);
    if (type === DINE_IN) {
      setShowTableDialog(true);
    }
  };

  const handleOrderTypeSelect = (type: OrderType) => {
    // Prevent selecting "Dine In" if user is restricted
    if (type === DINE_IN && isRestrictedFromTableOrders) {
      return;
    }
    // Re-tapping the current type is not a change: never clear the cart for it.
    if (type === selectedOrderType) {
      if (type === DINE_IN) setShowTableDialog(true);
      return;
    }
    // A new check's type change clears the cart: ask first (an order being
    // updated only reaches here on the in-place edit_order_type path).
    if (activeOrders.length > 0 && !isUpdatingOrder) {
      setPendingType(type);
      return;
    }
    applyOrderType(type);
  };

  const handleTableDialogClose = () => {
    setShowTableDialog(false);
    // Use a timeout to allow state to update before checking
    setTimeout(() => {
      const currentState = usePOSStore.getState();
      if (currentState.selectedOrderType === DINE_IN && !currentState.selectedTable) {
        setSelectedOrderType(DEFAULT_ORDER_TYPE);
      }
    }, 100);
  };

  const tableDisplayLabel =
    selectedTable && tableOrder?.message?.restaurant_table
      ? formatMergedTableLabel(
          tableOrder.message.restaurant_table,
          tableOrder.message.custom_merged_tables
        )
      : selectedTable;

  return (
    <div>
      <div className="flex gap-2 overflow-x-auto pb-2 -mx-2 px-2">
        {ORDER_TYPES.map(({ value, icon: Icon }) => {
          const isDineIn = value === DINE_IN;
          
          let isUpdatingDisabled = isUpdatingOrder;
          if (isUpdatingOrder && posProfile?.edit_order_type) {
            const isCurrentTypeToggleable = selectedOrderType === 'Take Away' || selectedOrderType === 'Delivery';
            const isValueToggleable = value === 'Take Away' || value === 'Delivery';
            if (isCurrentTypeToggleable && isValueToggleable) {
              isUpdatingDisabled = false;
            }
          }

          const isDisabled = disabled || (isDineIn && isRestrictedFromTableOrders) || isUpdatingDisabled;
          
          return (
            <Button
              key={value}
              onClick={() => handleOrderTypeSelect(value)}
              variant={selectedOrderType === value ? 'default' : 'outline'}
              className={cn(
                'h-fit flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium whitespace-nowrap bg-white border transition-colors',
                selectedOrderType === value
                ? 'text-primary-700 bg-primary-50 border-primary-600 hover:bg-primary-50'
                : 'text-gray-700 border-gray-200 hover:bg-gray-50',
                isDisabled && 'opacity-50 cursor-not-allowed'
              )}
              disabled={isDisabled}
              title={isDineIn && isRestrictedFromTableOrders ? t('errors.dine_in_restricted') || 'Dine In is not available for your role' : undefined}
            >
              <Icon className="w-4 h-4" />
              {t(`order_types.${value.toLowerCase().replace(/ /g, '_')}`)}
            </Button>
          );
        })}
      </div>

      {selectedOrderType === DINE_IN && selectedTable && (
        <Button
          onClick={() => setShowTableDialog(true)}
          variant="ghost"
          className="h-fit w-fit gap-x-2 mt-2 text-sm text-primary-600 hover:text-primary-700"
          disabled={disabled}
        >
          <HandPlatter className="w-4 h-4" /> {tableDisplayLabel}
        </Button>
      )}

      {showTableDialog && (
        <TableSelectionDialog onClose={handleTableDialogClose} />
      )}

      <Dialog open={!!pendingType} onOpenChange={(open) => !open && setPendingType(null)}>
        <DialogContent size="sm" showCloseButton onClose={() => setPendingType(null)}>
          <DialogHeader>
            <DialogTitle>{t('order.change_type_title')}</DialogTitle>
            <DialogDescription>
              {pendingType && t('order.change_type_message', {
                type: t(`order_types.${pendingType.toLowerCase().replace(/ /g, '_')}`),
                count: activeOrders.length,
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPendingType(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                if (pendingType) applyOrderType(pendingType);
                setPendingType(null);
              }}
            >
              {t('order.change_type_confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default OrderTypeSelect; 