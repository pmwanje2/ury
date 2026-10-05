const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
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
function luminance(color) {
  const values = rgb(color).map((c) => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
}
function contrast(a, b) {
  const pair = [luminance(a), luminance(b)].sort((a, b) => b - a);
  return (pair[0] + 0.05) / (pair[1] + 0.05);
}

test('light primary and focus ring render Maximus purple #8a3ffc', () => {
  for (const token of ['--primary', '--primary-500', '--ring']) {
    assert.deepEqual(rgb(blocks[':root'][token]).map((c) => Math.round(c * 255)), [138, 63, 252], token);
  }
});
test('light and dark primary actions keep AA text contrast and visible focus rings', () => {
  for (const selector of [':root', '.dark']) {
    const tokens = blocks[selector];
    assert.ok(contrast(tokens['--primary'], tokens['--primary-foreground']) >= 4.5, `${selector}: primary text must pass AA`);
    assert.ok(contrast(tokens['--primary-500'], tokens['--white']) >= 4.5, `${selector}: white text on primary-500 must pass AA`);
    assert.ok(contrast(tokens['--ring'], tokens['--background']) >= 3, `${selector}: focus ring must be visible`);
    for (const step of [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]) {
      const [h] = tokens[`--primary-${step}`].split(' ').map(Number.parseFloat);
      assert.ok(h >= 260 && h <= 270, `${selector}: primary-${step} must stay in the purple palette`);
    }
  }
});
