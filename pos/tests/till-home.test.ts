import { expect, it } from 'vitest';
import { homePath } from '../src/lib/home-route';

it.each([
  [['URY Captain'], '/order'],
  [['URY Cashier'], '/orders'],
  [['Floor Lead'], '/orders'],
  [['URY Manager'], '/orders'],
  [['System Manager'], '/orders'],
  [['URY Captain', 'URY Cashier'], '/orders'],
  [['URY Captain', 'URY Manager'], '/orders'],
  [['URY Captain', 'Desk User'], '/orders'],
])('routes %j to %s', (roles, path) => {
  expect(homePath({ name: 'operator', roles: roles as string[] })).toBe(path);
});
