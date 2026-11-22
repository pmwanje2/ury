<template>
  <section v-if="branch && permissions.canMark" class="mb-4">
    <button type="button" data-testid="sold-out-toggle" :aria-expanded="open" @click="open = !open"
      class="min-h-11 rounded border px-4 font-semibold" style="border-color: var(--ac); color: var(--ac)">
      {{ t("Sold out") }}
    </button>
    <div v-if="open" class="mt-3 max-h-[60vh] overflow-auto rounded border p-3" style="background: var(--panel); border-color: var(--hair)">
      <p v-if="loading" role="status">{{ t("Loading…") }}</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="!loading && !error && !items.length">{{ t("No menu items.") }}</p>
      <div v-for="row in items" :key="row.item" class="flex flex-wrap items-center justify-between gap-3 border-b p-2">
        <div>
          <p class="font-semibold">{{ row.name }}</p>
          <p v-if="soldOut(row) || available[row.item]?.reason_code === 'COUNT_EXHAUSTED'">{{ t("Sold out") }}</p>
          <p v-else-if="available[row.item]?.available_qty != null">{{ t("{0} left", available[row.item].available_qty) }}</p>
        </div>
        <button v-if="!soldOut(row)" type="button" data-testid="mark-sold-out" :disabled="pending"
          class="min-h-11 rounded border px-3" @click="change(row.item, { status: 'Sold Out' })">{{ t("Mark sold out") }}</button>
        <button v-else-if="permissions.canRestore" type="button" :disabled="pending" class="min-h-11 rounded border px-3"
          @click="change(row.item, { status: 'Available' })">{{ t("Available again") }}</button>
        <form v-if="permissions.canRestore" class="flex flex-wrap items-center gap-2" @submit.prevent="saveCount(row.item)">
          <label>
            {{ t("Daily count") }}
            <input type="number" min="0" max="2147483647" step="1" required v-model="counts[row.item]" class="min-h-11 w-24 rounded border px-2">
          </label>
          <label><input type="checkbox" v-model="resets[row.item]"> {{ t("Reset at first opening") }}</label>
          <button type="submit" :disabled="pending" class="min-h-11 rounded border px-3">{{ t("Set count") }}</button>
        </form>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { t } from "../i18n";
import { availabilityPermissions } from "../../../packages/core/src/availability.ts";

const props = defineProps({ branch: String, roles: Array, call: Object, socket: Object });
const prefix = "ury.ury.api.ury_availability";
const permissions = computed(() => availabilityPermissions(props.roles));
const open = ref(false), loading = ref(false), pending = ref(false), error = ref("");
const items = ref([]), available = ref({}), counts = ref({}), resets = ref({});
const soldOut = ({ item }) => available.value[item]?.manual_status === "Sold Out" || available.value[item]?.reason_code === "SOLD_OUT_MANUAL";
let revision = 0;

async function load() {
  const branch = props.branch;
  const token = ++revision;
  loading.value = true;
  error.value = "";
  try {
    const { message } = await props.call.get(`${prefix}.get_branch_availability_menu`, { branch });
    const batches = [];
    const codes = message.items.map(row => row.item);
    for (let i = 0; i < codes.length; i += 500) {
      batches.push(props.call.post(`${prefix}.get_menu_availability`, {
        branch, company: message.company, item_codes: codes.slice(i, i + 500),
      }));
    }
    const snapshots = await Promise.all(batches);
    if (token !== revision) return;
    items.value = message.items;
    available.value = Object.assign({}, ...snapshots.map(snapshot => snapshot.message.items));
    for (const { item } of items.value) {
      counts.value[item] ??= available.value[item]?.daily_limit ?? "";
      resets.value[item] ??= available.value[item]?.auto_reset_on_open ?? true;
    }
  } catch {
    if (token === revision) error.value = t("Could not load availability. Try again.");
  } finally {
    if (token === revision) loading.value = false;
  }
}

async function change(item_code, update) {
  if (pending.value) return;
  const branch = props.branch;
  pending.value = true;
  error.value = "";
  try {
    const result = await props.call.post(`${prefix}.set_item_availability`, { branch, item_code, ...update });
    if (props.branch === branch && open.value) {
      if (result.message) available.value = { ...available.value, [item_code]: result.message };
      await load();
    }
  } catch {
    if (props.branch === branch && open.value) error.value = t("Could not update availability. Try again.");
  } finally {
    pending.value = false;
  }
}

function saveCount(item) {
  const count = Number(counts.value[item]);
  if (counts.value[item] !== "" && Number.isInteger(count) && count >= 0) {
    void change(item, { count, reset_policy: resets.value[item] ? "Start of business day" : "Never" });
  }
}

watch(() => [open.value, props.branch, permissions.value.canMark, props.socket], ([visible, branch, canMark, socket], _, cleanup) => {
  if (!visible || !branch || !canMark) return;
  items.value = [];
  available.value = {};
  counts.value = {};
  resets.value = {};
  const channel = `menu_availability_update_${branch}`;
  let timer;
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(load, 500);
  };
  socket?.on(channel, refresh);
  socket?.on("connect", refresh);
  void load();
  cleanup(() => {
    ++revision;
    clearTimeout(timer);
    socket?.off(channel, refresh);
    socket?.off("connect", refresh);
  });
});
</script>
