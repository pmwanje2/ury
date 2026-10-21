import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const [outDir, port] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), 'ury-floor-session-capture-'));
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

const clickCard = (text) => `(async()=>{let card;for(let attempt=0;attempt<30&&!card;attempt+=1){card=[...document.querySelectorAll('.cursor-pointer.h-56')].find((candidate)=>candidate.textContent.includes(${JSON.stringify(text)}));if(!card)await new Promise((resolve)=>setTimeout(resolve,300));}if(!card)throw new Error('missing card '+${JSON.stringify(text)}+'; body='+document.body.innerText.slice(0,400));card.click();await new Promise((resolve)=>setTimeout(resolve,600));})()`;

const shots = [
  {
    file: 'unsent-items-prompt.png', url: '/harness?stage=menu', width: 800, height: 1280,
    actions: [clickText('Menu'), clickCard('Passion juice'), clickText('Order'), clickText('Back to Tables')],
  },
  {
    file: 'unsent-items-kept.png', url: '/harness?stage=menu', width: 800, height: 1280,
    actions: [clickText('Menu'), clickCard('Passion juice'), clickText('Order'), clickText('Back to Tables'), clickText('Cancel')],
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
