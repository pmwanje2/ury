import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const [outDir, port] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), 'ury-one-staff-capture-'));
const chrome = spawn('google-chrome', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function devtoolsPort() {
  const file = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 100 && !existsSync(file); i += 1) await sleep(100);
  return readFileSync(file, 'utf8').split('\n')[0];
}

async function page(debugPort) {
  const target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve) => ws.addEventListener('open', resolve, { once: true }));
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

const shots = [
  { file: 'paired-pin-pad-tablet.png', url: '/harness?screen=pad', width: 800, height: 1280 },
  {
    file: 'wrong-pin-phone.png', url: '/harness?screen=pad', width: 390, height: 844,
    actions: ["(async()=>{for(const digit of ['0','5','2','7']){const button=[...document.querySelectorAll('button')].find((candidate)=>candidate.textContent.trim()===digit);if(!button)throw new Error('missing digit '+digit);button.click();await new Promise((resolve)=>setTimeout(resolve,40));}})()"],
  },
  {
    file: 'username-fallback-phone.png', url: '/harness?screen=pad', width: 390, height: 844,
    actions: ["[...document.querySelectorAll('button')].find((button) => button.textContent.includes('username')).click()"],
  },
  { file: 'offline-pad-phone.png', url: '/harness?screen=offline', width: 390, height: 844 },
  { file: 'pad-ar-rtl-phone.png', url: '/harness?screen=pad&lang=ar', width: 390, height: 844 },
  { file: 'captain-tables-tablet.png', url: '/harness?screen=captain', width: 800, height: 1280 },
];

try {
  const debugPort = await devtoolsPort();
  for (const shot of shots) {
    const tab = await page(debugPort);
    await tab.send('Emulation.setDeviceMetricsOverride', {
      width: shot.width, height: shot.height, deviceScaleFactor: 1, mobile: false,
    });
    await tab.send('Page.navigate', { url: `http://127.0.0.1:${port}${shot.url}` });
    await sleep(1800);
    for (const expression of shot.actions ?? []) {
      const result = await tab.send('Runtime.evaluate', { expression, awaitPromise: true });
      if (result.result?.exceptionDetails) throw new Error(`${shot.file}: ${JSON.stringify(result.result.exceptionDetails)}`);
      await sleep(500);
    }
    const text = await tab.send('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
    writeFileSync(join(outDir, shot.file.replace('.png', '.txt')), text.result.result.value);
    const png = await tab.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(join(outDir, shot.file), Buffer.from(png.result.data, 'base64'));
    tab.close();
    console.log(`captured ${shot.file}`);
  }
} finally {
  chrome.kill();
}
