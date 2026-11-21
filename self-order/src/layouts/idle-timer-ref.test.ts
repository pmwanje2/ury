// React 19 removed the zero-argument useRef overload (fork #52 c9889682).
// The kiosk/tablet idle timers must initialise their ref so the self-order
// TypeScript check stays clean on these files.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

describe('idle timer refs compile under React 19', () => {
  for (const layout of ['LandscapeKioskLayout', 'PortraitKioskLayout', 'TabletLayout']) {
    it(`${layout} initialises its idle timer ref`, () => {
      const source = readFileSync(resolve(__dirname, `${layout}.tsx`), 'utf8')
      expect(source).not.toMatch(/useRef<ReturnType<typeof setTimeout>>\(\)/)
      expect(source).toMatch(/useRef<ReturnType<typeof setTimeout> \| undefined>\(undefined\)/)
    })
  }
})
