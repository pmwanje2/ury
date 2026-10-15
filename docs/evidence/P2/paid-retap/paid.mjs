import {createRequire} from 'node:module';
import {readFileSync, writeFileSync, mkdtempSync, rmSync} from 'node:fs';
const puppeteer = createRequire('/tmp/o3-chrome/package.json')('puppeteer-core');
const demo = JSON.parse(readFileSync('/tmp/ury-p2-hKTJTxtp/demo-r3.log','utf8').match(/P2_DEMO=(.*)/)[1]);
const base = 'http://p2.localhost:19372';
const dir = '/tmp/ury-p2-hKTJTxtp/shots-r3';
const profile = mkdtempSync('/tmp/ury-p2-hKTJTxtp/chrome-r3-');
const browser = await puppeteer.launch({executablePath:'/usr/bin/google-chrome', headless:true, userDataDir:profile,
  args:['--no-sandbox','--disable-dev-shm-usage','--host-resolver-rules=MAP p2.localhost 127.0.0.1']});
const page = await browser.newPage();
await page.setViewport({width:640, height:1100});
const responses = []; const navigations = []; let shotName = null;
await page.exposeFunction('p2Capture', v => responses.push(v));
await page.evaluateOnNewDocument(() => {
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    this.addEventListener('load', () => {
      if (String(url).includes('create_payment_request')) window.p2Capture({status:this.status, body:JSON.parse(this.responseText)});
    });
    return open.call(this, method, url, ...args);
  };
});
await page.setRequestInterception(true);
page.on('request', async r => {
  if (r.isNavigationRequest() && r.url().startsWith('https://cybqa.pesapal.com/')) {
    navigations.push(r.url());
    await new Promise(res => setTimeout(res, 800));
    if (shotName) await page.screenshot({path:`${dir}/${shotName}`, fullPage:true});
    r.abort();
  } else r.continue();
});
const pause = ms => new Promise(r => setTimeout(r, ms));
const body = () => page.evaluate(() => document.body.innerText);
async function click(match) {
  const ok = await page.evaluate(m => { const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===m || b.textContent.includes(m) && m.startsWith('P2 Tea')); if(!b||b.disabled) return false; b.click(); return true; }, match);
  if (!ok) throw Error('no button ' + match + ': ' + await body());
}
async function ipn(pr) {
  const r = await fetch('http://127.0.0.1:19372/api/method/ea_payments.ea_payments.doctype.pesapal_settings.pesapal_settings.ipn?' +
    new URLSearchParams({OrderTrackingId:'TRACK-'+pr, OrderMerchantReference:pr}), {headers:{'X-Frappe-Site-Name':'p2.localhost'}});
  return {status:r.status, body:await r.json()};
}
const out = {};
try {
  await page.goto(`${base}/order?t=${demo.token}`, {waitUntil:'networkidle2'});
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b=>b.textContent.includes('P2 Tea')), {timeout:20000});
  await click('P2 Tea'); await click('Place Order');
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='Pay Online'), {timeout:20000});
  await click('Pay Online');
  await page.waitForFunction(n => window.__nav || true, {}, 0); await pause(2500);
  out.first = responses[0]; out.firstNavigation = navigations[0];
  out.firstIpn = await ipn(out.first.body.message.payment_request);

  await page.goto(`${base}/order`, {waitUntil:'networkidle2'});
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='Pay Online'), {timeout:20000});
  const navBefore = navigations.length;
  await click('Pay Online');
  await page.waitForFunction(() => document.body.innerText.includes('Paid ('), {timeout:10000});
  await pause(1500);
  out.paidRetap = responses[1]; out.paidRetapNavigations = navigations.length - navBefore;
  await page.screenshot({path:`${dir}/guest-paid-retap.png`, fullPage:true});
  if (out.paidRetap.body.message.payment_url !== null || out.paidRetapNavigations !== 0) throw Error('paid retap redirected');

  await click('P2 Tea'); await click('Place Order');
  await page.waitForFunction(() => document.body.innerText.includes('24000'), {timeout:20000});
  shotName = 'guest-remainder.png';
  await click('Pay Online'); await pause(3000);
  out.remainder = responses[2]; out.remainderNavigation = navigations.at(-1);
  const m = out.remainder.body.message;
  if (m.amount !== 12000 || m.paid_amount !== 12000 || m.remaining_amount !== 12000 || out.remainderNavigation !== m.payment_url
    || m.payment_request === out.first.body.message.payment_request) throw Error('bad remainder ' + JSON.stringify(m));
  console.log('P2_PAID_REMAINDER_OK', JSON.stringify(out));
} catch (e) {
  console.error(e, JSON.stringify(out)); console.error(await body().catch(()=>''));
  await page.screenshot({path:`${dir}/failure.png`, fullPage:true}).catch(()=>{}); process.exitCode = 1;
} finally {
  writeFileSync(`${dir}/paid-remainder-result.json`, JSON.stringify({...out, navigations}, null, 2));
  await browser.close(); rmSync(profile, {recursive:true, force:true});
}
