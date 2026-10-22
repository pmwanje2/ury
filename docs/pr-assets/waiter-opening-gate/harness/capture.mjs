import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const [outDir, port] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), 'ury-waiter-opening-capture-'));
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

const waitFor = (text, absent = '') => `(async()=>{const want=${JSON.stringify(text)};const avoid=${JSON.stringify(absent)};for(let attempt=0;attempt<50;attempt+=1){if(document.readyState==='complete'&&document.body){const body=document.body.innerText||'';if(body.includes(want)&&(!avoid||!body.includes(avoid)))return;}await new Promise((resolve)=>setTimeout(resolve,250));}const seen=document.body&&document.body.innerText||'';throw new Error('missing text '+want+'; body='+seen.slice(0,800));})()`;

const shots = [
  { file: 'waiter-tables-till-open.png', url: '/pos/order?stage=waiter-open', text: 'Tables', absent: 'Loading notifications' },
  { file: 'waiter-till-not-open.png', url: '/pos/order?stage=till-closed', text: 'The till is not open' },
  { file: 'cashier-gate-kept.png', url: '/pos/order?stage=cashier', text: 'Till already open' },
];

try {
  const debugPort = await devtoolsPort();
  for (const shot of shots) {
    const tab = await page(debugPort);
    await tab.send('Emulation.setDeviceMetricsOverride', {
      width: 800, height: 1280, deviceScaleFactor: 1, mobile: false,
    });
    await tab.send('Page.navigate', { url: `http://127.0.0.1:${port}${shot.url}` });
    const ready = await tab.send('Runtime.evaluate', { expression: waitFor(shot.text, shot.absent || ''), awaitPromise: true });
    if (ready.result?.exceptionDetails) throw new Error(`${shot.file}: ${JSON.stringify(ready.result.exceptionDetails)}`);
    const body = await tab.send('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
    writeFileSync(join(outDir, shot.file.replace('.png', '.txt')), body.result.result.value);
    const png = await tab.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(join(outDir, shot.file), Buffer.from(png.result.data, 'base64'));
    tab.close();
    console.log(`captured ${shot.file}`);
  }
} finally {
  chrome.kill();
}
