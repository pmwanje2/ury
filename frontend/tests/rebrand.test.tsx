import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { WizardLayout } from '../src/components/setup/WizardLayout';
import form from '../src/data/forms/setup.json';
import schema from '../src/data/schemas/pos_profile.json';

it('uses site identity in the setup wizard', () => {
  vi.stubGlobal('frappe', { boot: { ury_brand: { name: 'Sample', logo: '/files/sample.png' } } });
  try {
    render(<WizardLayout step={1}>Setup</WizardLayout>);
    expect(screen.getByRole('img', { name: 'Sample' })).toHaveAttribute('src', '/files/sample.png');
  } finally { vi.unstubAllGlobals(); }
});

it('does not preselect an operator or suggest the vendor name in setup', () => {
  expect(schema.properties.restaurant).not.toHaveProperty('default');
  for (const field of form.fields.filter(f => ['company_name', 'company_abbr'].includes(f.id))) {
    expect(field.placeholder || '').not.toContain('URY');
  }
});
