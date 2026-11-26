// Desk and the bundled clients use the same receipt authorization protocol.
const api = 'ury.ury.api.ury_print.';

function requestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // getRandomValues also works on HTTP-served LAN terminals.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
}

export async function prepareReceipt(post, args) {
  const { message } = await post(api + 'prepare_receipt_delivery', { ...args, request_id: requestId() });
  if (!message?.delivery_id || !message.html || !message.invoice_revision) {
    throw new Error('Receipt authorization was unavailable.');
  }
  return message;
}

export async function completeReceipt(post, invoice, delivery) {
  await post(api + 'complete_receipt_delivery', { invoice, delivery_id: delivery.delivery_id });
}

export async function dispatchReceipt(post, args, print) {
  const delivery = await prepareReceipt(post, args);
  if (delivery.delivered) return;
  await print(delivery.html);
  await completeReceipt(post, args.invoice, delivery);
}
