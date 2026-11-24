import { call, ownedSessionKey, getSessionUser } from '@ury/core';

export interface PaymentModeConfig {
  mode_of_payment: string;
  opening_amount?: number;
  type?: string;
  custom_reference_mode?: 'Hidden' | 'Optional' | 'Required' | string;
  custom_reference_label?: string;
  custom_reference_placeholder?: string;
  custom_reference_pattern?: string;
  custom_reference_date_required?: number | boolean;
  custom_tender_currency?: string;
  custom_fx_rate_type?: 'Buying' | 'Mid/Selling' | string;
  default_account?: string | null;
  custom_fx_vault_account?: string | null;
  tender_precision?: number;
  exchange_rate?: number | null;
  exchange_rate_date?: string | null;
  fx_rate_status?: 'fresh' | 'stale' | string;
  fx_rate_error?: string;
}

interface PaymentModeResponse {
  message: PaymentModeConfig[];
}

export interface PosFxRateInfo {
  status: 'fresh' | 'stale';
  tender_currency: string;
  home_currency: string;
  rate: number | null;
  rate_date: string | null;
  rate_type: string;
  precision: number;
  error?: string;
}

export interface PosFxRatesResponse {
  home_currency: string;
  allow_fx_rate_override: number;
  max_fx_variance_pct: number;
  rates: Record<string, PosFxRateInfo>;
}

export const getPaymentModeDetails = async (forceRefresh = false): Promise<PaymentModeConfig[]> => {
  const owner = getSessionUser();
  const detailsKey = ownedSessionKey('payment_mode_details');
  const namesKey = ownedSessionKey('payment_modes');
  if (!forceRefresh && detailsKey) {
    const cached = sessionStorage.getItem(detailsKey);
    if (cached) {
      try {
        return JSON.parse(cached) as PaymentModeConfig[];
      } catch {
        // Fall through and refetch.
      }
    }
  }

  const response = await call.get<PaymentModeResponse>('ury.ury_pos.api.getModeOfPayment');
  const rows = Array.isArray(response?.message) ? response.message : [];
  if (owner === getSessionUser()) {
    if (detailsKey) sessionStorage.setItem(detailsKey, JSON.stringify(rows));
    if (namesKey) sessionStorage.setItem(namesKey, JSON.stringify(rows.map((m) => m.mode_of_payment)));
  }
  return rows;
};

export const getPaymentModes = async (): Promise<string[]> => {
  const owner = getSessionUser();
  const key = ownedSessionKey('payment_modes');
  const cached = key && sessionStorage.getItem(key);
  if (cached) {
    return JSON.parse(cached);
  }

  try {
    const rows = await getPaymentModeDetails(true);
    const paymentModes = rows.map((mode) => mode.mode_of_payment);
    if (key && owner === getSessionUser()) sessionStorage.setItem(key, JSON.stringify(paymentModes));
    return paymentModes;
  } catch (error) {
    console.error('Failed to fetch payment modes:', error);
    throw error;
  }
};

export const getPosFxRates = async (posProfile: string, postingDate?: string): Promise<PosFxRatesResponse> => {
  const res = await call.get<{ message: PosFxRatesResponse }>('ury.ury_pos.fx.get_pos_fx_rates', {
    pos_profile: posProfile,
    ...(postingDate ? { posting_date: postingDate } : {}),
  });
  return res.message;
};
