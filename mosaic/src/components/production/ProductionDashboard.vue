<template>
  <div class="p-6">

    <!-- Loading -->
    <div v-if="loading" class="text-center py-10">
      <h2 class="text-lg font-semibold text-gray-500">
        Loading Production Dashboard...
      </h2>
    </div>

    <!-- Production Cards -->
    <div
      v-else
      class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6"
    >
      <ProductionCard
        v-for="unit in dashboard"
        :key="unit.name"
        :title="unit.name"
        :activeOrders="unit.active_orders"
        :servedOrders="unit.served_orders"
        :totalOrders="unit.total_orders"
        :disabled="!!unit.disable"
        @open="!unit.disable && openProduction(unit.name)"
      />
    </div>

  </div>
</template>

<script>
import ProductionCard from "./ProductionCard.vue";
import { FrappeApp } from "frappe-js-sdk";

const frappe = new FrappeApp(window.location.origin);

export default {
  name: "ProductionDashboard",

  components: {
    ProductionCard,
  },

  data() {
    return {
      loading: true,
      dashboard: [],
      db: frappe.db(),
    };
  },

  mounted() {
    this.loadDashboard();
  },

  methods: {
    async loadDashboard() {
      try {
        const result = await this.db.getDocList("URY Production Unit", {
          fields: ["name", "disable"],
          orderBy: {
            field: "name",
            order: "asc",
          },
        });

        const units = result || [];

        // Same time window/filters as the per-unit KOT view (server-side).
        const counts = await frappe.call().post("ury.ury.api.ury_kot_display.production_unit_counts", {
          units: JSON.stringify(units.map((u) => u.name)),
        });
        const byUnit = counts.message || {};
        for (const unit of units) {
          const c = byUnit[unit.name] || {};
          unit.active_orders = c.active_orders || 0;
          unit.served_orders = c.served_orders || 0;
          unit.total_orders = c.total_orders || 0;
        }

        console.log("Dashboard:", units);
        this.dashboard = units;
      } catch (error) {
        console.error(error);
      } finally {
        this.loading = false;
      }
    },

    openProduction(productionName) {
      this.$router.push(`/${productionName}`);
    },
  },
};
</script>
