import { useEffect, useRef, type MouseEvent as ReactMouseEvent } from 'react';
import {
  Flame,
  Gift,
  GitMerge,
  MoreVertical,
  Percent,
  ShieldCheck,
  SplitSquareHorizontal,
} from 'lucide-react';
import { Button } from '@ury/ui';
import { t } from '../i18n';

interface OrderActionsMenuProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  showSplitBill?: boolean;
  onSplitBill?: () => void;
  showMergeBill?: boolean;
  onMergeBill?: () => void;
  showComplimentary?: boolean;
  onComplimentary?: () => void;
  showTaxExempt?: boolean;
  onTaxExempt?: () => void;
  showServiceCharge?: boolean;
  onServiceCharge?: () => void;
  showRefireKot?: boolean;
  onRefireKot?: () => void;
}

const OrderActionsMenu = ({
  isOpen,
  onOpenChange,
  showSplitBill = false,
  onSplitBill,
  showMergeBill = false,
  onMergeBill,
  showComplimentary = false,
  onComplimentary,
  showTaxExempt = false,
  onTaxExempt,
  showServiceCharge = false,
  onServiceCharge,
  showRefireKot = false,
  onRefireKot,
}: OrderActionsMenuProps) => {
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;

    const handleClickOutside = (event: globalThis.MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        onOpenChange(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen, onOpenChange]);

  const hasAnyAction =
    showSplitBill ||
    showMergeBill ||
    showComplimentary ||
    showTaxExempt ||
    showServiceCharge ||
    showRefireKot;
  if (!hasAnyAction) {
    return null;
  }

  const handleToggle = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    onOpenChange(!isOpen);
  };

  const runAction = (callback?: () => void) => (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    onOpenChange(false);
    callback?.();
  };

  return (
    <div className="relative shrink-0" ref={menuRef}>
      <Button
        variant="ghost"
        size="icon"
        className="h-8 w-8 shrink-0 text-gray-600 hover:text-gray-900"
        onClick={handleToggle}
        aria-label={t('order.order_actions')}
        aria-expanded={isOpen}
      >
        <MoreVertical className="h-4 w-4" />
      </Button>

      {isOpen && (
        <div className="absolute end-0 top-full z-50 mt-1 w-56 rounded-lg border border-border bg-white py-1 shadow-lg">
          {showMergeBill && (
            <Button
              variant="ghost"
              className="flex h-auto min-h-10 w-full justify-start gap-2 rounded-none px-4 py-2 text-sm font-normal text-gray-700 hover:bg-gray-100"
              onClick={runAction(onMergeBill)}
            >
              <GitMerge className="h-4 w-4 shrink-0" />
              {t('bill_merge.merge_bill')}
            </Button>
          )}
          {showSplitBill && (
            <Button
              variant="ghost"
              className="flex h-auto min-h-10 w-full justify-start gap-2 rounded-none px-4 py-2 text-sm font-normal text-gray-700 hover:bg-gray-100"
              onClick={runAction(onSplitBill)}
            >
              <SplitSquareHorizontal className="h-4 w-4 shrink-0" />
              {t('bill_split.split_bill')}
            </Button>
          )}
          {showComplimentary && (
            <Button
              variant="ghost"
              className="flex h-auto min-h-10 w-full justify-start gap-2 rounded-none px-4 py-2 text-sm font-normal text-gray-700 hover:bg-gray-100"
              onClick={runAction(onComplimentary)}
            >
              <Gift className="h-4 w-4 shrink-0" />
              {t('privileged_actions.complimentary')}
            </Button>
          )}
          {showTaxExempt && (
            <Button
              variant="ghost"
              className="flex h-auto min-h-10 w-full justify-start gap-2 rounded-none px-4 py-2 text-sm font-normal text-gray-700 hover:bg-gray-100"
              onClick={runAction(onTaxExempt)}
            >
              <ShieldCheck className="h-4 w-4 shrink-0" />
              {t('privileged_actions.tax_exempt')}
            </Button>
          )}
          {showServiceCharge && (
            <Button
              variant="ghost"
              className="flex h-auto min-h-10 w-full justify-start gap-2 rounded-none px-4 py-2 text-sm font-normal text-gray-700 hover:bg-gray-100"
              onClick={runAction(onServiceCharge)}
            >
              <Percent className="h-4 w-4 shrink-0" />
              {t('privileged_actions.service_charge')}
            </Button>
          )}
          {showRefireKot && (
            <Button
              variant="ghost"
              className="flex h-auto min-h-10 w-full justify-start gap-2 rounded-none px-4 py-2 text-sm font-normal text-gray-700 hover:bg-gray-100"
              onClick={runAction(onRefireKot)}
            >
              <Flame className="h-4 w-4 shrink-0" />
              {t('privileged_actions.reprint_kot')}
            </Button>
          )}
        </div>
      )}
    </div>
  );
};

export default OrderActionsMenu;
