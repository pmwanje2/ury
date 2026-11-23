/** Locale-independent comparison form: NFC, single spaces, Unicode lower case. */
function fold(value: string): string {
  return value.normalize('NFC').replace(/\s+/g, ' ').trim().toLowerCase();
}

/** What may follow a customer name inside a label that names it: "(Rm 408)", ", Rm 4", " - Board". */
const NOTE_AFTER_NAME = /^(?:\s*[([{,:;]|\s+[-\u2013\u2014|/\u00b7]\s)/;

/**
 * The check's name: its guest label (Simphony's check name), then the billed
 * customer, so two splits on one customer ("Susan · Walk In", "David · Walk
 * In") are told apart. The customer shows by name; its id only when it has no
 * name. The customer is left out only when the label is that same customer:
 * its name or id, or the customer as shown (its name, else its id) followed
 * by a bracketed or separated note ("Dr. Elena Rostova (Rm 408)"). A label
 * that merely contains or sits inside the customer never hides it.
 */
export function formatGuestSubtitle(
  guestLabel?: string | null,
  customerName?: string | null,
  customerId?: string | null,
): string {
  const label = guestLabel?.trim() ?? '';
  const name = customerName?.trim() ?? '';
  const id = customerId?.trim() ?? '';
  const customer = name || id;
  if (!label || !customer) return label || customer;
  const foldedLabel = fold(label);
  const foldedCustomer = fold(customer);
  if (foldedLabel === foldedCustomer || (id && foldedLabel === fold(id))) return customer;
  if (foldedLabel.startsWith(foldedCustomer) && NOTE_AFTER_NAME.test(foldedLabel.slice(foldedCustomer.length))) return label;
  return `${label} · ${customer}`;
}

/**
 * A staff member's display name. The User record's full name wins (see
 * useStaffNames); only when it cannot be read is a name guessed from the
 * email user id ("samuel.otieno@domain.example" becomes "Samuel Otieno"),
 * so callers also show the raw user id where the full detail is shown.
 */
export function formatStaffDisplayName(fullName?: string | null, userId?: string | null): string {
  const name = fullName?.trim() ?? '';
  if (name && !name.includes('@')) return name;
  const id = userId?.trim() || name;
  const at = id.indexOf('@');
  if (at === -1) return id;
  const words = id.slice(0, at).split('+')[0].split(/[._-]+/).filter(Boolean);
  if (words.length === 0) return id;
  return words.map((word) => word.charAt(0).toLocaleUpperCase() + word.slice(1)).join(' ');
}
