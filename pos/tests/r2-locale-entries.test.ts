import { describe, expect, it } from 'vitest';
import en from '../src/i18n/locales/en.json';
import ar from '../src/i18n/locales/ar.json';
import es from '../src/i18n/locales/es.json';
import fr from '../src/i18n/locales/fr.json';

// Strings added for the open-check covers and the unconfirmed stock count
// must exist in every POS locale, with the same placeholders as English.

type Tree = { [key: string]: string | Tree };

function lookup(tree: Tree, key: string): string | undefined {
  let node: string | Tree | undefined = tree;
  for (const part of key.split('.')) node = typeof node === 'object' ? node[part] : undefined;
  return typeof node === 'string' ? node : undefined;
}

const placeholders = (value: string) => (value.match(/\{\{\w+\}\}/g) ?? []).sort().join(',');

const KEYS = { 'order.covers': '{{count}}', 'menu.qty_left_unconfirmed': '{{qty}}' };

describe.each([['en', en], ['ar', ar], ['es', es], ['fr', fr]] as const)('%s locale', (_language, locale) => {
  it.each(Object.entries(KEYS))('has %s with its placeholder', (key, placeholder) => {
    const value = lookup(locale as Tree, key);
    expect(value?.trim(), key).toBeTruthy();
    expect(placeholders(value!)).toBe(placeholder);
  });
});
