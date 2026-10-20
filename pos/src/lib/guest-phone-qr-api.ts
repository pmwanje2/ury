import { call } from '@ury/core';

const SELF_ORDER_PATH = '/order/';

interface GrantResponse {
  session: string;
  expires_at: string;
}

// The ERP answers for this user and bill; staff never read the profile (it holds the QR signing secret).
export async function canIssueGuestPhoneQr(invoice: string): Promise<boolean> {
  const response = await call.post<{ message: { visible?: boolean } }>(
    'ury.ury.api.self_ordering.can_issue_table_grant',
    { invoice },
  );
  return response.message?.visible === true;
}

export async function issueGuestPhoneQr(invoice: string): Promise<{ image: string; expiresAt: string }> {
  const grant = await call.post<{ message: GrantResponse }>(
    'ury.ury.api.self_ordering.issue_table_grant',
    { invoice },
  );
  const url = new URL(SELF_ORDER_PATH, window.location.origin);
  url.hash = `g=${grant.message.session}`;
  const qr = await call.post<{ message: string }>(
    'frappe.utils.print_format_generator.get_qr_code',
    { value: url.toString() },
  );
  return { image: qr.message, expiresAt: grant.message.expires_at };
}
