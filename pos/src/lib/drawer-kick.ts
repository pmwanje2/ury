import { call, kickDrawerQz, resolveDrawerKickHex, type DrawerPin } from '@ury/core';

export const DRAWER_KICK_EVENT = 'pos:drawer-kick';
export const PAYMENT_SETTLED_EVENT = 'pos:payment-settled';

export interface DrawerKickProfileContext {
  pos_profile?: string | null;
  name?: string | null;
  print_type?: string | null;
  qz_host?: string | null;
  printer?: string | null;
  custom_drawer_pin?: DrawerPin | number | string | null;
  custom_drawer_kick_hex?: string | null;
  custom_auto_kick_on_cash_tender?: boolean | number | null;
  drawer_opening_modes?: string[] | null;
}

export interface DrawerKickEventDetail {
  hex?: string | null;
  pin?: DrawerPin | number | string | null;
  posProfile?: string | null;
  printerName?: string | null;
  qzHost?: string | null;
  printType?: string | null;
  drawerEvent?: string | null;
  reason?: string | null;
}

export interface SettlementPaymentLine {
  mode_of_payment?: string | null;
  amount?: number | string | null;
  open_cash_drawer?: boolean | number | null;
}

export interface PaymentSettledEventDetail extends DrawerKickEventDetail {
  autoKickEnabled?: boolean | number | null;
  payments?: SettlementPaymentLine[];
  changeAmount?: number;
  drawerModes?: Iterable<string>;
}

export interface DrawerKickResult {
  status: 'Sent' | 'Failed' | 'Not Configured';
  transport: 'qz' | 'tcp' | 'cups' | 'network' | 'none';
  hex: string;
  message?: string;
}

export interface DrawerKickDependencies {
  getProfile?: () => DrawerKickProfileContext | null | undefined;
  kickQz?: (
    host: string,
    printerName?: string | null,
    hexCommand?: string | null,
    pin?: DrawerPin,
  ) => Promise<{ status: 'Sent'; hex: string; printer?: unknown }>;
  callServer?: (payload: {
    pos_profile: string;
    drawer_pin: DrawerPin;
    hex_command: string;
    reason?: string;
    drawer_event?: string;
  }) => Promise<Partial<DrawerKickResult>>;
}

function normalizePin(value: DrawerPin | number | string | null | undefined): DrawerPin {
  return Number(value) === 5 ? 5 : 2;
}

/**
 * Determine whether settling an invoice should auto-kick the cash drawer.
 * Strictly configuration-driven via `Mode of Payment.custom_open_cash_drawer`
 * (passed as `open_cash_drawer` per line or `drawerModes` set) — never hardcoded
 * to any payment method name such as "Cash".
 */
export function shouldAutoKickForSettlement(opts: {
  autoKickEnabled?: boolean | number | null;
  payments?: SettlementPaymentLine[];
  changeAmount?: number;
  drawerModes?: Iterable<string>;
}): boolean {
  if (opts.autoKickEnabled === false || opts.autoKickEnabled === 0) {
    return false;
  }
  const configuredModes = new Set(
    Array.from(opts.drawerModes ?? [])
      .map((mode) => String(mode || '').trim())
      .filter(Boolean),
  );
  const payments = Array.isArray(opts.payments) ? opts.payments : [];
  for (const row of payments) {
    const amount = Number(row?.amount ?? 0);
    if (!Number.isFinite(amount) || amount <= 0) continue;
    if (row?.open_cash_drawer === true || Number(row?.open_cash_drawer) === 1) {
      return true;
    }
    const modeName = String(row?.mode_of_payment || '').trim();
    if (modeName && configuredModes.has(modeName)) {
      return true;
    }
  }
  return false;
}

async function defaultCallServer(payload: {
  pos_profile: string;
  drawer_pin: DrawerPin;
  hex_command: string;
  reason?: string;
  drawer_event?: string;
}): Promise<Partial<DrawerKickResult>> {
  const response = await call.post<{ message?: Partial<DrawerKickResult> }>(
    'ury.ury.api.ury_print.kick_cash_drawer',
    payload,
  );
  return response?.message ?? {};
}

export async function executeDrawerKick(
  detail: DrawerKickEventDetail = {},
  deps: DrawerKickDependencies = {},
): Promise<DrawerKickResult> {
  const profile = deps.getProfile ? deps.getProfile() : null;
  const pin = normalizePin(detail.pin ?? profile?.custom_drawer_pin ?? 2);
  let hex: string;
  try {
    hex = resolveDrawerKickHex({
      hex: detail.hex ?? profile?.custom_drawer_kick_hex ?? null,
      pin,
    });
  } catch (error) {
    return {
      status: 'Failed',
      transport: 'none',
      hex: '',
      message: error instanceof Error ? error.message : String(error),
    };
  }

  const printType = String(detail.printType ?? profile?.print_type ?? '').trim().toLowerCase();
  const qzHost = String(detail.qzHost ?? profile?.qz_host ?? '').trim();
  const printerName = (detail.printerName ?? profile?.printer ?? null) || null;
  const posProfile = String(detail.posProfile ?? profile?.pos_profile ?? profile?.name ?? '').trim();

  const useQz = printType === 'qz' || (Boolean(qzHost) && printType !== 'network');
  if (useQz && qzHost) {
    const kickQz = deps.kickQz ?? kickDrawerQz;
    try {
      await kickQz(qzHost, printerName, hex, pin);
      return { status: 'Sent', transport: 'qz', hex };
    } catch (error) {
      return {
        status: 'Failed',
        transport: 'qz',
        hex,
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  if (posProfile) {
    const invokeServer = deps.callServer ?? defaultCallServer;
    try {
      const serverRes = await invokeServer({
        pos_profile: posProfile,
        drawer_pin: pin,
        hex_command: hex,
        reason: detail.reason || undefined,
        drawer_event: detail.drawerEvent || undefined,
      });
      const status =
        serverRes.status === 'Sent' || serverRes.status === 'Failed' || serverRes.status === 'Not Configured'
          ? serverRes.status
          : 'Not Configured';
      const transport =
        serverRes.transport === 'tcp' ||
        serverRes.transport === 'cups' ||
        serverRes.transport === 'network' ||
        serverRes.transport === 'qz'
          ? serverRes.transport
          : 'none';
      return {
        status,
        transport,
        hex: serverRes.hex || hex,
        message: serverRes.message,
      };
    } catch (error) {
      return {
        status: 'Failed',
        transport: 'network',
        hex,
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  return {
    status: 'Not Configured',
    transport: 'none',
    hex,
    message: 'No cash drawer printer transport is configured.',
  };
}

export function installDrawerKickListener(options: {
  target?: EventTarget;
  getProfile?: () => DrawerKickProfileContext | null | undefined;
  onResult?: (result: DrawerKickResult) => void;
  deps?: DrawerKickDependencies;
} = {}): () => void {
  const eventTarget: EventTarget | undefined =
    options.target ?? (typeof window !== 'undefined' ? window : undefined);
  if (!eventTarget) {
    return () => {};
  }

  const mergedDeps: DrawerKickDependencies = {
    ...options.deps,
    getProfile: options.getProfile ?? options.deps?.getProfile,
  };

  const handleKick = (event: Event) => {
    const custom = event as CustomEvent<DrawerKickEventDetail | undefined>;
    void executeDrawerKick(custom?.detail ?? {}, mergedDeps).then((res) => {
      options.onResult?.(res);
    });
  };

  const handlePaymentSettled = (event: Event) => {
    const custom = event as CustomEvent<PaymentSettledEventDetail | undefined>;
    const detail = custom?.detail ?? {};
    const profile = mergedDeps.getProfile ? mergedDeps.getProfile() : null;
    const autoKickEnabled =
      detail.autoKickEnabled !== undefined
        ? detail.autoKickEnabled
        : profile?.custom_auto_kick_on_cash_tender;
    const drawerModes = detail.drawerModes ?? profile?.drawer_opening_modes ?? [];
    if (
      !shouldAutoKickForSettlement({
        autoKickEnabled,
        payments: detail.payments,
        changeAmount: detail.changeAmount,
        drawerModes,
      })
    ) {
      return;
    }
    void executeDrawerKick(detail, mergedDeps).then((res) => {
      options.onResult?.(res);
    });
  };

  eventTarget.addEventListener(DRAWER_KICK_EVENT, handleKick);
  eventTarget.addEventListener(PAYMENT_SETTLED_EVENT, handlePaymentSettled);

  return () => {
    eventTarget.removeEventListener(DRAWER_KICK_EVENT, handleKick);
    eventTarget.removeEventListener(PAYMENT_SETTLED_EVENT, handlePaymentSettled);
  };
}
