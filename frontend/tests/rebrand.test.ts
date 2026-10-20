import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const root = resolve(import.meta.dirname, '..', '..');
const read = (path: string) => readFileSync(resolve(root, path), 'utf8');

describe('admin dashboard rebrand contracts', () => {
  it('makes the dashboard entry title and favicon brand-driven', () => {
    const source = read('frontend/index.html');
    expect(source).toContain('brand.favicon');
    expect(source).toContain('brand.name');
  });

  it('uses the configured brand in dashboard header and setup wizard', () => {
    const header = read('frontend/src/components/layout/Header.tsx');
    const wizard = read('frontend/src/components/setup/WizardLayout.tsx');
    expect(header).toContain('resolveBrand');
    expect(header).toContain('alt={brand.name}');
    expect(header).not.toContain('alt="URY Logo"');
    expect(wizard).toContain('resolveBrand');
    expect(wizard).toContain('alt={brand.name}');
    expect(wizard).toContain('{brand.name} · {version}');
    expect(wizard).not.toContain('alt="URY Logo"');
  });

  it('labels dashboard tiles neutrally while retaining their routes', () => {
    const source = read('frontend/src/pages/Dashboard/KPIGrid.tsx');
    expect(source).toContain('label="POS"');
    expect(source).toContain('label="Kitchen Display"');
    expect(source).not.toContain('label="URY POS"');
    expect(source).not.toContain('label="URY MOSAIC"');
  });

  it('uses POS role labels without changing role values', () => {
    const sources = [
      read('frontend/src/components/setup/sections/UserSection.tsx'),
      read('frontend/src/pages/Dashboard/UserPage.tsx'),
      read('frontend/src/pages/Dashboard/QuickActions.tsx'),
    ].join('\n');
    for (const [value, label] of [
      ['URY Manager', 'POS Manager'],
      ['URY Captain', 'POS Captain'],
      ['URY Cashier', 'POS Cashier'],
      ['URY Admin', 'POS Admin'],
    ]) {
      expect(sources).toContain(`value: '${value}'`);
      expect(sources).toContain(`label: '${label}'`);
    }
  });

  it('uses neutral setup defaults and manager wording', () => {
    const setup = read('frontend/src/data/forms/setup.json');
    const profile = read('frontend/src/data/schemas/pos_profile.json');
    const guard = read('frontend/src/components/RoleGuard.tsx');
    expect(setup).toContain('e.g. Company');
    expect(setup).toContain('e.g. CO');
    expect(profile).toContain('default": "Restaurant"');
    expect(guard).toContain('You need the manager role');
    expect(guard).not.toContain('You need the URY Manager role');
  });
});
