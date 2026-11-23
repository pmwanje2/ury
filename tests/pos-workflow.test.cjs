const assert = require('node:assert/strict');
const { existsSync, readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const test = require('node:test');
const yaml = require('js-yaml');

const file = resolve(__dirname, '../.github/workflows/pos-tests.yml');
const PRINT_JINJA = 'ury/ury/print_format/print-jinja-requirements.txt';

test('integration PRs and pushes run the locked, site-less POS suite', () => {
  assert.ok(existsSync(file), 'missing .github/workflows/pos-tests.yml');
  const workflow = yaml.load(readFileSync(file, 'utf8'));
  for (const event of ['pull_request', 'push']) {
    assert.deepEqual(workflow.on[event], { branches: ['maximus/v16-integration'] });
  }
  const job = workflow.jobs.pos;
  assert.equal(job.services, undefined);
  assert.equal(job['continue-on-error'], undefined);
  assert.equal(job.defaults.run['working-directory'], 'pos');
  const setup = job.steps.find(step => step.uses?.startsWith('actions/setup-node@'));
  assert.equal(setup.with['node-version'], '24.20.0');
  const commands = job.steps.filter(step => step.run);
  assert.ok(commands.every(step => !step['continue-on-error'] && !step.if && !step['working-directory']));
  assert.deepEqual(commands.map(step => step.run), [
    'npm --prefix .. ci --ignore-scripts --no-audit --no-fund',
    'node --test ../tests/pos-workflow.test.cjs',
    'npm test --offline -- ./tests/ --maxWorkers=1',
    // MenuItemCard and the other shared components are also rendered by serve.
    'npm --prefix ../packages/ui test --offline -- --maxWorkers=1',
    'npm --prefix ../serve test --offline -- --maxWorkers=1',
    // The print-format tests render with the Jinja2 and MarkupSafe that Frappe v16 pins.
    `python3 -m pip install --user --break-system-packages --no-deps --only-binary=:all: --require-hashes -r ../${PRINT_JINJA}`,
    [
      'ury.tests.unit.test_offline_capture_fixture',
      'ury.ury.api.test_ury_kot_notes',
      'ury.ury.api.test_ury_hold_fire',
      'ury.ury.api.test_ury_table_bookings',
      'ury.ury.doctype.ury_order.test_split_bill_release',
      'ury.ury.doctype.ury_order.test_cancel_order_reason',
      'ury.ury_pos.test_floor_pin',
      'ury.ury.doctype.ury_order.test_price_determination',
      'ury.ury.doctype.ury_menu.test_menu_price_upkeep',
      'ury.ury.api.test_shown_price_is_billed',
      'ury.ury.api.test_kds_display_window',
      'ury.tests.unit.test_backfill_invoice_employee_attribution',
      'ury.ury.api.test_availability_tiers',
      'ury.ury.api.test_menu_availability_operations',
      'ury.ury_pos.test_pos_invoice_list_fields',
      'ury.ury.print_format.test_print_format_waiter_name',
    ].map(module => `PYTHONPATH=.. python3 -m unittest ${module}`).join('\n') + '\n',
  ]);
});

test('the print Jinja is the hash-pinned version Frappe v16 pins', () => {
  const requirements = resolve(__dirname, '..', PRINT_JINJA);
  assert.ok(existsSync(requirements), `missing ${PRINT_JINJA}`);
  const pins = readFileSync(requirements, 'utf8').replace(/\\\n/g, ' ').split('\n')
    .map(line => line.trim()).filter(line => line && !line.startsWith('#'));
  assert.deepEqual(pins.map(line => line.split(/\s+/)[0]), ['jinja2==3.1.6', 'markupsafe==3.0.3']);
  for (const line of pins) assert.match(line, /--hash=sha256:[0-9a-f]{64}/, line);
});
