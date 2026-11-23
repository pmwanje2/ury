const WORD_CHAR = /[\p{L}\p{N}]/u;

/** True when `needle` appears in `haystack` as whole words (both already case-folded). */
function containsWords(haystack: string, needle: string): boolean {
  for (let at = haystack.indexOf(needle); at !== -1; at = haystack.indexOf(needle, at + 1)) {
    const before = haystack[at - 1];
    const after = haystack[at + needle.length];
    const startsClean = !before || !WORD_CHAR.test(before) || !WORD_CHAR.test(needle[0]);
    const endsClean = !after || !WORD_CHAR.test(after) || !WORD_CHAR.test(needle[needle.length - 1]);
    if (startsClean && endsClean) return true;
  }
  return false;
}

/**
 * The check's name: its guest label first, then its customer, so two splits on
 * one walk-in customer ("Susan · Walk In", "David · Walk In") are told apart.
 * When one side already names the other ("Dr. Elena Rostova (Rm 408)" and
 * "Dr. Elena Rostova") only the fuller one is shown.
 */
export function formatGuestSubtitle(guestLabel?: string | null, customerName?: string | null): string {
  const label = guestLabel?.trim() ?? '';
  const customer = customerName?.trim() ?? '';
  if (!label || !customer) return label || customer;
  const foldedLabel = label.toLocaleLowerCase();
  const foldedCustomer = customer.toLocaleLowerCase();
  if (containsWords(foldedLabel, foldedCustomer)) return label;
  if (containsWords(foldedCustomer, foldedLabel)) return customer;
  return `${label} · ${customer}`;
}

/**
 * A staff member's display name. A full name from the payload wins; a bare
 * email user id ("samuel.otieno@domain.example") becomes "Samuel Otieno".
 * Callers keep the raw user id in a tooltip for auditability.
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
