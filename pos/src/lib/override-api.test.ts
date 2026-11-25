import { execFileSync } from 'node:child_process';
import { afterEach, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { authoriseOverride, setMyPin } from './override-api';

afterEach(() => vi.restoreAllMocks());

// Set URY_FRAPPE_TEST_PYTHON to the Python interpreter in a Frappe bench.
const frappePython = process.env.URY_FRAPPE_TEST_PYTHON;

it.skipIf(!frappePython)('Frappe masks both outgoing PIN fields in pre-hook error metadata', async () => {
  const post = vi.spyOn(call, 'post').mockResolvedValue({ message: { ok: true } });
  const pin = '4829';
  await setMyPin('fixture-password', pin);
  await authoriseOverride({
    posProfile: 'Main Till',
    privilege: 'over_short_close',
    approver: 'manager@example.test',
    pin,
    referenceDoctype: 'POS Opening Entry',
    referenceName: 'OPEN-1',
    claim: {},
  });

  const payloads = post.mock.calls.map(([, payload]) => payload);
  const evidence = execFileSync(frappePython!, ['-B', '-c', `
import json
import sys
from types import SimpleNamespace

import frappe
from frappe.utils.error import get_error_metadata

frappe.local.request = SimpleNamespace(method="POST", path="/api/method/fixture", referrer=None)
frappe.local.session = frappe._dict(user="cashier@example.test")
payloads, pin = json.load(sys.stdin)
results = []
for payload in payloads:
    frappe.local.form_dict = frappe._dict(payload)
    fields = [key for key, value in payload.items() if value == pin]
    snapshot_form = json.loads(get_error_metadata())["form_dict"]
    results.append((fields, all(snapshot_form[field] == "********" for field in fields)))
print(json.dumps(results))
`], { input: JSON.stringify([payloads, pin]), encoding: 'utf-8' });
  expect(JSON.parse(evidence)).toEqual([
    [['pin_secret'], true],
    [['pin_secret'], true],
  ]);
});
