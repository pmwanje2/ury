# S6 native bench evidence

Disposable Docker project `ury-fixes-o2s6-mijnpjnz`; only published address
`127.0.0.1:18723`. Frappe 16.52.0, ERPNext 16.50.0, Python 3.14.8,
Node 24.21.0, MariaDB 11.8, Redis 6.2.

## RED → GREEN

Tests-only RED `7fa8c5b598ea6bc83e5c3253e75ab2806e21073d`:

```text
bench --site ury.localhost run-tests --app ury --module ury.tests.test_branch_resolution
Running 7 integration tests for ury
Ran 7 tests in 2.780s
FAILED (failures=7)
AssertionError: 'Branch A 29de5546' != 'Branch B 29de5546'
AssertionError: ValidationError not raised
AssertionError: Lists differ: two assigned rooms != one selected default branch room
```

There were no setup/import errors. The failures were the unordered first-row
selection and missing ambiguity refusal. Existing single-branch/room behavior
protections passed.

Production-only GREEN `ada89def2ce5a04b96eb3058c873f00f764ad518`:

```text
bench --site ury.localhost run-tests --app ury --module ury.tests.test_branch_resolution
Running 7 integration tests for ury
Ran 7 tests in 2.753s
OK
```

The shared `_user_branch_assignments()` helper returns the sole assigned
branch, otherwise filters to the current user's native default Branch User
Permission, or throws `URY-BRANCH-AMBIGUOUS`. `getBranchRoom` and `getRoom`
use that same resolved assignment.

## Baseline and regression

Same current target (`d7a5b9d880a2251bd84cc9d14324bfaad73797dd`) full-app
baseline with the S6 test module removed:

```text
bench --site ury.localhost run-tests --app ury
Ran 334 tests in 2.566s
FAILED (failures=1, errors=5)
Then: DocType Payment Gateway not found
```

S6 candidate full-app run:

```text
Focused S6: 7/7 OK
Existing application selection: Ran 334 tests in 2.227s
FAILED (failures=1, errors=3)
Then: DocType Payment Gateway not found
```

The existing baseline failures are unrelated legacy CSRF/customer/branding
tests and missing Payment Gateway preparation. This is not a full-suite green
claim; no skips or weakened checks were used.

## Running POS evidence

The user `offline-7b76b0d6@example.com` has two assigned branches. With the
native default Branch permission set to `Offline Branch 7b76b0d6`, the actual
POS opening screen showed that branch. A fresh headless Chrome profile was
removed after capture.

With the default removed, all three server APIs refused with the stable ID:

```text
getBranch: URY-BRANCH-AMBIGUOUS: Set a default Branch User Permission for an assigned branch.
getBranchRoom: URY-BRANCH-AMBIGUOUS: Set a default Branch User Permission for an assigned branch.
getRoom: URY-BRANCH-AMBIGUOUS: Set a default Branch User Permission for an assigned branch.
```

The POS UI displayed its existing Access Denied screen. The native Desk
request from the same user displayed the stable refusal in a message dialog.

