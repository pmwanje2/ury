import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import postcss, { type AtRule } from 'postcss';
import tailwindcss from 'tailwindcss';
import preset from '@ury/ui/tailwind-preset';

const POS_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

const compiled = new Map<string, Promise<string>>();

function compile(sources: string[]): Promise<string> {
  const key = sources.join('\n');
  if (!compiled.has(key)) {
    compiled.set(key, postcss([tailwindcss({
      presets: [preset],
      content: sources.map((path) => ({
        raw: readFileSync(resolve(POS_ROOT, path), 'utf8'),
        extension: 'tsx',
      })),
    })]).process('@tailwind utilities;', { from: undefined }).then((result) => result.css));
  }
  return compiled.get(key)!;
}

/**
 * Whether a width media query applies at a viewport `width` (CSS px). Any
 * other condition (print, hover, colour scheme) never applies.
 */
function mediaApplies(params: string, width: number): boolean {
  const below = /^not all and \(min-width:\s*([\d.]+)px\)$/.exec(params.trim());
  if (below) return width < Number(below[1]);
  return params.trim().split(/\s+and\s+/).every((condition) => {
    const min = /^\(min-width:\s*([\d.]+)px\)$/.exec(condition);
    if (min) return width >= Number(min[1]);
    const max = /^\(max-width:\s*([\d.]+)px\)$/.exec(condition);
    if (max) return width <= Number(max[1]);
    return false;
  });
}

/**
 * Compiles the Tailwind utilities used by the given sources (paths relative to
 * pos/) and installs them in the jsdom document, so tests can read the styles
 * an element really gets through getComputedStyle. jsdom has no layout engine
 * and ignores media queries, so by default only base (unprefixed)
 * declarations apply. Pass `viewport` to resolve the width media queries for
 * that viewport width instead: the responsive rules that apply stay in cascade
 * order and the rest are dropped. Remove the returned element to undo it.
 */
export async function installTailwindUtilities(
  sources: string[],
  options: { viewport?: number } = {},
): Promise<HTMLStyleElement> {
  const root = postcss.parse(await compile(sources));
  if (options.viewport !== undefined) {
    const media: AtRule[] = [];
    root.walkAtRules('media', (atRule) => { media.push(atRule); });
    for (const atRule of media) {
      if (mediaApplies(atRule.params, options.viewport)) atRule.replaceWith(atRule.nodes ?? []);
      else atRule.remove();
    }
  }
  const style = document.createElement('style');
  style.textContent = root.toString();
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

/** Whether a stylesheet rule or the inline style sets `property` on this very element. */
function declares(element: Element, property: string): boolean {
  if ((element as HTMLElement).style?.getPropertyValue(property)) return true;
  for (const sheet of Array.from(document.styleSheets)) {
    for (const rule of Array.from(sheet.cssRules)) {
      if (!(rule instanceof CSSStyleRule) || !rule.style.getPropertyValue(property)) continue;
      try {
        if (element.matches(rule.selectorText)) return true;
      } catch { /* a pseudo-element selector never matches an element */ }
    }
  }
  return false;
}

/**
 * The value an inherited property really has on `element`. jsdom computes
 * white-space without inheritance (a span inside a `whitespace-nowrap` button
 * reads "normal"), so take it from the nearest element that sets it.
 */
export function inheritedStyle(element: Element, property: string): string {
  for (let node: Element | null = element; node; node = node.parentElement) {
    if (declares(node, property)) return getComputedStyle(node).getPropertyValue(property);
  }
  return getComputedStyle(element).getPropertyValue(property);
}

/** The styles that decide whether text stays whole when its box is narrow. */
export function wrapStyle(element: Element) {
  return {
    whiteSpace: inheritedStyle(element, 'white-space'),
    textOverflow: getComputedStyle(element).textOverflow,
    overflowWrap: inheritedStyle(element, 'overflow-wrap'),
  };
}

/**
 * Text that stays readable on a touch screen: it wraps onto further lines, and
 * a long unbroken token breaks too, instead of ending in an ellipsis that only
 * a mouse-hover tooltip could reveal.
 */
export const WRAPS = { whiteSpace: 'normal', textOverflow: 'clip', overflowWrap: 'break-word' };
