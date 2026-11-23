import { describe, expect, it } from 'vitest';
import { formatGuestSubtitle, formatStaffDisplayName } from './order-display';

describe('formatGuestSubtitle', () => {
  it('shows the guest label alone when it already names the customer', () => {
    expect(formatGuestSubtitle('Dr. Elena Rostova (Rm 408)', 'Dr. Elena Rostova')).toBe('Dr. Elena Rostova (Rm 408)');
  });

  it('treats a label equal to the customer after trimming and case-folding as one name', () => {
    expect(formatGuestSubtitle('  dr. elena rostova ', 'Dr. Elena Rostova')).toBe('dr. elena rostova');
  });

  it('shows the customer alone when it already contains the guest label', () => {
    expect(formatGuestSubtitle('Elena Rostova', 'Dr. Elena Rostova')).toBe('Dr. Elena Rostova');
  });

  it('keeps both, label first, when they genuinely differ', () => {
    expect(formatGuestSubtitle('Guest Label', 'Corporate Account')).toBe('Guest Label · Corporate Account');
    expect(formatGuestSubtitle('Susan', 'Walk In')).toBe('Susan · Walk In');
  });

  it('matches whole words only, so a short label inside another word is kept', () => {
    expect(formatGuestSubtitle('Ann', 'Joanne Smith')).toBe('Ann · Joanne Smith');
    expect(formatGuestSubtitle('Al', 'Walk In')).toBe('Al · Walk In');
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
