<template>
  <div >
    <nav
      class="fixed left-0 top-0 z-20 w-full border-b border-gray-200 bg-white dark:border-gray-600 dark:bg-gray-900"
    >
      <div
        class="mx-auto flex max-w-screen-2xl items-center justify-between p-4"
      >
        <!-- Logo/Title Section -->
        <div class="flex items-center">
          <template
            v-if="
              this.tabClick.currentTab === '/Table' ||
              this.auth.cashier ||
              this.tabClick.isLoginPage
            "
          >
            <a href="/urypos/Table" class="flex-shrink-0">
              <img :src="brand.logo" :alt="brand.name" class="w-32 lg:w-44" />
            </a>
          </template>
          <template v-else>
            <h3
              class="mb-2 mt-2 p-1 text-2xl font-medium text-gray-900 dark:text-white lg:text-3xl"
            >
              {{ this.table.selectedTable }}
            </h3>
          </template>
        </div>

        <!-- User Menu Section -->
        <div
          v-if="!this.tabClick.isLoginPage"
          class="relative ml-4 flex flex-shrink-0 items-center gap-3"
        >
          <button
            type="button"
            :disabled="this.auth.loggingOut"
            class="min-h-[44px] rounded-md border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
            @click="this.auth.switchWaiter"
          >
            Switch waiter
          </button>
          <button
            type="button"
            class="flex items-center rounded-full bg-gray-100 p-1 text-sm hover:bg-gray-200 focus:outline-none focus:ring-2 focus:ring-gray-300 dark:bg-gray-700 dark:hover:bg-gray-600"
            id="user-menu-button"
            aria-expanded="false"
            @click="this.auth.toggleDropdown()"
            ref="dropdownButton"
          >
            <div
              class="relative inline-flex h-8 w-8 items-center justify-center overflow-hidden rounded-full bg-gray-200 dark:bg-gray-600 sm:h-8 sm:w-8"
            >
              <span
                class="text-sm font-medium text-gray-700 dark:text-gray-300"
              >
                {{ this.auth.sessionUser.charAt(0).toUpperCase() }}
              </span>
            </div>
          </button>

          <!-- New Dropdown Menu Style -->
          <div
            v-show="this.auth.activeDropdown"
            class="absolute right-0 mt-2 w-56 origin-top-right rounded-md bg-white shadow-lg ring-1 ring-black ring-opacity-5 focus:outline-none"
          >
            <div class="py-1">
              <a
                href="#"
                class="block px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
              >
                {{ this.auth.getLoginAvatar() }}
              </a>
              <a
                href="#"
                class="block px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
                @click="reload"
              >
                Reload
              </a>

              <a
                href="#"
                class="block px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
                @click="this.auth.routeToHome()"
                >Switch To Desk
              </a>
              <a
                v-if="!this.auth.pinSession"
                href="/update-password"
                class="block px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
              >
                Change my password
              </a>
              <template v-if="canManagePins">
                <button type="button" class="block min-h-[44px] w-full px-4 py-2 text-left text-sm text-gray-700 hover:bg-gray-100" @click="pairing = true">Pair this tablet</button>
                <button type="button" class="block min-h-[44px] w-full px-4 py-2 text-left text-sm text-gray-700 hover:bg-gray-100" @click="staffPins = true">Staff PINs</button>
              </template>
              <div class="border-t border-gray-200"></div>
              <button
                type="button"
                :disabled="this.auth.loggingOut"
                class="block w-full px-4 py-2 text-left text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-50"
                @click="this.auth.logOut"
              >
                Log out
              </button>
            </div>
          </div>
        </div>
      </div>
    </nav>

    <!-- Spacer for Fixed Header -->
    <div class="h-16 sm:h-20"></div>
    <StaffPins v-if="staffPins && canManagePins" @close="staffPins = false" />
    <div v-if="pairing && canManagePins" class="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="Pair this tablet">
      <form class="w-full max-w-md space-y-4 rounded-lg bg-white p-6 shadow-xl" @submit.prevent="pairTablet">
        <h2 class="text-2xl font-semibold">Pair this tablet</h2>
        <label for="tablet_label" class="block text-sm text-gray-700">Tablet label</label>
        <input id="tablet_label" v-model="tabletLabel" name="tablet_label" required maxlength="80" class="w-full rounded-md border p-3" placeholder="Floor tablet 1" />
        <p v-if="pairError" role="alert" class="text-sm text-red-700">{{ pairError }}</p>
        <button type="submit" :disabled="pairBusy" class="min-h-[44px] w-full rounded-md bg-blue-600 p-3 text-white">Pair tablet</button>
        <button type="button" class="min-h-[44px] w-full p-3" @click="pairing = false">Cancel</button>
      </form>
    </div>
  </div>
</template>

<script>
import { useAuthStore } from "@/stores/Auth.js";
import { posOpening } from "@/stores/posOpening.js";
import { posClosing } from "@/stores/posClosing.js";
import uriPosImage from "@/assets/logos/URY_POS.jpg";
import { resolveBrand } from "../../../packages/core/src/brand.ts";
import { tabFunctions } from "@/stores/bottomTabs.js";
import { useTableStore } from "@/stores/Table.js";
import { useInvoiceDataStore } from "@/stores/invoiceData.js";
import StaffPins from './StaffPins.vue';

export default {
  name: "Header",
  components: { StaffPins },
  setup() {
    const auth = useAuthStore();
    const posOpen = posOpening();
    const posClose = posClosing();
    const tabClick = tabFunctions();
    const table = useTableStore();

    return { auth, posOpen, posClose, tabClick, table, invoice: useInvoiceDataStore() };
  },
  data() {
    return {
      brand: resolveBrand({ name: "URY POS logo", logo: uriPosImage }),
      canManagePins: false, pairing: false, staffPins: false, tabletLabel: '', pairError: '', pairBusy: false,
    };
  },
  watch: {
    'invoice.posProfile': { immediate: true, handler: 'checkPinPermission' },
  },
  methods: {
    async checkPinPermission() {
      this.canManagePins = false;
      if (!this.invoice.posProfile) return;
      try {
        const { message } = await this.auth.call.get('frappe.client.has_permission', {
          doctype: 'POS Profile', docname: this.invoice.posProfile, perm_type: 'write',
        });
        this.canManagePins = message.has_permission === true;
      } catch (_error) { /* Native permission lookup fails closed. */ }
    },
    async pairTablet() {
      this.pairBusy = true; this.pairError = '';
      try {
        await this.auth.call.post('ury.ury_pos.floor_pin.pair_device', { pos_profile: this.invoice.posProfile, label: this.tabletLabel });
        this.pairing = false;
      } catch (_error) { this.pairError = 'This tablet could not be paired. Check PIN sign-in is enabled.'; }
      finally { this.pairBusy = false; }
    },
    reload() {
      window.location.reload();
    },
  },
};
</script>
