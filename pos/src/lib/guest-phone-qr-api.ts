import { call, db } from '@ury/core';

const SELF_ORDER_PATH = '/order/';

interface GuestPhoneQrProfile {
  require_guest_invoice_grant?: number | boolean | string;
}

interface GrantResponse {
  session: string;
  expires_at: string;
}

export async function getGuestPhoneQrEnabled(branch: string, posProfile: string): Promise<boolean> {
  const profiles = await db.getDocList<GuestPhoneQrProfile>('URY Self Ordering Profile', {
    fields: ['name', 'require_guest_invoice_grant'],
    filters: [
      ['branch', '=', branch],
      ['pos_profile', '=', posProfile],
      ['enabled', '=', 1],
      ['enable_qr_table_ordering', '=', 1],
    ],
    limit: 2,
    asDict: true,
  });
  return profiles.length === 1 && [1, '1', true].includes(profiles[0]?.require_guest_invoice_grant ?? false);
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
