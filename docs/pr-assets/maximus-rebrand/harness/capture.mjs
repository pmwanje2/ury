// Drives headless Chrome over the DevTools protocol (Node stdlib only).
// Usage: node capture.mjs <outDir> <posPort> <frontendPort>
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [outDir, posPort, frontPort] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), 'maximus-capture-'));
const chrome = spawn('google-chrome', ['--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function devtoolsPort() {
  const file = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 100 && !existsSync(file); i++) await sleep(100);
  return readFileSync(file, 'utf8').split('\n')[0];
}

async function page(port) {
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener('open', r, { once: true }));
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  const send = (method, params = {}) => new Promise((r) => { pending.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
  return { send, close: () => ws.close() };
}

const shots = [
  { file: 'pos-loader.png', url: `http://127.0.0.1:${posPort}/harness?screen=loader`, width: 1440, height: 900 },
  // ScreenSizeProvider shows the dialog only below 320px, so render at 300px.
  { file: 'pos-classic-dialog.png', url: `http://127.0.0.1:${posPort}/harness?screen=gate`, width: 300, height: 760 },
  { file: 'dashboard-brand.png', url: `http://127.0.0.1:${frontPort}/ury/dashboard`, width: 1440, height: 900 },
  {
    file: 'role-dropdown.png', url: `http://127.0.0.1:${frontPort}/ury/user`, width: 1440, height: 900,
    actions: [
      "[...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Add User').click()",
      // Click the SearchableSelect chevron, as a user would, to open the options.
      "document.getElementById('role').parentElement.querySelector('svg').parentElement.click()",
    ],
  },
];

try {
  const port = await devtoolsPort();
  for (const shot of shots) {
    const tab = await page(port);
    await tab.send('Emulation.setDeviceMetricsOverride', { width: shot.width, height: shot.height, deviceScaleFactor: 1, mobile: false });
    await tab.send('Page.navigate', { url: shot.url });
    await sleep(4000);
    for (const expression of shot.actions ?? []) {
      const result = await tab.send('Runtime.evaluate', { expression });
      if (result.result?.exceptionDetails) throw new Error(`${shot.file}: ${expression}`);
      await sleep(800);
    }
    const text = await tab.send('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
    writeFileSync(join(outDir, shot.file.replace('.png', '.txt')), text.result.result.value);
    const png = await tab.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(join(outDir, shot.file), Buffer.from(png.result.data, 'base64'));
    tab.close();
    console.log('captured', shot.file);
  }
} finally {
  chrome.kill();
}
