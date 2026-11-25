// @vitest-environment node

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { afterEach, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { authoriseOverride, setMyPin } from './override-api';

afterEach(() => vi.restoreAllMocks());

// Set URY_FRAPPE_TEST_PYTHON to the Python interpreter in a Frappe bench.
const frappePython = process.env.URY_FRAPPE_TEST_PYTHON;

it.skipIf(!frappePython)('Frappe masks supported PIN credentials before hooks and rejects the legacy wire field', async () => {
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

  const payloads = post.mock.calls.map(([method, payload]) => [method, payload]);
  const evidence = execFileSync(frappePython!, ['-B', '-c', `
import json
import sys
from unittest.mock import MagicMock, patch

import frappe
from frappe.app import make_form_dict
from frappe.utils.error import get_error_metadata
from werkzeug.wrappers import Request

from ury.ury_pos import override

requests, pin = json.load(sys.stdin)
enrolment_method, enrolment = requests[0]
requests.append((enrolment_method, {
    "pin_secret": enrolment["pin_secret"], "current_password": enrolment["password"],
}))
masks = []
refusals = []

def load_form(method, payload):
    request = Request.from_values(path="/api/method/" + method, method="POST", json=payload)
    frappe.local.request = request
    frappe.local.session = frappe._dict(user="cashier@example.test")
    frappe.local.flags = frappe._dict()
    frappe.local.message_log = []
    make_form_dict(request)

for method, payload in requests:
    load_form(method, payload)
    fields = sorted(key for key, value in payload.items() if value in (pin, "fixture-password"))
    snapshot_form = json.loads(get_error_metadata())["form_dict"]
    masks.append((fields, all(snapshot_form[field] == "********" for field in fields)))

def translate(message, *args, **kwargs):
    return message

with (
    patch.object(frappe, "_", side_effect=translate),
    patch("frappe.utils.messages._", side_effect=translate),
    patch.object(frappe, "logger", return_value=MagicMock()),
):
    for method, payload in requests[:2]:
        endpoint = getattr(override, method.rsplit(".", 1)[1])
        for mixed_fields in (False, True):
            legacy_payload = {**payload, "pin": payload["pin_secret"]}
            if not mixed_fields:
                legacy_payload.pop("pin_secret")
            load_form(method, legacy_payload)
            frappe.call(override.scrub_pin_request)
            try:
                frappe.call(endpoint, **frappe.form_dict)
            except frappe.ValidationError as error:
                refusals.append((
                    "Refresh the till" in str(error),
                    bool(getattr(error, "skip_error_log", False)),
                    not ({"pin", "pin_secret", "password", "current_password"} & frappe.form_dict.keys()),
                ))
            else:
                refusals.append((False, False, False))

print(json.dumps({"masks": masks, "refusals": refusals}))
`], {
    cwd: fileURLToPath(new URL('../../../', import.meta.url)),
    input: JSON.stringify([payloads, pin]),
    encoding: 'utf-8',
  });
  expect(JSON.parse(evidence)).toEqual({
    masks: [
      [['password', 'pin_secret'], true],
      [['pin_secret'], true],
      [['current_password', 'pin_secret'], true],
    ],
    refusals: Array.from({ length: 4 }, () => [true, true, true]),
  });
});
