import { useState } from 'react';
import { Store, ChevronDown, Check, AlertTriangle } from 'lucide-react';
import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@ury/ui';
import { usePOSStore } from '../store/pos-store';
import type { AvailableOutlet } from '../lib/outlet-api';
import { t } from '../i18n';

interface OutletPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mandatory?: boolean;
}

export const OutletPickerDialog = ({
  open,
  onOpenChange,
  mandatory = false,
}: OutletPickerDialogProps) => {
  const posProfile = usePOSStore((state) => state.posProfile);
  const availableOutlets = usePOSStore((state) => state.availableOutlets) ?? [];
  const activeOrders = usePOSStore((state) => state.activeOrders) ?? [];
  const switchOutlet = usePOSStore((state) => state.switchOutlet);

  const [pendingTarget, setPendingTarget] = useState<AvailableOutlet | null>(null);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const executeSwitch = async (target: AvailableOutlet) => {
    setSwitching(true);
    setError(null);
    try {
      await switchOutlet(target.pos_profile);
      setPendingTarget(null);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('outlets.switch_failed'));
    } finally {
      setSwitching(false);
    }
  };

  const handleSelect = (outlet: AvailableOutlet) => {
    if (switching) return;
    if (posProfile?.name && outlet.pos_profile === posProfile.name && !mandatory) {
      onOpenChange(false);
      return;
    }
    if (activeOrders.length > 0 && !mandatory) {
      setPendingTarget(outlet);
      return;
    }
    void executeSwitch(outlet);
  };

  const handleClose = (next: boolean) => {
    if (switching) return;
    if (mandatory && !next) return;
    if (!next) {
      setPendingTarget(null);
      setError(null);
    }
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent
        size="md"
        onClose={mandatory ? undefined : () => handleClose(false)}
        className="max-h-[90vh] overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle>
            {pendingTarget
              ? t('outlets.discard_cart_title')
              : t('outlets.select_outlet_title')}
          </DialogTitle>
          <DialogDescription>
            {pendingTarget
              ? t('outlets.discard_cart_description', {
                  count: activeOrders.length,
                  outlet: pendingTarget.pos_profile,
                })
              : t('outlets.select_outlet_description')}
          </DialogDescription>
        </DialogHeader>

        <div className="px-6 pb-4 space-y-2">
          {pendingTarget ? (
            <div className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span>
                {t('outlets.discard_cart_warning', {
                  count: activeOrders.length,
                  outlet: pendingTarget.pos_profile,
                })}
              </span>
            </div>
          ) : (
            <div className="space-y-2">
              {availableOutlets.map((outlet) => {
                const isCurrent = Boolean(
                  posProfile?.name && posProfile.name === outlet.pos_profile,
                );
                return (
                  <button
                    key={outlet.pos_profile}
                    type="button"
                    disabled={switching}
                    onClick={() => handleSelect(outlet)}
                    aria-label={`${outlet.pos_profile} (${outlet.currency})`}
                    className={
                      isCurrent
                        ? 'w-full flex items-center justify-between gap-3 rounded-lg border-2 border-primary-500 bg-primary-50/40 px-3.5 py-3 text-start transition-colors'
                        : 'w-full flex items-center justify-between gap-3 rounded-lg border border-border bg-white hover:bg-gray-50 px-3.5 py-3 text-start transition-colors'
                    }
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-sm text-gray-900 truncate">
                          {outlet.pos_profile}
                        </span>
                        {outlet.currency && (
                          <Badge variant="secondary" size="sm">
                            {outlet.currency}
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-gray-500 truncate mt-0.5">
                        {outlet.branch}
                        {outlet.company ? ` · ${outlet.company}` : ''}
                      </p>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {outlet.has_open_shift && (
                        <Badge variant="info" size="sm">
                          {t('outlets.open_shift_badge')}
                        </Badge>
                      )}
                      {isCurrent && <Check className="w-4 h-4 text-primary-600" />}
                    </div>
                  </button>
                );
              })}
            </div>
          )}

          {error && (
            <p className="text-sm font-medium text-red-600 break-words" role="alert">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          {pendingTarget ? (
            <>
              <Button
                type="button"
                variant="outline"
                disabled={switching}
                onClick={() => setPendingTarget(null)}
              >
                {t('common.cancel')}
              </Button>
              <Button
                type="button"
                disabled={switching}
                onClick={() => void executeSwitch(pendingTarget)}
              >
                {switching ? t('outlets.switching') : t('outlets.discard_and_switch')}
              </Button>
            </>
          ) : !mandatory ? (
            <Button
              type="button"
              variant="outline"
              disabled={switching}
              onClick={() => handleClose(false)}
            >
              {t('common.cancel')}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

interface OutletSwitcherProps {
  disabled?: boolean;
}

const OutletSwitcher = ({ disabled = false }: OutletSwitcherProps) => {
  const posProfile = usePOSStore((state) => state.posProfile);
  const availableOutlets = usePOSStore((state) => state.availableOutlets) ?? [];
  const [open, setOpen] = useState(false);

  if (availableOutlets.length <= 1) {
    return null;
  }

  const activeName = posProfile?.name || availableOutlets[0]?.pos_profile || '';
  const activeCurrency = posProfile?.currency || availableOutlets[0]?.currency || '';
  const label = activeCurrency ? `${activeName} (${activeCurrency})` : activeName;

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={disabled}
        onClick={() => setOpen(true)}
        data-testid="outlet-switcher-button"
        aria-label={label}
        className="flex items-center gap-1.5 h-7 px-2 max-w-[11rem] sm:max-w-[15rem]"
      >
        <Store className="w-3.5 h-3.5 shrink-0 text-primary-600" />
        <span className="truncate text-xs font-medium">{activeName}</span>
        {activeCurrency && (
          <span className="hidden sm:inline text-[11px] text-gray-500 shrink-0">
            ({activeCurrency})
          </span>
        )}
        <ChevronDown className="w-3.5 h-3.5 shrink-0 text-gray-400" />
      </Button>

      <OutletPickerDialog open={open} onOpenChange={setOpen} />
    </>
  );
};

export default OutletSwitcher;
