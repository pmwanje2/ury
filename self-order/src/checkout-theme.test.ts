// @vitest-environment node
import { readFileSync } from 'node:fs'
import postcss from 'postcss'
import tailwindcss from 'tailwindcss'
import { expect, it } from 'vitest'

it('keeps the shared night palette in the compiled checkout stylesheet', async () => {
  const theme = readFileSync('../packages/ui/src/styles/theme.css', 'utf8')
  const output = await postcss([tailwindcss('./tailwind.config.js')])
    .process(theme + '\n@tailwind base;', { from: undefined })
  const palette = output.root.nodes.find(node =>
    node.type === 'rule' && node.selector === '.d' + 'ark')
  expect(palette?.toString()).toContain('--background:')
})
