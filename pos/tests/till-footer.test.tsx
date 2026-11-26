import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it } from 'vitest';
import Footer from '../src/components/Footer';
import { initI18n } from '../src/i18n';

it.each(['en', 'fr', 'es', 'ar'])('links Orders and Dashboard in %s', async (locale) => {
  await initI18n(locale);
  const { footer } = (await import(`../src/i18n/locales/${locale}.json`)).default;
  render(<MemoryRouter><Footer /></MemoryRouter>);
  const links = screen.getAllByRole('link');
  expect(footer.orders).toBeTruthy();
  expect(footer.dashboard).toBeTruthy();
  expect(screen.getByRole('link', { name: footer.orders })).toBe(links[0]);
  expect(links[0]).toHaveAttribute('href', '/orders');
  expect(links[0].querySelector('.lucide-receipt-text')).toBeTruthy();
  const analytics = screen.getByRole('link', { name: footer.dashboard });
  expect(analytics).toHaveAttribute('href', '/dashboard');
  expect(analytics.querySelector('.lucide-gauge')).toBeTruthy();
});
