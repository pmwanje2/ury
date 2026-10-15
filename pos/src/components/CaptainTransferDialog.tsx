import { useEffect, useMemo, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@ury/ui';
import { Button } from '@ury/ui';
import { cn } from '@ury/ui';
import { t } from '../i18n';
import { Spinner } from '@ury/ui';
import { getCaptainTransferCandidates } from '../lib/order-api';
import { parseFrappeError } from '@ury/core';

interface CaptainTransferDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentCaptain: string;
  invoiceName: string;
  onConfirm: (newCaptain: string) => Promise<void>;
}

interface CaptainOption {
  name: string;
  full_name?: string;
}

const CaptainTransferDialog = ({
  open,
  onOpenChange,
  currentCaptain,
  invoiceName,
  onConfirm,
}: CaptainTransferDialogProps) => {
  const [selected, setSelected] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [candidates, setCandidates] = useState<CaptainOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setSelected(null);
    setSearch('');
    setCandidates([]);
    setLoadError(null);
    setError(null);
    setIsSubmitting(false);
  }, [open, currentCaptain]);

  useEffect(() => {
    if (!open) return;

    let cancelled = false;
    setLoading(true);
    setLoadError(null);

    getCaptainTransferCandidates(invoiceName)
      .then((rows) => {
        if (cancelled) return;
        setCandidates(
          rows.filter((row) => row.name !== currentCaptain)
        );
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadError(parseFrappeError(err, t('tables.transfer_failed')));
          setCandidates([]);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open, currentCaptain, invoiceName]);

  const displayCandidates = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return candidates;
    return candidates.filter((row) =>
      [row.name, row.full_name || ''].some((value) => value.toLowerCase().includes(query))
    );
  }, [candidates, search]);

  const handleOpenChange = (next: boolean) => {
    if (isSubmitting) return;
    onOpenChange(next);
  };

  const handleConfirm = async () => {
    if (!selected) return;
    setIsSubmitting(true);
    setError(null);
    try {
      await onConfirm(selected);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('tables.transfer_failed'));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        variant="large"
        size="lg"
        onClose={() => handleOpenChange(false)}
        className="max-h-dialog-max-h overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle>{t('tables.transfer_captain')}</DialogTitle>
          <DialogDescription>{t('tables.select_new_captain')}</DialogDescription>
        </DialogHeader>

        <div className="px-6 pb-2">
          <label className="mb-1 block text-sm font-medium text-gray-700">
            {t('tables.current_captain')}
          </label>
          <input
            type="text"
            readOnly
            value={currentCaptain}
            className="mb-4 w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700"
          />

          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('tables.search_captain_placeholder')}
            className="mb-3 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
            disabled={isSubmitting}
          />

          {loading ? (
            <div className="flex items-center justify-center py-8">
              <Spinner hideMessage  message={t('common.loading')} />
            </div>
          ) : loadError ? (
            <p className="py-4 text-sm text-red-600">{loadError}</p>
          ) : displayCandidates.length === 0 ? (
            <p className="py-4 text-sm text-gray-500">{t('tables.no_captains_found')}</p>
          ) : (
            <div className="space-y-2">
              {displayCandidates.map((row) => {
                const isSelected = selected === row.name;
                return (
                  <button
                    key={row.name}
                    type="button"
                    disabled={isSubmitting}
                    onClick={() => setSelected(row.name)}
                    className={cn(
                      'flex w-full flex-col rounded-lg border p-3 text-left transition-colors',
                      isSelected
                        ? 'border-primary bg-primary-50/40'
                        : 'border-gray-200 hover:border-gray-300'
                    )}
                  >
                    <p className="font-medium text-gray-900">{row.full_name || row.name}</p>
                    <p className="text-xs text-gray-500">{row.name}</p>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {error && <p className="px-6 pb-2 text-sm text-red-600">{error}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={isSubmitting}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleConfirm} disabled={isSubmitting || !selected || loading}>
            {isSubmitting ? t('common.loading') : t('tables.transfer_confirm')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default CaptainTransferDialog;
