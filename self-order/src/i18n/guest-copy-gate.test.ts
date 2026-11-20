import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

// The language gate for every guest-facing file (review B2 on #113). It walks
// the whole self-order source tree, so a new guest screen is covered the day it
// is added. Any English text node or copy-shaped string literal outside the
// catalog fails, unless it is in the closed lists below.
const here = dirname(fileURLToPath(import.meta.url))
const src = join(here, '..')

// Staff-only or never shown to a guest. Adding a file here is a reviewed decision.
const NOT_GUEST_FACING = new Set([
  // Staff binds a portable tablet with a PIN; the guest never sees it.
  'layouts/PortableTabletAssignment.tsx',
  // Developer console output for the realtime socket; never rendered.
  'lib/realtime.ts',
])

// Machine values the server or the layout router compares against; never shown.
const NOT_COPY = new Set([
  'Landscape Kiosk',
  'Portrait Kiosk',
  'QR Pickup',
  'Already Requested',
  'Content-Type',
])

function guestSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return entry.name === 'test' || entry.name === 'i18n' ? [] : guestSources(path)
    if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name) || entry.name.endsWith('.d.ts')) return []
    return [path]
  })
}

// Copy-shaped: a capitalised word, or a word ending a sentence. Identifiers,
// class names, method paths and storage keys are lowercase and never match.
const COPY = /\b[A-Z][a-z]+\b|[A-Za-z]{2,}[.?!…:]$/

function englishCopyIn(name: string, text: string): string[] {
  const source = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const found: string[] = []
  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node)) {
      if (/[A-Za-z]/.test(node.text)) found.push(node.text.trim())
    } else if (
      ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)
    ) {
      const parent = node.parent
      const structural = ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent) ||
        ts.isLiteralTypeNode(parent) || ts.isExternalModuleReference(parent) ||
        (ts.isPropertyAssignment(parent) && parent.name === node)
      if (!structural && COPY.test(node.text) && !NOT_COPY.has(node.text)) found.push(node.text)
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return found
}

describe('every guest-facing file takes its copy from the catalog', () => {
  const files = guestSources(src).map((file) => relative(src, file).split('\\').join('/')).sort()

  it('scans the shared screens the review named', () => {
    for (const rel of [
      'layouts/shared/CartPage.tsx',
      'layouts/shared/OrderStatusScreen.tsx',
      'layouts/shared/ProductDetail.tsx',
      'layouts/shared/SearchBar.tsx',
      'layouts/shared/CheckoutScreen.tsx',
      'layouts/MobileQRLayout.tsx',
    ]) expect(files, rel).toContain(rel)
    for (const rel of NOT_GUEST_FACING) expect(files, `${rel} no longer exists`).toContain(rel)
  })

  it('the scanner reports each shape of English copy', () => {
    // Proves the gate can fail on a text node, an attribute, a constant and a template.
    for (const shape of [
      '<h1>Your Cart</h1>',
      '<input placeholder="Search menu..." />',
      "const label = 'Sold out'",
      'const a = `Remove one ${name}`',
    ]) expect(englishCopyIn('sample.tsx', shape), shape).not.toEqual([])
    expect(englishCopyIn('sample.tsx', "<p>{t('cart.title')}</p>")).toEqual([])
  })

  it.each(files.filter((rel) => !NOT_GUEST_FACING.has(rel)))('%s has no English copy outside the catalog', (rel) => {
    expect(englishCopyIn(rel, readFileSync(join(src, rel), 'utf8'))).toEqual([])
  })
})
