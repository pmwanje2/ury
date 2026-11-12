import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const script = readFileSync(new URL('./ProductionDashboard.vue', import.meta.url), 'utf8').split('<script>')[1].split('</script>')[0];

test('overview renders board-window counts, not five old verified cancellation tickets', async () => {
  const db = {
    getDocList: async () => [{ name: 'Kitchen', disable: 0 }],
    // Reproduces Tankhill: five confirmed cancellations retained their active status.
    getCount: async () => 5,
  };
  const component = new Function('FrappeApp', 'ProductionCard', 'window',
    script.replace(/^import .*;\s*$/gm, '').replace('export default', 'return'))(
    class { db() { return db; } call() { return { post: async (method, args) => {
      assert.equal(method, 'ury.ury.api.ury_kot_display.production_unit_counts');
      assert.deepEqual(JSON.parse(args.units), ['Kitchen']);
      return { message: { Kitchen: { active_orders: 0, served_orders: 0, total_orders: 0 } } };
    } }; } }, {}, { location: { origin: 'http://synthetic.test' } },
  );
  const vm = { ...component.data(), ...component.methods };
  await vm.loadDashboard();
  assert.equal(vm.dashboard[0].active_orders, 0);
  assert.equal(vm.dashboard[0].total_orders, 0);
});
