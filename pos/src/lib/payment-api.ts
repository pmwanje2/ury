import { call, ownedSessionKey, getSessionUser } from '@ury/core';

interface PaymentMode {
  mode_of_payment: string;
  opening_amount: number;
}

interface PaymentModeResponse {
  message: PaymentMode[];
}

export const getPaymentModes = async (): Promise<string[]> => {
  // Check session storage first
  const owner = getSessionUser();
  const key = ownedSessionKey('payment_modes');
  const cached = key && sessionStorage.getItem(key);
  if (cached) {
    return JSON.parse(cached);
  }

  try {
    const response = await call.get<PaymentModeResponse>("ury.ury_pos.api.getModeOfPayment");

    const paymentModes = response.message.map((mode:PaymentMode) => mode.mode_of_payment);
    
    // Cache in session storage
    if (key && owner === getSessionUser()) sessionStorage.setItem(key, JSON.stringify(paymentModes));
    
    return paymentModes;
  } catch (error) {
    console.error('Failed to fetch payment modes:', error);
    throw error;
  }
}; 