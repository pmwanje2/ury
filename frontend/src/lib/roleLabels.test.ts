import { expect, it } from 'vitest';
import { roleOption } from './roleLabels';

it.each([
  ['URY Manager', 'Restaurant POS Manager'],
  ['URY Captain', 'Restaurant POS Captain'],
  ['URY Cashier', 'Restaurant POS Cashier'],
  ['URY Admin', 'Restaurant POS Admin'],
  ['POS Manager', 'POS Manager'],
])('keeps the Frappe option value %s when displaying %s', (value, label) => {
  expect(roleOption(value)).toEqual({ value, label });
});
