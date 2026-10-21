import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const [outDir, port] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), 'ury-waiter-split-capture-'));
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

const clickText = (text) => `(async()=>{let button;for(let attempt=0;attempt<30&&!button;attempt+=1){button=[...document.querySelectorAll('button')].find((candidate)=>candidate.textContent.trim().includes(${JSON.stringify(text)})||candidate.getAttribute('aria-label')===${JSON.stringify(text)});if(!button)await new Promise((resolve)=>setTimeout(resolve,300));}if(!button)throw new Error('missing button '+${JSON.stringify(text)}+'; body='+document.body.innerText.slice(0,500));button.click();await new Promise((resolve)=>setTimeout(resolve,500));})()`;

const shots = [
  {
    file: 'waiter-menu-split-bill.png', url: '/harness?stage=menu', width: 800, height: 1280,
    actions: [clickText('More actions')],
  },
  {
    file: 'waiter-split-dialog.png', url: '/harness?stage=dialog', width: 800, height: 1280,
    actions: [clickText('More actions'), clickText('Split bill')],
  },
  { file: 'waiter-sibling-checks.png', url: '/harness?stage=siblings', width: 800, height: 1280 },
  {
    file: 'waiter-kitchen-alert.png', url: '/harness?stage=kitchen', width: 800, height: 1280,
    actions: ["(async()=>{await new Promise((resolve)=>setTimeout(resolve,4000));await fetch('/harness/notification-bump',{method:'POST'});const panel=document.querySelector('[aria-label=Kitchen]');const refresh=[...panel.querySelectorAll('button')].find((button)=>button.textContent.trim()==='Refresh');if(!refresh)throw new Error('missing Kitchen refresh');refresh.click();for(let attempt=0;attempt<24;attempt+=1){if(document.body.innerText.includes('Food ready: Table T-05'))break;await new Promise((resolve)=>setTimeout(resolve,250));}})()"],
  },
];

try {
  const debugPort = await devtoolsPort();
  for (const shot of shots) {
    const tab = await page(debugPort);
    await tab.send('Emulation.setDeviceMetricsOverride', {
      width: shot.width, height: shot.height, deviceScaleFactor: 1, mobile: false,
    });
    await tab.send('Page.navigate', { url: `http://127.0.0.1:${port}${shot.url}` });
    await sleep(2200);
    for (const expression of shot.actions ?? []) {
      const result = await tab.send('Runtime.evaluate', { expression, awaitPromise: true });
      if (result.result?.exceptionDetails) throw new Error(`${shot.file}: ${JSON.stringify(result.result.exceptionDetails)}`);
      await sleep(500);
    }
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
