import {readFileSync, writeFileSync} from 'node:fs';
const demo = JSON.parse(readFileSync('/tmp/ury-p2-hKTJTxtp/demo-r2.log','utf8').match(/P2_DEMO=(.*)/)[1]);
const base = 'http://127.0.0.1:19372/api/method/ury.ury.api.self_ordering.';
const headers = {'X-Frappe-Site-Name': 'p2.localhost', 'Content-Type': 'application/json'};
async function call(method, body, get = false) {
  const url = base + method + (get ? '?' + new URLSearchParams(body) : '');
  const r = await fetch(url, get ? {headers} : {method: 'POST', headers, body: JSON.stringify(body)});
  return {status: r.status, location: r.headers.get('location'), body: await r.json()};
}
const context = await call('get_ordering_context', {token: demo.token}, true);
const session = context.body.message.session;
const order = await call('add_customer_items', {session, items: [{item: demo.item, qty: 1}]});
const first = await call('create_payment_request', {session});
const second = await call('create_payment_request', {session, amount: 1});
const status = await call('get_payment_status', {session}, true);
const out = {order: order.status, first, second, status};
writeFileSync('/tmp/ury-p2-hKTJTxtp/retap-result.json', JSON.stringify(out, null, 2));
const a = first.body.message, b = second.body.message, s = status.body.message;
if (first.status !== 200 || second.status !== 200 || first.location || second.location
  || a.payment_request !== b.payment_request || a.payment_url !== b.payment_url
  || b.amount !== 12000 || s.payment_url !== a.payment_url) {
  console.error(JSON.stringify(out, null, 2)); process.exit(1);
}
console.log('P2_RETAP_OK', JSON.stringify({payment_request: a.payment_request, payment_url: a.payment_url, status: s}));
