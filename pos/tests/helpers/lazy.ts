import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect } from 'vitest';

// Import a module the offline theme adds, failing on a named assertion (not a
// Vite resolve error) while the module is still missing.
export async function lazyModule<T = any>(relativeToPos: string, what: string): Promise<T> {
  const file = resolve(relativeToPos);
  expect(existsSync(file), `${what} is not implemented (${relativeToPos})`).toBe(true);
  return import(/* @vite-ignore */ file) as Promise<T>;
}
