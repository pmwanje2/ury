# RED recheck invocation attribution

The revision was emitted to the parent terminal immediately before the test
pipeline, **outside** `tee`; it is therefore not present in raw `red-recheck.log`.
This note transcribes that command/output association; it is not new test output
or an independent attestation.

```sh
printf 'RED_SHA='
git -C red-recheck rev-parse HEAD
docker compose -p ury-p2-hktjtxtp -f compose.yaml exec -T bench \
  env/bin/python /proof/run_tests.py ury.tests.test_guest_phone_payment \
  2>&1 | tee red-bdf6afc.log
```

Parent-terminal output before the pipeline:

```text
RED_SHA=bdf6afc2fe345d77b18d68def4630c7e704dd94c
```

`red-recheck` was the detached clone mounted read-only at
`/home/frappe/frappe-bench/apps/ury`. The original `red-bdf6afc.log` was copied
unchanged to `red-recheck.log`; SHA-256 of both:
`e0bdb126fca1be8cf489df3b62a257b0855b8af36cb6829d13d091fb2d16846e`.
After that run, the scratch clone was checked out at `a2547fe` for GREEN.
Independent re-execution by the reviewer remains CANNOT-VERIFY.
