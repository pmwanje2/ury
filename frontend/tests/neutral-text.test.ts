import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const read = (path: string) => readFileSync(resolve(import.meta.dirname, '..', 'src', path), 'utf8');

describe('user-visible restaurant wording', () => {
  it.each([
    ['pages/Dashboard/TablePage.tsx', 'No Restaurant configured for this branch — set one up first', 'No URY Restaurant configured'],
    ['pages/Dashboard/BranchPage.tsx', 'No Restaurant linked to this branch.', 'No URY Restaurant linked'],
    ['pages/Dashboard/PosProfilePage.tsx', "selectedProfile.company || 'Restaurant'", "'URY Restaurant'"],
    ['pages/Reports/ItemWisePurchaseHistory.tsx', 'not a Restaurant-specific workflow', 'URY-specific'],
  ])('%s uses neutral wording', (path, wanted, removed) => {
    const source = read(path);
    expect(source).toContain(wanted);
    expect(source).not.toContain(removed);
  });

  it('keeps the technical DocType names used in API calls', () => {
    expect(read('pages/Dashboard/TablePage.tsx')).toContain("doctype: 'URY Restaurant'");
    expect(read('pages/Dashboard/BranchPage.tsx')).toContain("doctype: 'URY Restaurant'");
  });
});
