const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { execFileSync } = require('node:child_process');
const { resolve } = require('node:path');
const test = require('node:test');

const css = readFileSync('packages/ui/src/styles/theme.css', 'utf8');
const blocks = Object.fromEntries([':root', '.dark'].map((selector) => {
  const body = css.match(new RegExp(selector.replace('.', '\\.') + '\\s*\\{([^}]+)\\}'))[1];
  return [selector, Object.fromEntries(Array.from(body.matchAll(/(--[\w-]+):\s*([^;]+);/g), (match) => [match[1], match[2]]))];
}));

function rgb(hsl) {
  const [h, s, l] = hsl.split(' ').map(Number.parseFloat);
  const a = s / 100 * Math.min(l / 100, 1 - l / 100);
  return [0, 8, 4].map((n) => {
    const k = (n + h / 30) % 12;
    return l / 100 - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  });
}
test('Standard blue tokens render the exact stock Tailwind palette', () => {
  const stockBlue = require('tailwindcss/colors').blue;
  for (const [step, hex] of Object.entries(stockBlue)) {
    const value = blocks[':root'][`--blue-${step}`];
    assert.ok(value, `Standard is missing --blue-${step}`);
    const expected = hex.slice(1).match(/../g).map((channel) => Number.parseInt(channel, 16));
    assert.deepEqual(rgb(value).map((channel) => Math.round(channel * 255)), expected, `blue-${step}`);
  }
});

test('shared Tailwind blue colours consume theme variables with opacity support', async () => {
  const preset = (await import('../../packages/ui/tailwind-preset.js')).default;
  for (const step of [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]) {
    assert.equal(preset.theme.extend.colors.blue?.[step], `hsl(var(--blue-${step}) / <alpha-value>)`);
  }
});

test('the POS Tailwind config compiles every used blue utility through the shared tokens', async () => {
  const config = (await import('../tailwind.config.js')).default;
  const classes = [...new Set(execFileSync('rg', ['--no-filename', '-o',
    String.raw`\b(?:[\w-]+:)*[\w-]+-blue-\d+(?:/\d+)?`, 'pos/src'], { encoding: 'utf8' }).trim().split('\n'))];
  const result = await require('postcss')([require('tailwindcss')({
    ...config, content: config.content.map((path) => resolve('pos', path)),
  })]).process('@tailwind utilities;', { from: undefined });
  const escapeClassName = require('tailwindcss/lib/util/escapeClassName').default;
  for (const name of classes) {
    const rules = [];
    result.root.walkRules((rule) => {
      if (rule.selector.split(/[\s,]+/).includes(`.${escapeClassName(name)}`)
          || rule.selector.startsWith(`.${escapeClassName(name)}:`)) rules.push(rule);
    });
    assert.ok(rules.length, `POS build is missing ${name}`);
    const step = name.match(/blue-(\d+)/)[1];
    assert.ok(rules.some((rule) => rule.nodes.some((node) => node.value?.includes(`var(--blue-${step})`))),
      `${name} must consume --blue-${step}, not stock hard-coded blue`);
    if (name.includes('/')) {
      const alpha = Number(name.split('/')[1]) / 100;
      assert.ok(rules.some((rule) => rule.nodes.some((node) =>
        node.value?.includes(`hsl(var(--blue-${step}) / ${alpha})`))), `${name} must preserve its alpha`);
    }
  }
});
