import { expect, it } from 'vitest';
import en from './locales/en.json';
import fr from './locales/fr.json';
import es from './locales/es.json';
import ar from './locales/ar.json';

type Catalog = { floor?: Record<string, string> };

it.each([['en', en], ['fr', fr], ['es', es], ['ar', ar]] as const)(
  'translates the entire floor lifecycle workflow in %s',
  (_language, catalog) => {
    const strings = (catalog as Catalog).floor;
    const english = (en as Catalog).floor;
    for (const key of ['seat', 'course', 'split_by_seat', 'split_even', 'split_manual', 'share',
      'dirty', 'check_printed', 'mark_clean', 'pay', 'waitlist', 'elapsed']) {
      expect(strings?.[key]?.trim(), key).toBeTruthy();
    }
    expect(Object.keys(strings ?? {}).sort()).toEqual(Object.keys(english ?? {}).sort());
    for (const [key, value] of Object.entries(strings ?? {})) {
      expect(value.trim()).toBeTruthy();
      expect(value.match(/{{\w+}}/g)?.sort()).toEqual(english?.[key].match(/{{\w+}}/g)?.sort());
    }
  },
);
