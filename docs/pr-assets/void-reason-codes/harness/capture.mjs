import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const [outDir, port] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), 'ury-void-reason-capture-'));
const chrome = spawn('google-chrome', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function devtoolsPort() {
  const file = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 100 && !existsSync(file); i += 1) await sleep(100);
  if (!existsSync(file)) throw new Error('Chrome DevTools port did not open');
  return readFileSync(file, 'utf8').split('\n')[0];
}

async function page(debugPort) {
  const target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    id += 1;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  return { send, close: () => ws.close() };
}

async function evalOrThrow(tab, expression) {
  const result = await tab.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (result.result?.exceptionDetails) {
    throw new Error(JSON.stringify(result.result.exceptionDetails));
  }
  return result.result?.result?.value;
}

try {
  const debugPort = await devtoolsPort();
  const tab = await page(debugPort);
  await tab.send('Emulation.setDeviceMetricsOverride', {
    width: 1280, height: 800, deviceScaleFactor: 1, mobile: false,
  });
  await tab.send('Page.navigate', { url: `http://127.0.0.1:${port}/harness` });
  const opened = await evalOrThrow(tab, `new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = () => {
      const card = [...document.querySelectorAll('h3')].find((node) => node.textContent.trim() === 'POS-INV-1001');
      if (card) { card.click(); }
      const cancel = document.querySelector('button[aria-label="Cancel order"]');
      if (cancel) { cancel.click(); resolve('opened'); return; }
      if (Date.now() - started > 15000) {
        reject(new Error('void dialog did not open: ' + document.body.innerText.slice(0, 500)));
        return;
      }
      setTimeout(tick, 200);
    };
    tick();
  })`);
  if (opened !== 'opened') throw new Error('dialog was not opened');
  const ready = await evalOrThrow(tab, `new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = () => {
      const select = document.querySelector('[role="dialog"] select');
      if (select && select.options.length >= 2) {
        resolve([...select.options].map((option) => option.textContent));
        return;
      }
      if (Date.now() - started > 5000) reject(new Error('select missing'));
      else setTimeout(tick, 100);
    };
    tick();
  })`);
  const text = await evalOrThrow(tab, 'document.body.innerText');
  const disclosed = `${text}\n\n[select options read from the real dialog select]\n${ready.join('\n')}\n`;
  writeFileSync(join(outDir, 'void-dialog-select.txt'), disclosed);
  const png = await tab.send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(outDir, 'void-dialog-select.png'), Buffer.from(png.result.data, 'base64'));
  tab.close();
  console.log('captured void-dialog-select.png');
  console.log(ready.join(' | '));
} finally {
  chrome.kill();
}
