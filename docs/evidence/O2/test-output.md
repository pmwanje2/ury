# O2 native bench evidence

Disposable Docker project `ury-fixes-o2s6-mijnpjnz`; only published address
`127.0.0.1:18723`. Demo site `ury.localhost`. Frappe 16.52.0,
ERPNext 16.50.0, Python 3.14.8, Node 24.21.0, MariaDB 11.8, Redis 6.2.
The bench uses copied worktree sources, native documents, and native exported
fixtures. Only external printer/realtime seams are intercepted in tests.

## RED and GREEN

Initial tests-only RED `37a357748a8838004b5191c4b5612544fd8c1c11`, on unchanged
O1 production:

```text
bench --site ury.localhost run-tests --app ury --module ury.tests.test_offline_replay
Ran 17 tests in 59.252s
FAILED (failures=17)
AssertionError: Lists differ: [1.0] != [2, 1]
AssertionError: ValidationError not raised
```

Failures include subtests; no import or setup errors. Missing append,
capture/refusal, and offline-KOT behaviour. Two baseline protections pass.

Production-only GREEN `4c9687b9dd25c969de2d08002454cdfed223f0b8`:

```text
Ran 17 tests in 53.730s
OK
```

Independent review regression, tests-only RED
`847c97464b480313ae0901b53eb187180a955fcd`:

```text
Ran 18 tests
FAILED (failures=1)
AssertionError: 'Draft' != 'Failure'
```

The occupied gate for a non-billing append without snapshot fields was
restored in production-only GREEN `9d29e6c9678cf129ec5f60094fd2c5471035c452`.
That candidate, including one passing defaults protection, reports:

```text
Ran 19 tests in 78.828s
OK
```

The defaults protection was strengthened to make the profile disagree with
the saved order: tests-only RED `0934894f`. The saved invoice header and first
child are explicitly set to a real leaf cost centre before the profile changes:

```text
bench --site ury.localhost run-tests --app ury --module ury.tests.test_offline_replay --test test_expected_total_validation_does_not_reset_saved_invoice_defaults
Ran 1 test in 8.175s
FAILED (failures=1)
AssertionError: 'Offline Other ed6962ce-4235-4697-8498-b2e2a0c56b45 - URY' != 'Main - URY'
```

Native ERPNext v16's `set_missing_values(for_validate=True)` preserves saved
header and nonempty child accounting defaults while preparing new lines.
Production-only GREEN `60ddd45eef9ee44a90160d7e3693184264c44af1`:

```text
Final focused module (including the strengthened regression):
Ran 19 tests in 77.979s
OK

Full-app run on the same production fix:
Ran 31 tests in 126.245s
OK
Ran 329 existing tests in 2.464s
FAILED (failures=1, errors=3)
frappe.exceptions.DoesNotExistError: DocType Payment Gateway not found
```

The full-app run preceded the test's final explicit nonempty cost-centre
strengthening. The final 19-test focused run exercises that strengthened
test on the identical production fix. The 31-test group includes all
12 O1 tests and 19 O2 tests. There are no skipped tests in the focused groups.

The final screenshot readback is one invoice and one KOT for a double send:
`OFF-539ab117-00001`, `KOT-539ab117-00001`, Served, `ury_offline_printed=1`.
Both PNGs were captured from the running native Desk as a demo cashier using
fresh headless Chrome profiles, which were removed after each capture.

## Exact-pin baseline and limits

```text
bench --site ury.localhost run-tests --app ury
pin 1f652d526d398993a11f740e008e15bbab1deec4:
Ran 329 tests in 2.974s
FAILED (failures=1, errors=5)
frappe.exceptions.DoesNotExistError: DocType Payment Gateway not found
```

The full legacy run is incomplete, not green. No skips, changed tests, or
weakened gates were used to hide it. Error-count differences across runs
are not claimed as fixes to unrelated legacy code.

Existing native ERPNext validation refuses a POS opening whose start date
is before today's date even if its status remains Open. Same-day captures
are tested; no overnight-shift override is implemented or claimed.

## Billing-user hazard: proposed upstream issue note only

The coordinator may use this observation in an upstream issue. No upstream
issue or PR was posted by this implementation, and no failing hazard test
is committed. The established billing-user NEW-order replacement exemption
is intentionally unchanged, per the user's decision.

```text
Ran 1 test in 5.859s
FAILED (failures=1)
AssertionError: 1.0 != 2 :
NEW billing order silently replaced occupied order OFF-fca76d5b-00001
```

Offline replay uses `append=1`, so it adds round lines rather than using
that existing replacement path.
