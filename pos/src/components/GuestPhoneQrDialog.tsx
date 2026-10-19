import { useEffect, useState } from 'react';
import { QrCode } from 'lucide-react';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Button } from '@ury/ui';
import { parseFrappeError } from '@ury/core';
import { getGuestPhoneQrEnabled, issueGuestPhoneQr } from '../lib/guest-phone-qr-api';

interface GuestPhoneQrAvailabilityParams {
  branch?: string | null;
  posProfile?: string | null;
  invoice?: string | null;
  table?: string | null;
  canModify?: boolean;
}

export function useGuestPhoneQr({
  branch,
  posProfile,
  invoice,
  table,
  canModify = true,
}: GuestPhoneQrAvailabilityParams) {
  const [profileEnabled, setProfileEnabled] = useState(false);
  const [open, setOpen] = useState(false);
  const [requestVersion, setRequestVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setProfileEnabled(false);
    if (!branch || !posProfile || !invoice || !table || !canModify) return () => { cancelled = true; };
    void getGuestPhoneQrEnabled(branch, posProfile)
      .then((enabled) => { if (!cancelled) setProfileEnabled(enabled); })
      .catch(() => { if (!cancelled) setProfileEnabled(false); });
    return () => { cancelled = true; };
  }, [branch, canModify, invoice, posProfile, table]);

  const visible = profileEnabled && Boolean(invoice && table && canModify);

  useEffect(() => {
    if (!visible) setOpen(false);
  }, [visible]);

  const request = () => {
    if (!visible) return;
    setOpen(true);
    setRequestVersion((value) => value + 1);
  };

  return {
    visible,
    invoice: invoice ?? null,
    open,
    requestVersion,
    onRequest: request,
    onOpenChange: setOpen,
  };
}

export interface GuestPhoneQrDialogProps {
  visible: boolean;
  invoice: string | null;
  showTrigger?: boolean;
  open?: boolean;
  requestVersion?: number;
  onRequest?: () => void;
  onOpenChange?: (open: boolean) => void;
}

function secondsUntil(expiresAt: string | null): number | null {
  if (!expiresAt) return null;
  const parsed = new Date(expiresAt.replace(' ', 'T')).getTime();
  if (!Number.isFinite(parsed)) return null;
  return Math.max(0, Math.ceil((parsed - Date.now()) / 1000));
}

function formatCountdown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes}:${String(remainder).padStart(2, '0')}`;
}

export default function GuestPhoneQrDialog({
  visible,
  invoice,
  showTrigger = true,
  open,
  requestVersion,
  onRequest,
  onOpenChange,
}: GuestPhoneQrDialogProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const [internalRequestVersion, setInternalRequestVersion] = useState(0);
  const [image, setImage] = useState<string | null>(null);
  const [remaining, setRemaining] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const controlled = open !== undefined;
  const isOpen = controlled ? open : internalOpen;
  const version = requestVersion ?? internalRequestVersion;

  useEffect(() => {
    if (!isOpen || !invoice || version === 0) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setImage(null);
    void issueGuestPhoneQr(invoice)
      .then((result) => {
        if (cancelled) return;
        setImage(result.image);
        setRemaining(secondsUntil(result.expiresAt));
      })
      .catch((reason) => {
        if (!cancelled) setError(parseFrappeError(reason, 'Could not issue the guest phone QR.'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [invoice, isOpen, version]);

  useEffect(() => {
    if (!isOpen || remaining === null) return;
    const timer = window.setInterval(() => setRemaining((value) => value === null ? null : Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [isOpen, remaining]);

  if (!visible || !invoice) return null;

  const request = () => {
    setInternalRequestVersion((value) => value + 1);
    setInternalOpen(true);
    onRequest?.();
  };

  const handleOpenChange = (next: boolean) => {
    if (!next) setInternalOpen(false);
    onOpenChange?.(next);
  };

  return (
    <>
      {showTrigger && (
        <Button variant="outline" className="w-full gap-2" onClick={request}>
          <QrCode className="h-4 w-4" />
          Guest phone QR
        </Button>
      )}
      <Dialog open={isOpen} onOpenChange={handleOpenChange}>
        <DialogContent onClose={() => handleOpenChange(false)}>
          <DialogHeader>
            <DialogTitle>Guest phone QR</DialogTitle>
            <DialogDescription>Scan this code to open this table bill on the guest phone.</DialogDescription>
          </DialogHeader>
          <div className="flex min-h-56 flex-col items-center justify-center gap-3 px-6 pb-4">
            {loading && <p className="text-sm text-gray-500">Creating QR…</p>}
            {image && <img src={image} alt="Guest phone QR" className="h-56 w-56" />}
            {remaining !== null && <p role="status" className="text-sm text-gray-600">Expires in {formatCountdown(remaining)}</p>}
            {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => handleOpenChange(false)}>Close</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
