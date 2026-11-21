const assert = require('node:assert/strict');
const { existsSync, readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const test = require('node:test');
const yaml = require('js-yaml');

const file = resolve(__dirname, '../.github/workflows/pos-tests.yml');

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
    'PYTHONPATH=.. python3 -m unittest ury.tests.unit.test_offline_capture_fixture ury.ury_pos.test_floor_pin',
  ]);
});
