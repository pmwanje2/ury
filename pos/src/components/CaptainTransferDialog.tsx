import { useEffect, useState } from 'react';
import { UserPickerDialog } from '@ury/ui';
import { getCaptainTransferCandidates } from '../lib/order-api';
import { parseFrappeError } from '@ury/core';
import { t } from '../i18n';

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
  currentCaptain, invoiceName,
  onConfirm,
}: CaptainTransferDialogProps) => {
  const [search, setSearch] = useState('');
  const [candidates, setCandidates] = useState<CaptainOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setSearch('');
    setCandidates([]);
    setLoadError(null);
  }, [open, currentCaptain, invoiceName]);

  useEffect(() => {
    if (!open || !invoiceName) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    getCaptainTransferCandidates(invoiceName)
      .then((rows) => {
        if (cancelled) return;
        setCandidates(rows.filter((row) => row.name !== currentCaptain));
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

  const query = search.trim().toLowerCase();
  const visible = candidates.filter(row => [row.name, row.full_name || ''].some(value => value.toLowerCase().includes(query)));

  return (
    <UserPickerDialog key={invoiceName}
      open={open}
      onOpenChange={onOpenChange}
      sourceValue={currentCaptain}
      options={visible.map((c) => ({
        name: c.name,
        label: c.full_name || c.name,
      }))}
      loading={loading}
      loadError={loadError}
      search={search}
      onSearchChange={setSearch}
      onConfirm={onConfirm}
      labels={{
        title: t('tables.transfer_captain'),
        description: t('tables.select_new_captain'),
        currentLabel: t('tables.current_captain'),
        searchPlaceholder: t('tables.search_captain_placeholder'),
        empty: t('tables.no_captains_found'),
        cancel: t('common.cancel'),
        confirm: t('tables.transfer_confirm'),
        loading: t('common.loading'),
        errorFallback: t('tables.transfer_failed'),
      }}
    />
  );
};

export default CaptainTransferDialog;
