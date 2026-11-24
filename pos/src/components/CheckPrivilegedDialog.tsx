import { useEffect, useState } from 'react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Textarea,
  cn,
  showToast,
} from '@ury/ui';
import { t } from '../i18n';
import {
  isAuthRequiredError,
  listReasons,
  overrideErrorMessage,
  type OverrideReason,
} from '../lib/override-api';
import {
  adjustServiceCharge,
  applyComplimentary,
  applyTaxExemption,
  clearTaxExemption,
  extractAuthChallenge,
  listTaxExemptionOptions,
  type TaxExemptionOption,
} from '../lib/privileged-actions-api';
import ManagerApproval, { type ManagerApprovalResult } from './ManagerApproval';

export type CheckPrivilegedTab = 'complimentary' | 'tax_exempt' | 'service_charge';

export interface CheckPrivilegedDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialTab?: CheckPrivilegedTab;
  posProfile: string;
  company: string;
  invoiceName: string;
  currency?: string;
  isTaxExempt?: boolean;
  isServiceChargeWaived?: boolean;
  onSuccess: () => void | Promise<void>;
}

const TAB_PRIVILEGE: Record<CheckPrivilegedTab, string> = {
  complimentary: 'complimentary',
  tax_exempt: 'tax_exempt',
  service_charge: 'service_charge_waive',
};

export default function CheckPrivilegedDialog({
  open,
  onOpenChange,
  initialTab = 'complimentary',
  posProfile,
  company,
  invoiceName,
  isTaxExempt = false,
  isServiceChargeWaived = false,
  onSuccess,
}: CheckPrivilegedDialogProps) {
  const [tab, setTab] = useState<CheckPrivilegedTab>(initialTab);
  const [reasons, setReasons] = useState<OverrideReason[]>([]);
  const [selectedReason, setSelectedReason] = useState('');
  const [reasonNote, setReasonNote] = useState('');
  const [compType, setCompType] = useState<'Complimentary' | 'House' | 'Non-Revenue'>('Complimentary');
  const [taxCategories, setTaxCategories] = useState<TaxExemptionOption[]>([]);
  const [selectedTaxCategory, setSelectedTaxCategory] = useState('');
  const [certificate, setCertificate] = useState('');
  const [svcMode, setSvcMode] = useState<'waive' | 'restore' | 'override'>(
    isServiceChargeWaived ? 'restore' : 'waive',
  );
  const [svcRate, setSvcRate] = useState('');
  const [svcAmount, setSvcAmount] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{
    privilege: string;
    claim: Record<string, unknown>;
    summaryLines: string[];
  } | null>(null);

  useEffect(() => {
    if (open) {
      setTab(initialTab);
      setSvcMode(isServiceChargeWaived ? 'restore' : 'waive');
      setError(null);
      setPendingApproval(null);
    }
  }, [open, initialTab, isServiceChargeWaived]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setSelectedReason('');
    setReasonNote('');
    setError(null);
    const priv = TAB_PRIVILEGE[tab];
    listReasons(priv)
      .then((rows) => {
        if (active) setReasons(rows);
      })
      .catch(() => {
        if (active) setReasons([]);
      });
    if (tab === 'tax_exempt' && company) {
      listTaxExemptionOptions(company)
        .then((opts) => {
          if (active) {
            setTaxCategories(opts.tax_categories || []);
            if (opts.tax_categories?.length && !selectedTaxCategory) {
              setSelectedTaxCategory(opts.tax_categories[0].name);
            }
          }
        })
        .catch(() => {
          if (active) setTaxCategories([]);
        });
    }
    return () => {
      active = false;
    };
  }, [open, tab, company]);

  const selectedReasonObj = reasons.find((r) => r.code === selectedReason) ?? null;
  const needsNote = Boolean(selectedReasonObj && Number(selectedReasonObj.requires_note) === 1);
  const selectedTaxObj = taxCategories.find((c) => c.name === selectedTaxCategory);

  const submitAction = async (approval?: ManagerApprovalResult, clearExempt = false) => {
    setSubmitting(true);
    setError(null);
    const effectiveReason = selectedReason || approval?.reason || '';
    const effectiveNote = reasonNote.trim() || approval?.note || '';

    try {
      if (tab === 'complimentary') {
        await applyComplimentary({
          invoice: invoiceName,
          compType,
          reason: effectiveReason,
          reasonNote: effectiveNote,
          approvalToken: approval?.token,
        });
        showToast.success(t('privileged_actions.complimentary_applied'));
      } else if (tab === 'tax_exempt') {
        if (clearExempt) {
          await clearTaxExemption({
            invoice: invoiceName,
            reason: effectiveReason,
            reasonNote: effectiveNote,
            approvalToken: approval?.token,
          });
          showToast.success(t('privileged_actions.tax_exempt_cleared'));
        } else {
          await applyTaxExemption({
            invoice: invoiceName,
            taxCategory: selectedTaxCategory || undefined,
            certificate: certificate.trim() || undefined,
            reason: effectiveReason,
            reasonNote: effectiveNote,
            approvalToken: approval?.token,
          });
          showToast.success(t('privileged_actions.tax_exempt_applied'));
        }
      } else {
        await adjustServiceCharge({
          invoice: invoiceName,
          mode: svcMode,
          rate: svcMode === 'override' && svcRate !== '' ? Number(svcRate) : undefined,
          amount: svcMode === 'override' && svcAmount !== '' ? Number(svcAmount) : undefined,
          reason: effectiveReason,
          reasonNote: effectiveNote,
          approvalToken: approval?.token,
        });
        showToast.success(t('privileged_actions.service_charge_updated'));
      }
      await onSuccess();
      if (tab === 'service_charge') {
        onOpenChange(false);
      }
    } catch (err) {
      if (!approval?.token && isAuthRequiredError(err)) {
        const challenge = extractAuthChallenge(err);
        const priv = TAB_PRIVILEGE[tab];
        let fallbackClaim: Record<string, unknown>;
        let summaryLines: string[];
        if (tab === 'complimentary') {
          fallbackClaim = { action: 'complimentary', invoice: invoiceName, comp_type: compType, item_row_name: '' };
          summaryLines = [
            `${t('privileged_actions.complimentary')}: ${compType}`,
            `${t('privileged_actions.check_ref')}: ${invoiceName}`,
          ];
        } else if (tab === 'tax_exempt') {
          fallbackClaim = clearExempt
            ? { action: 'clear_tax_exemption', invoice: invoiceName }
            : {
                action: 'tax_exempt',
                invoice: invoiceName,
                tax_category: selectedTaxCategory,
                certificate: certificate.trim(),
              };
          summaryLines = [
            `${t('privileged_actions.tax_exempt')}: ${selectedTaxCategory || ''}`,
            certificate.trim() ? `${t('privileged_actions.certificate_ref')}: ${certificate.trim()}` : invoiceName,
          ];
        } else {
          fallbackClaim = {
            action: 'service_charge',
            invoice: invoiceName,
            mode: svcMode,
            rate: svcMode === 'override' && svcRate !== '' ? Number(svcRate) : null,
            amount: svcMode === 'override' && svcAmount !== '' ? Number(svcAmount) : null,
          };
          summaryLines = [
            `${t('privileged_actions.service_charge')}: ${svcMode}`,
            `${t('privileged_actions.check_ref')}: ${invoiceName}`,
          ];
        }
        setPendingApproval({ privilege: priv, claim: challenge?.claim ?? fallbackClaim, summaryLines });
        return;
      }
      setError(overrideErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Dialog open={open} onOpenChange={(next) => !submitting && onOpenChange(next)}>
        <DialogContent size="md" onClose={() => !submitting && onOpenChange(false)}>
          <DialogHeader>
            <DialogTitle>{t('privileged_actions.check_adjustments')}</DialogTitle>
            <DialogDescription>
              {t('privileged_actions.check_ref')}: {invoiceName}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 px-6 pb-2">
            <div role="tablist" className="grid grid-cols-3 gap-1.5 rounded-lg bg-gray-100 p-1">
              {(['complimentary', 'tax_exempt', 'service_charge'] as CheckPrivilegedTab[]).map((tabKey) => (
                <button
                  key={tabKey}
                  type="button"
                  role="tab"
                  aria-selected={tab === tabKey}
                  onClick={() => setTab(tabKey)}
                  className={cn(
                    'min-h-11 rounded-md px-2 py-2 text-xs font-medium transition-colors sm:text-sm',
                    tab === tabKey ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-600 hover:text-gray-900',
                  )}
                >
                  {t(`privileged_actions.${tabKey}`)}
                </button>
              ))}
            </div>

            {tab === 'complimentary' && (
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <label htmlFor="comp-type-select" className="block text-sm font-medium text-gray-900">
                    {t('privileged_actions.comp_type')}
                  </label>
                  <select
                    id="comp-type-select"
                    value={compType}
                    onChange={(e) => setCompType(e.target.value as 'Complimentary' | 'House' | 'Non-Revenue')}
                    disabled={submitting}
                    className="min-h-11 w-full rounded-md border border-border bg-white px-3 py-2 text-sm"
                  >
                    <option value="Complimentary">{t('privileged_actions.comp_complimentary')}</option>
                    <option value="House">{t('privileged_actions.comp_house')}</option>
                    <option value="Non-Revenue">{t('privileged_actions.comp_non_revenue')}</option>
                  </select>
                </div>
                <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  {t('privileged_actions.comp_second_person_notice')}
                </p>
              </div>
            )}

            {tab === 'tax_exempt' && (
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <label htmlFor="tax-category-select" className="block text-sm font-medium text-gray-900">
                    {t('privileged_actions.tax_category')}
                  </label>
                  <select
                    id="tax-category-select"
                    value={selectedTaxCategory}
                    onChange={(e) => setSelectedTaxCategory(e.target.value)}
                    disabled={submitting}
                    className="min-h-11 w-full rounded-md border border-border bg-white px-3 py-2 text-sm"
                  >
                    <option value="">{t('privileged_actions.select_tax_category')}</option>
                    {taxCategories.map((cat) => (
                      <option key={cat.name} value={cat.name}>
                        {cat.name}
                        {cat.custom_efris_tax_category ? ` (EFRIS ${cat.custom_efris_tax_category})` : ''}
                      </option>
                    ))}
                  </select>
                  {selectedTaxObj?.custom_efris_tax_category && (
                    <p className="text-xs text-gray-500">
                      {t('privileged_actions.efris_mapping', { code: selectedTaxObj.custom_efris_tax_category })}
                    </p>
                  )}
                </div>

                <div className="space-y-1.5">
                  <label htmlFor="tax-certificate-input" className="block text-sm font-medium text-gray-900">
                    {t('privileged_actions.certificate_ref')}
                  </label>
                  <input
                    id="tax-certificate-input"
                    type="text"
                    value={certificate}
                    onChange={(e) => setCertificate(e.target.value)}
                    disabled={submitting}
                    placeholder={t('privileged_actions.certificate_placeholder')}
                    className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm"
                  />
                </div>
              </div>
            )}

            {tab === 'service_charge' && (
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <label htmlFor="svc-mode-select" className="block text-sm font-medium text-gray-900">
                    {t('privileged_actions.service_charge_action')}
                  </label>
                  <select
                    id="svc-mode-select"
                    value={svcMode}
                    onChange={(e) => setSvcMode(e.target.value as 'waive' | 'restore' | 'override')}
                    disabled={submitting}
                    className="min-h-11 w-full rounded-md border border-border bg-white px-3 py-2 text-sm"
                  >
                    <option value="waive">{t('privileged_actions.waive_service_charge')}</option>
                    <option value="restore">{t('privileged_actions.restore_service_charge')}</option>
                    <option value="override">{t('privileged_actions.override_service_charge')}</option>
                  </select>
                </div>

                {svcMode === 'override' && (
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1.5">
                      <label htmlFor="svc-rate-input" className="block text-sm font-medium text-gray-900">
                        {t('privileged_actions.service_charge_rate')}
                      </label>
                      <input
                        id="svc-rate-input"
                        type="number"
                        inputMode="decimal"
                        min={0}
                        step="any"
                        value={svcRate}
                        onChange={(e) => {
                          setSvcRate(e.target.value);
                          setSvcAmount('');
                        }}
                        disabled={submitting}
                        className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm tabular-nums"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label htmlFor="svc-amount-input" className="block text-sm font-medium text-gray-900">
                        {t('privileged_actions.service_charge_amount')}
                      </label>
                      <input
                        id="svc-amount-input"
                        type="number"
                        inputMode="decimal"
                        min={0}
                        step="any"
                        value={svcAmount}
                        onChange={(e) => {
                          setSvcAmount(e.target.value);
                          setSvcRate('');
                        }}
                        disabled={submitting}
                        className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm tabular-nums"
                      />
                    </div>
                  </div>
                )}
              </div>
            )}

            <div className="space-y-1.5">
              <label htmlFor="check-priv-reason" className="block text-sm font-medium text-gray-900">
                {t('privileged_actions.reason')}
              </label>
              {reasons.length > 0 ? (
                <select
                  id="check-priv-reason"
                  value={selectedReason}
                  onChange={(e) => setSelectedReason(e.target.value)}
                  disabled={submitting}
                  className="min-h-11 w-full rounded-md border border-border bg-white px-3 py-2 text-sm"
                >
                  <option value="">{t('privileged_actions.select_reason')}</option>
                  {reasons.map((r) => (
                    <option key={r.code} value={r.code}>
                      {r.description || r.code}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id="check-priv-reason"
                  type="text"
                  value={selectedReason}
                  onChange={(e) => setSelectedReason(e.target.value)}
                  disabled={submitting}
                  placeholder={t('privileged_actions.enter_reason')}
                  className="min-h-11 w-full rounded-md border border-border px-3 py-2 text-sm"
                />
              )}
            </div>

            {(needsNote || reasonNote) && (
              <div className="space-y-1.5">
                <label htmlFor="check-priv-note" className="block text-sm font-medium text-gray-900">
                  {t('privileged_actions.reason_note')}
                </label>
                <Textarea
                  id="check-priv-note"
                  value={reasonNote}
                  onChange={(e) => setReasonNote(e.target.value)}
                  disabled={submitting}
                  rows={2}
                />
              </div>
            )}

            {error && (
              <p role="alert" className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700">
                {error}
              </p>
            )}
          </div>

          <DialogFooter className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting} className="min-h-11">
              {t('common.cancel')}
            </Button>
            {tab === 'tax_exempt' && isTaxExempt && (
              <Button
                variant="outline"
                onClick={() => void submitAction(undefined, true)}
                disabled={submitting}
                className="min-h-11"
              >
                {t('privileged_actions.clear_tax_exempt')}
              </Button>
            )}
            <Button onClick={() => void submitAction()} disabled={submitting} className="min-h-11">
              {tab === 'complimentary'
                ? t('privileged_actions.apply_complimentary')
                : tab === 'tax_exempt'
                  ? t('privileged_actions.apply_tax_exempt')
                  : t('privileged_actions.apply_service_charge')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {pendingApproval && (
        <ManagerApproval
          open
          posProfile={posProfile}
          privilege={pendingApproval.privilege}
          title={t(`privileged_actions.${tab}`)}
          summaryLines={pendingApproval.summaryLines}
          reference={{ doctype: 'POS Invoice', name: invoiceName }}
          claim={pendingApproval.claim}
          requireReason={false}
          onApproved={(res) => {
            setPendingApproval(null);
            void submitAction(res);
          }}
          onCancel={() => setPendingApproval(null)}
        />
      )}
    </>
  );
}
