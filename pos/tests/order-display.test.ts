import { describe, expect, it, vi } from 'vitest';
import { formatGuestSubtitle, formatStaffDisplayName } from '../src/lib/order-display';

// A check's name is its free-text guest label plus the billed customer (Simphony
// keeps the Check Name apart from the Customer record). The customer may be
// dropped only when the label demonstrably names that same customer, so the
// cashier never loses sight of who the check is billed to.

describe('formatGuestSubtitle', () => {
  it('shows the label alone when it is the customer plus a bracketed note', () => {
    expect(formatGuestSubtitle('Dr. Elena Rostova (Rm 408)', 'Dr. Elena Rostova', 'CUST-0001')).toBe('Dr. Elena Rostova (Rm 408)');
    expect(formatGuestSubtitle('Acme [VIP]', 'Acme')).toBe('Acme [VIP]');
    expect(formatGuestSubtitle('Acme(Rm 2)', 'Acme')).toBe('Acme(Rm 2)');
  });

  it('shows the label alone when it is the customer plus a separated note', () => {
    for (const label of ['Acme - Board Lunch', 'Acme – Board Lunch', 'Acme — Board', 'Acme, Rm 4', 'Acme: Board',
      'Acme / Seat 2', 'Acme | Seat 2', 'Acme · Seat 2']) {
      expect(formatGuestSubtitle(label, 'Acme'), label).toBe(label);
    }
  });

  it('shows the customer record once when the label only repeats it', () => {
    expect(formatGuestSubtitle('  dr. elena rostova ', 'Dr. Elena Rostova')).toBe('Dr. Elena Rostova');
  });

  it('names the customer, never its id, when the label is the customer id', () => {
    expect(formatGuestSubtitle('CUST-0001', 'Dr. Elena Rostova', 'CUST-0001')).toBe('Dr. Elena Rostova');
    expect(formatGuestSubtitle('cust-0001', 'Dr. Elena Rostova', 'CUST-0001')).toBe('Dr. Elena Rostova');
  });

  it('shows the customer name, and its id only when the customer has no name', () => {
    expect(formatGuestSubtitle(null, 'Dr. Elena Rostova', 'CUST-0001')).toBe('Dr. Elena Rostova');
    expect(formatGuestSubtitle('Susan', 'Walk In', 'CUST-0009')).toBe('Susan · Walk In');
    expect(formatGuestSubtitle(null, '', 'CUST-0001')).toBe('CUST-0001');
    expect(formatGuestSubtitle('Susan', null, 'Walk In')).toBe('Susan · Walk In');
  });

  it('keeps both when the label is only part of the customer name', () => {
    expect(formatGuestSubtitle('Elena Rostova', 'Dr. Elena Rostova')).toBe('Elena Rostova · Dr. Elena Rostova');
    expect(formatGuestSubtitle('Customer', 'Walk In Customer')).toBe('Customer · Walk In Customer');
  });

  it('tells apart two labels that both sit inside one customer name', () => {
    const smith = formatGuestSubtitle('Smith', 'Smith Family');
    const family = formatGuestSubtitle('Family', 'Smith Family');
    expect(smith).toBe('Smith · Smith Family');
    expect(family).toBe('Family · Smith Family');
    expect(smith).not.toBe(family);
  });

  it('does not treat a hyphen or an apostrophe inside a name as a separator', () => {
    expect(formatGuestSubtitle('Ann', 'Ann-Marie Smith')).toBe('Ann · Ann-Marie Smith');
    expect(formatGuestSubtitle('Ann', "Ann's Catering")).toBe("Ann · Ann's Catering");
    expect(formatGuestSubtitle('Ann-Marie Smith', 'Ann')).toBe('Ann-Marie Smith · Ann');
    expect(formatGuestSubtitle("Ann's table", 'Ann')).toBe("Ann's table · Ann");
  });

  it('never hides a billed customer that the label merely mentions', () => {
    expect(formatGuestSubtitle('Not Acme - Bob pays cash', 'Acme')).toBe('Not Acme - Bob pays cash · Acme');
    expect(formatGuestSubtitle('Walk In Bob', 'Walk In')).toBe('Walk In Bob · Walk In');
    expect(formatGuestSubtitle('Acmeco (Rm 2)', 'Acme')).toBe('Acmeco (Rm 2) · Acme');
    expect(formatGuestSubtitle('Ann', 'Joanne Smith')).toBe('Ann · Joanne Smith');
    expect(formatGuestSubtitle('Room 4', 'Room 40')).toBe('Room 4 · Room 40');
  });

  it('reads composed and decomposed accents as the same letters, and an accent as a letter', () => {
    expect(formatGuestSubtitle('José Smith (Rm 2)', 'José Smith')).toBe('José Smith (Rm 2)');
    expect(formatGuestSubtitle('José Smith', 'Jose')).toBe('José Smith · Jose');
  });

  it('compares names the same way whatever language the till runs in', () => {
    // On a Turkish runtime toLocaleLowerCase() turns "I" into a dotless "ı".
    const localeLower = String.prototype.toLocaleLowerCase;
    const turkish = vi.spyOn(String.prototype, 'toLocaleLowerCase').mockImplementation(function (this: string) {
      return localeLower.call(this, 'tr');
    });
    try {
      expect(formatGuestSubtitle('IRMAK OTEL (Rm 2)', 'irmak otel')).toBe('IRMAK OTEL (Rm 2)');
    } finally {
      turkish.mockRestore();
    }
  });

  it('falls back to whichever side is present', () => {
    expect(formatGuestSubtitle('', 'Walk In')).toBe('Walk In');
    expect(formatGuestSubtitle(null, 'Walk In')).toBe('Walk In');
    expect(formatGuestSubtitle('Seat 2', undefined)).toBe('Seat 2');
    expect(formatGuestSubtitle('   ', '')).toBe('');
  });
});

describe('formatStaffDisplayName', () => {
  it('prefers the full name the payload provides', () => {
    expect(formatStaffDisplayName('Samuel Otieno', 'samuel.otieno@domain.example')).toBe('Samuel Otieno');
  });

  it('turns an email-only user id into a readable name', () => {
    expect(formatStaffDisplayName(null, 'samuel.otieno@domain.example')).toBe('Samuel Otieno');
    expect(formatStaffDisplayName(undefined, 'mary_jane-doe@domain.example')).toBe('Mary Jane Doe');
  });

  it('drops a plus-address tag from the local part', () => {
    expect(formatStaffDisplayName('', 'sam+till2@domain.example')).toBe('Sam');
  });

  it('formats a name field that itself holds an email', () => {
    expect(formatStaffDisplayName('samuel.otieno@domain.example', null)).toBe('Samuel Otieno');
  });

  it('keeps a non-email user id as it is', () => {
    expect(formatStaffDisplayName(null, 'Administrator')).toBe('Administrator');
    expect(formatStaffDisplayName(null, 'Waiter')).toBe('Waiter');
  });

  it('returns an empty string when there is nothing to show', () => {
    expect(formatStaffDisplayName(null, null)).toBe('');
    expect(formatStaffDisplayName('  ', '')).toBe('');
  });
});
