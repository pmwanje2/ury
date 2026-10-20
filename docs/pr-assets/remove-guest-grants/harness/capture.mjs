import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

// Usage: node capture.mjs <out.png> <port>
const [outFile, port] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), 'ury-menu-capture-'));
const chrome = spawn('google-chrome', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

try {
  const file = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 100 && !existsSync(file); i += 1) await sleep(100);
  const debugPort = readFileSync(file, 'utf8').split('\n')[0];
  const target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve) => ws.addEventListener('open', resolve, { once: true }));
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    id += 1; pending.set(id, resolve); ws.send(JSON.stringify({ id, method, params }));
  });
  await send('Emulation.setDeviceMetricsOverride', { width: 420, height: 360, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: `http://127.0.0.1:${port}/harness` });
  await sleep(2500);
  const text = await send('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
  writeFileSync(outFile.replace('.png', '.txt'), text.result.result.value);
  const png = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(outFile, Buffer.from(png.result.data, 'base64'));
  ws.close();
  console.log(`captured ${outFile}`);
} finally {
  chrome.kill();
}
