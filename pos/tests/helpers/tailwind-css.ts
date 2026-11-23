import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import preset from '@ury/ui/tailwind-preset';

const POS_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

/**
 * Compiles the Tailwind utilities used by the given sources (paths relative to
 * pos/) and installs them in the jsdom document, so tests can read the styles
 * an element really gets through getComputedStyle. jsdom has no layout engine
 * and ignores media queries: assert base (unprefixed) declarations only.
 */
export async function installTailwindUtilities(sources: string[]): Promise<HTMLStyleElement> {
  const css = await postcss([tailwindcss({
    presets: [preset],
    content: sources.map((path) => ({
      raw: readFileSync(resolve(POS_ROOT, path), 'utf8'),
      extension: 'tsx',
    })),
  })]).process('@tailwind utilities;', { from: undefined });
  const style = document.createElement('style');
  style.textContent = css.css;
  document.head.appendChild(style);
  return style;
}

/** The styles that decide whether one line of text wraps, overflows or clips. */
export function truncationStyle(element: Element) {
  const style = getComputedStyle(element);
  return {
    whiteSpace: style.whiteSpace,
    overflow: style.overflow,
    textOverflow: style.textOverflow,
    minWidth: style.minWidth,
  };
}

/** One line, clipped with an ellipsis, and allowed to shrink below its text width. */
export const TRUNCATES = { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: '0px' };
