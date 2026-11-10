import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// Tankhill 2026-11-10: on a laptop the waiter order screen (Menu | Order side by
// side) grew with the 254-item menu, so the whole page scrolled. The order lines
// and the Total / Send Order bar sat at the top and bottom of a very long page,
// and the waiter had to scroll down to pick items and back up to see the order.
// The screen must be one viewport tall with each pane scrolling on its own, so
// the order and the total stay on screen while the menu scrolls.
const here = typeof __dirname === 'string' ? __dirname : dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(here, '../src/captain/pages/CaptainOrder.tsx'), 'utf8');

function classesOfLineContaining(marker: string): string[] {
  const line = source.split('\n').find((candidate) => candidate.includes(marker));
  expect(line, `line containing ${marker}`).toBeDefined();
  return (line!.match(/className="([^"]*)"/)?.[1] ?? '').split(/\s+/);
}

describe('Captain order screen fits the viewport', () => {
  it('bounds the main screen to the viewport height instead of growing with the menu', () => {
    const root = source.split('\n').find((line) => line.includes('<div className=') && line.includes('bg-gray-50 flex flex-col'));
    expect(root).toBeDefined();
    const classes = (root!.match(/className="([^"]*)"/)?.[1] ?? '').split(/\s+/);
    expect(classes).toContain('h-dvh');
    expect(classes).toContain('overflow-hidden');
    expect(classes).not.toContain('min-h-screen');
  });

  it('keeps the tablet panes and the mobile pane inside that height', () => {
    expect(classesOfLineContaining('flex-1 flex flex-col overflow-hidden')).toEqual(
      expect.arrayContaining(['flex-1', 'min-h-0', 'overflow-hidden', 'lg:hidden']),
    );
    expect(classesOfLineContaining('hidden lg:flex lg:flex-col')).toEqual(expect.arrayContaining(['lg:min-h-0']));
  });
});
