<template>
  <div class="kot-board mx-auto p-6 mb-16 relative">
    <div v-if="socketStale" role="status" class="fixed bottom-0 inset-x-0 p-3 bg-red-600 text-white text-center z-50">
      Realtime offline or resyncing — board may be stale.
    </div>
    <!-- Alert Modal div start-->
    <div
      v-if="this.showModal"
      class="fixed inset-0 z-10 overflow-y-auto bg-gray-100"
    >
      <div class="flex items-center justify-center">
        <div class="w-full rounded-lg bg-white p-6 shadow-lg md:max-w-md">
          <p
            class="block text-left text-xl font-medium text-gray dark:text-gray"
          >
            <span
              class="w-3 h-3 rounded-full inline-block mr-1 bg-red-500"
            ></span>
            Not Permitted
          </p>
          <hr class="border-gray-200" />

          <p class="text-left text-xl mt-6 font-medium text-gray-500">
            Log in to access this page.
          </p>

          <div class="flex justify">
            <button
              @click="
                this.showModal = false;
                this.redirectToLogin();
              "
              class="mt-8 rounded bg-blue-500 px-3 py-2 text-white hover:bg-blue-600"
            >
              Login
            </button>
          </div>
        </div>
      </div>
    </div>
    <!-- Alert Modal div end-->

    <div v-if="actionError" role="alert" class="mb-4 rounded-lg bg-red-50 border border-red-500 p-4 text-red-700">
      {{ actionError }}
    </div>

    <div v-if="failedPrints.length" role="status" class="mb-4 rounded-lg bg-red-50 border border-red-600 p-4 text-red-700 flex justify-between items-center gap-4">
      <span>{{ failedPrints.length }} tickets not printed — Retry</span>
      <button @click="retryFailedPrints" class="rounded bg-red-700 px-4 py-2 text-white font-semibold">Retry</button>
    </div>
    <p v-if="printError" role="alert" class="mb-4 rounded border border-red-600 bg-red-50 p-3 text-red-700">{{ printError }}</p>
    <button v-if="branch" @click="openPrintSettings" class="mb-4 rounded border border-blue-600 px-4 py-2 text-blue-700 font-semibold">Manager settings</button>

    <div v-if="showPrintSettings && canManagePrintSettings" class="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4 overflow-y-auto">
      <form @submit.prevent="savePrintSettings" role="dialog" aria-modal="true" aria-labelledby="station-print-title" class="w-full max-w-lg rounded-lg bg-white p-6 shadow-xl">
        <h2 id="station-print-title" class="text-xl font-bold">Manager station printing settings</h2>
        <p class="mt-2 text-sm text-gray-600">{{ production }} · This browser only. Use one station per unit. New tickets print from the time this is enabled.</p>
        <p v-if="stationSettingsError" role="alert" class="mt-3 text-red-700">{{ stationSettingsError }}</p>
        <label class="mt-4 block font-semibold">Printer host
          <input v-model="stationSettings.host" class="mt-1 w-full rounded border p-2" type="text" autocomplete="off" required>
        </label>
        <label class="mt-3 block font-semibold">Printer port
          <input v-model.number="stationSettings.port" class="mt-1 w-full rounded border p-2" type="number" min="1" max="65535" required>
        </label>
        <label class="mt-3 block font-semibold">Print format
          <input v-model="stationSettings.printFormat" class="mt-1 w-full rounded border p-2" type="text" required>
        </label>
        <label class="mt-4 flex items-center gap-2 font-semibold">
          <input v-model="stationSettings.enabled" type="checkbox"> Enable station printing
        </label>
        <div class="mt-6 flex justify-end gap-3">
          <button type="button" @click="showPrintSettings = false" class="rounded border px-4 py-2">Cancel</button>
          <button type="submit" class="rounded bg-blue-600 px-4 py-2 text-white font-semibold">Save</button>
        </div>
      </form>
    </div>

    <div v-if="branch" class="mb-4">
      <button @click="fetchRecentServed" :disabled="loadingServed" class="rounded bg-blue-600 px-3 py-2 text-white disabled:opacity-50">
        {{ loadingServed ? "Loading…" : "Recently served · Recall" }}
      </button>
      <div v-if="showRecentServed" class="mt-3 rounded-lg border p-3">
        <p class="mb-2 text-gray-500">Recall is available for 15 minutes after serving.</p>
        <p v-if="!recentServed.length && !loadingServed" class="text-gray-500">No recently served tickets.</p>
        <div v-for="served in recentServed" :key="served.name" class="flex justify-between items-center py-2">
          <span>{{ served.table_takeaway || !served.restaurant_table ? "Takeaway" : served.restaurant_table }} · {{ daily_order_number ? served.order_no : served.invoice.slice(-4) }}</span>
          <button @click="recallOrder(served)" :disabled="recalling[served.name]" class="rounded bg-blue-600 px-3 py-2 text-white disabled:opacity-50">
            {{ recalling[served.name] ? "Recalling…" : "Recall" }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="kot.filter(k => k.production === production).length === 0 && !loadingKots" class="text-center py-10 text-gray-500 text-xl">
      No active orders for {{ production }}
    </div>

    <div
      class="grid kot-grid"
    >
      <template v-for="kot in this.kot" :key="kot.name">
        <div v-if="!kot.showDiv && kot.production === production" class="kot-ticket masonry-item">
        <div
          :class="[kot.color, { 'ring-4 ring-red-600': kot.late }]"
          class="kot-card shadow-lg rounded-2xl"
        >
          <div class="check">
              <!-- Card Header: Table Name and Order Number -->
              <div class="kot-header flex justify-between" @click="rotateCard(kot)">
                <div class="kot-identity text-sm">
                  <span
                    v-if="kot.tableortakeaway !== 'Takeaway'"
                    class="text-sm font-medium text-[#6B7280] mr-1"
                    >Table
                  </span>
                  <span class="kot-table text-black-500 font-semibold">
                    {{ kot.tableortakeaway }}
                    <span class="kot-user text-sm font-medium text-[#6B7280]"
                      >( {{ kot.user }} )</span
                    ></span
                  >
                  <span v-if="kot.is_aggregator" class="text-sm font-medium text-[#6B7280]">Aggregator</span>
                  <span v-if="kot.is_aggregator" class="text-black-500 ml-2 font-semibold"
                    >{{ kot.customer_name }}
                  </span><br v-if="kot.is_aggregator" />
                  <span v-if="kot.is_aggregator" class="text-sm font-medium text-[#6B7280]">Aggregator ID</span>
                  <span v-if="kot.is_aggregator" class="text-black-500 ml-2 font-semibold"
                    >{{ kot.aggregator_id }}
                  </span><br v-if="kot.is_aggregator"/>
                  <span class="text-sm font-medium text-[#6B7280]">Order</span>
                  <span class="text-black-500 ml-2 font-semibold"
                    >{{ this.daily_order_number ? kot.order_no : kot.invoice.slice(-4) }}
                    
                  </span>
                  <span
                    class="text-black-500 ml-2 font-semibold"
                    v-if="
                      kot.type === 'Partially cancelled' ||
                      kot.type === 'Cancelled'
                    "
                  >
                    ( {{ kot.type }} )</span
                  >
                </div>
                <div
                  :class="kot.timecolor"
                  class="kot-age font-inter font-semibold leading-10"
                >
                  {{ Number((kot.timeRemaining || '0:0').split(':')[0]) ? Number((kot.timeRemaining || '0:0').split(':')[0]) + ' h ' : '' }}{{ String(Number((kot.timeRemaining || '0:0').split(':')[1])).padStart(Number((kot.timeRemaining || '0:0').split(':')[0]) ? 2 : 1, '0') }} min
                  <span v-if="kot.late" class="block text-sm text-red-700 leading-4">LATE</span>
                </div>
              </div>
              <p v-if="kot.type === 'Order Modified'" class="font-semibold mt-2">Order Modified</p>
              <div
                v-if="kot.type === 'Duplicate'"
                class="text-[#DC0000] font-medium"
              >
                ( Duplicate KOT ( CHECK WITH CAPTAIN ) )
              </div>
              <div v-show="kot.comments" class="kot-note">
                <span class="kot-note-label">{{ kot.type === 'Cancelled' || kot.type === 'Partially cancelled' ? 'Cancellation remarks' : 'Special request' }}</span>
                ( {{ kot.comments }} )
              </div>
              <div></div>
              <div>
                <div
                  class="font-semibold justify-between items-center"
                  v-for="kotitem in sortedKotItems(kot)"
                  :key="kotitem.name"
                >
                  <div
                    @click="
                      () => {
                        toggleItemStrikeThrough(kotitem, kot);
                      }
                    "
                    :class="{
                      'line-through text-green-700': kotitem.striked,
                    }"
                    class="kot-item flex font-semibold items-start"
                  >
                    <div class="kot-quantity">
                      <span>{{ kotitem.qty }}</span>
                    </div>
                    <div class="kot-dish-detail">
                      <span class="kot-dish text-black-100">{{
                        kotitem.item_name
                      }}<span v-show="kotitem.indicate_course" class="text-sm text-gray-500 ml-1"> ( {{kotitem.course}} )</span>
                      </span
                      ><br />
                      <span
                        class="ml-2 text-black-100"
                        v-if="
                          kot.type === 'Partially cancelled' ||
                          kot.type === 'Cancelled'
                        "
                        >[Old Qty = {{ kotitem.quantity }}]</span
                      >
                    </div>
                  </div>
                  <div>
                    <p
                      v-show="kotitem.comments"
                      class="kot-note"
                    >
                      <span class="kot-note-label">{{ kot.type === 'Cancelled' || kot.type === 'Partially cancelled' ? 'Cancellation remarks' : 'Special request' }}</span>
                      {{ kotitem.comments }}
                    </p>
                    <hr class="my-1 border-gray-200" />
                  </div>
                </div>
              </div>
            
          </div>
          <footer @click.self="kot.isRotated && rotateCard(kot)" class="kot-actions mt-4 border-t border-gray-200 pt-4" :class="{ 'ring-2 ring-blue-600 rounded-lg': kot.isRotated }">
            <button
              v-if="!pendingServes[kot.name]"
              @click.stop="
                kot.type === 'Cancelled' || kot.type === 'Partially cancelled'
                  ? confirmOrder(kot)
                  : serveOrder(kot)
              "
              :disabled="!!pendingServes[kot.name] || !!confirming[kot.name]"
              class="w-full py-2 px-6 bg-blue-600 text-white font-semibold rounded-md hover:bg-blue-700 disabled:opacity-50"
            >
              {{ kot.type === "Cancelled" || kot.type === "Partially cancelled" ? "Confirm" : "Serve" }}
            </button>
            <button @click.stop="printKot(kot, { retry: true })" :disabled="!stationSettings.enabled || printingKots.includes(kot.name) || printedKots.includes(kot.name)" class="mt-2 w-full rounded border border-blue-600 py-2 px-6 text-blue-700 font-semibold hover:bg-blue-50 disabled:opacity-50">Print</button>
            <div v-if="pendingServes[kot.name]" class="flex justify-between items-center gap-3 text-blue-700">
              <span>{{ pendingServes[kot.name].sending ? "Serving…" : "Serving in 5 seconds…" }}</span>
              <button v-if="!pendingServes[kot.name].sending" @click.stop="undoServe(kot)" class="rounded border border-blue-600 px-3 py-1 font-semibold">Undo</button>
            </div>
          </footer>
        </div>
        </div>
      </template>
    </div>

    <!-- Audio Alert Message -->
    <div
      v-if="showAudioAlertMessage"
      class="absolute top-1 left-1/2 transform -translate-x-1/2 p-2 font-bold text-2xl text-red-500 text-center"
    >
      Audio notifications disabled. Click anywhere to enable.
    </div>

    <!-- KOT Delay Error Alert Banner -->
    <div
      v-if="showKotErrorAlert && kotErrorAlert"
      class="fixed top-0 left-0 right-0 mx-auto p-4 bg-red-50 border-b-4 border-red-500 shadow-lg z-40 flex justify-between items-center"
    >
      <div class="flex items-center gap-3">
        <div class="flex-shrink-0">
          <span class="text-3xl">⚠️</span>
        </div>
        <div class="flex-1">
          <p class="font-bold text-red-700">Order Delayed</p>
          <p class="text-red-600 text-sm mt-1">
            <span v-if="!daily_order_number">Invoice: {{ kotErrorAlert.invoice.slice(-4) }}</span>
            <span v-else>Order #: {{ kotErrorAlert.order_no }}</span>
            | Table: {{ kotErrorAlert.tableortakeaway }} | Time: {{ kotErrorAlert.timestamp }}
          </p>
        </div>
      </div>
      <button
        @click="hideKotErrorAlert"
        class="ml-4 text-red-700 hover:text-red-900 font-bold text-xl flex-shrink-0"
      >
        ✕
      </button>
    </div>

    <div
      v-if="statusMessage"
      :class="[
        'fixed',
        'bottom-10',
        'right-10',
        'p-4',
        'rounded',
        'text-white',
        {
          'bg-green-500': isOnline,
          'bg-red-500': !isOnline,
        },
      ]"
      @transitionend="handleTransitionEnd"
    >
      {{ statusMessage }}
    </div>
  </div>
</template>

<script>
import { FrappeApp } from "frappe-js-sdk";
import Masonry from "masonry-layout";
import io from "socket.io-client";
import { canRecallKot, isKotLate, kitchenErrorMessage, recentServedQuery, rememberKot } from "./kitchen-actions.js";
import { createStationPrinter } from "./station-print.js";

let host = window.location.hostname;
let port = window.location.port;
let protocol = window.location.protocol;
let url = port ? `${protocol}//${host}:${port}` : `${protocol}//${host}`;
window.globalSiteName = '';
let socket; 

async function fetchAndSetSiteName() {
    try {
        const response = await fetch('/api/method/ury.ury.api.ury_kot_display.get_site_name', {
            method: 'GET',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        const data = await response.json();
        window.globalSiteName = data.message.site_name;
        // console.log('Global Site Name:', window.globalSiteName);
    } catch (error) {
        console.error('Failed to fetch site name:', error);
    }
}

async function initializeSocket() {
    await fetchAndSetSiteName();
    if (window.globalSiteName) {
        let site = window.globalSiteName;
        let site_url = `${url}/${site}`;
        socket = io(site_url,{ withCredentials: true });
        console.log("socket == >",socket)
        socket.on('connect_error', (err) => {
            console.error("Socket connection error:", err);
        }); 
        socket.on('connect', () => {
            console.log('Socket connected:', socket.connected);
        });
    } else {
        console.error('Site name is not set. Socket cannot be initialized.');
    }
}

initializeSocket(); // Initialize the socket after fetching the site name


const frappe = new FrappeApp(url);
export default {
  // inject: ["$auth", "$socket"],
  props: ["production"],
  data() {
    return {
      kot: [],
      knownKots: new Set(),
      socketStale: true,
      socketActive: false,
      socketHandlers: {},
      resyncGeneration: 0,
      lastSocketAudioFile: null,
      masonry: null,
      call: frappe.call(),
      branch: "",
      kot_channel: "",
      kot_error_channel: "",
      clickedItems: new Set(),
      struckThroughItems: {},
      loggeduser: "",
      showModal: false,
      kot_alert_time: "",
      showAudioAlertMessage: false,
      audio_alert: 0,
      isOnline: navigator.onLine,
      statusMessage: "",
      daily_order_number:0,
      loadingKots: true,
      kotErrorAlert: null,
      showKotErrorAlert: false,
      actionError: "",
      pendingServes: {},
      confirming: {},
      recalling: {},
      recentServed: [],
      showRecentServed: false,
      loadingServed: false,
      serverTimeOffset: 0,
      timeInterval: null,
      stationPrinter: null,
      stationSettings: { enabled: false, enabledAt: null, host: "", port: 9100, printFormat: "" },
      failedPrints: [],
      printingKots: [],
      printedKots: [],
      printError: "",
      canManagePrintSettings: false,
      showPrintSettings: false,
      stationSettingsError: "",
    };
  },
  methods: {
    setupStationPrinting() {
      if (!this.stationPrinter) {
        this.stationPrinter = createStationPrinter({
          production: this.production, user: this.loggeduser, call: this.call,
          now: () => Date.now() + this.serverTimeOffset,
          onChange: state => {
            this.failedPrints = state.failed;
            this.printingKots = state.pending;
            this.printedKots = state.printed;
            if (state.error) this.printError = state.error;
          },
        });
      }
      this.stationSettings = this.stationPrinter.settings;
    },
    async printKot(kot, options = {}) {
      try {
        this.setupStationPrinting();
        await this.stationPrinter.process(kot, options);
        this.failedPrints = this.stationPrinter.failed();
      } catch (error) {
        this.printError = kitchenErrorMessage(error, "Station printing is unavailable.");
      }
    },
    printTickets(tickets) {
      tickets.forEach(kot => { this.printKot(kot); });
    },
    async retryFailedPrints() {
      if (this.stationPrinter) await this.stationPrinter.retry();
      this.failedPrints = this.stationPrinter?.failed() || [];
    },
    async openPrintSettings() {
      this.printError = "";
      try {
        const { message } = await this.call.get("frappe.client.get", { doctype: "User", name: this.loggeduser });
        const roles = (message.roles || []).map(row => row.role);
        this.canManagePrintSettings = this.loggeduser === "Administrator" || roles.some(role => ["System Manager", "URY Admin", "URY Manager"].includes(role));
        if (!this.canManagePrintSettings) throw Error("A manager must sign in to change station printing settings.");
        this.setupStationPrinting();
        this.stationSettingsError = "";
        this.showPrintSettings = true;
      } catch (error) {
        this.printError = kitchenErrorMessage(error, "Could not open station printing settings.");
      }
    },
    savePrintSettings() {
      if (!this.canManagePrintSettings) return;
      try {
        this.stationSettings = this.stationPrinter.configure(this.stationSettings);
        this.showPrintSettings = false;
      } catch (error) {
        this.stationSettingsError = error.message;
      }
    },
    playAlertSound(path) {
      var currentDomain = window.location.origin;
      var audio_path = currentDomain + path;
      const audio = new Audio(audio_path);
      audio.play();
    },
    auth() {
      return new Promise((resolve, reject) => {
        const auth = frappe.auth();
        auth
          .getLoggedInUser()
          .then((user) => {
            this.loggeduser = user;
            resolve();
          })
          .catch((error) => {
            console.error(error);
            reject(error);
          });
      });
    },
    fetchKOT(resync = false) {
      const generation = this.resyncGeneration;
      return new Promise((resolve, reject) => {
        try {
          this.call
            .get("ury.ury.api.ury_kot_display.kot_list", {})
            .then((result) => {
              if (generation !== this.resyncGeneration) { resolve(); return; }
              const missedTickets = resync && result.message.KOT.some(
                kot => kot.production === this.production && !this.knownKots.has(kot.name)
              );
              console.log(result,"..............result")
              this.branch = result.message.Branch;
              this.serverTimeOffset = new Date(result.message.server_time.replace(" ", "T")).getTime() - Date.now();
              this.kot_alert_time = result.message.kot_alert_time;
              this.audio_alert = result.message.audio_alert;
              this.daily_order_number = result.message.daily_order_number;
              this.kot_channel = `kot_update_${this.branch}_${this.production}`;
              this.kot_error_channel = `kot_error_${this.branch}_${this.production}`;
              this.kot = result.message.KOT;
              this.kot.forEach(kot => this.knownKots.add(kot.name));
              this.loadingKots = false;
              this.updateQtyColorTable();
              this.updateTimeRemaining();
              this.masonryLoading();
              this.printTickets(this.kot);
              const audioFile = result.message.audio_file || this.lastSocketAudioFile;
              if (missedTickets && this.audio_alert === 1 && audioFile) {
                this.playAlertSound(audioFile);
              }
              resolve();
            })
            .catch((error) => {
              console.error(error);
              if (generation === this.resyncGeneration) this.loadingKots = false;
              reject(error);
            });
        } catch (error) {
          if (generation === this.resyncGeneration) this.loadingKots = false;
          reject(error);
        }
      });
    },
    rotateCard(kot) {
      this.masonryLoading();
      kot.isRotated = !kot.isRotated;
    },
    async confirmOrder(kot) {
      if (this.confirming[kot.name]) return;
      this.confirming[kot.name] = true;
      this.actionError = "";
      try {
        await this.call.post("ury.ury.api.ury_kot_display.confirm_cancel_kot", {
          name: kot.name,
        });
        this.kot = this.kot.filter(card => card.name !== kot.name);
        this.removeAllItemsFromLocalStorage(kot);
      } catch (error) {
        this.actionError = kitchenErrorMessage(error, `Could not confirm ${kot.name}. Please try again.`);
      } finally {
        delete this.confirming[kot.name];
        this.masonryLoading();
      }
    },
    serveOrder(kot) {
      if (this.pendingServes[kot.name]) return;
      this.actionError = "";
      kot.isRotated = false;
      this.pendingServes[kot.name] = { timer: null, sending: false };
      const pending = this.pendingServes[kot.name];
      pending.timer = setTimeout(async () => {
        pending.sending = true;
        try {
          await this.call.post("ury.ury.api.ury_kot_display.serve_kot", { name: kot.name });
          this.kot = this.kot.filter(card => card.name !== kot.name);
          this.removeAllItemsFromLocalStorage(kot);
          if (this.showRecentServed) await this.fetchRecentServed();
        } catch (error) {
          this.actionError = kitchenErrorMessage(error, `Could not serve ${kot.name}. Please try again.`);
        } finally {
          delete this.pendingServes[kot.name];
          this.masonryLoading();
        }
      }, 5000);
      this.masonryLoading();
    },
    undoServe(kot) {
      const pending = this.pendingServes[kot.name];
      if (!pending || pending.sending) return;
      clearTimeout(pending.timer);
      delete this.pendingServes[kot.name];
      this.masonryLoading();
    },
    cancelPendingServes() {
      Object.values(this.pendingServes).forEach(pending => {
        if (!pending.sending) clearTimeout(pending.timer);
      });
    },
    async fetchRecentServed() {
      if (this.loadingServed) return;
      this.loadingServed = true;
      this.showRecentServed = true;
      this.actionError = "";
      try {
        const now = new Date(Date.now() + this.serverTimeOffset);
        const result = await this.call.get("frappe.client.get_list", recentServedQuery(this.branch, this.production, now));
        this.recentServed = result.message.filter(kot => canRecallKot(kot, now));
      } catch (error) {
        this.actionError = kitchenErrorMessage(error, "Could not load recently served tickets. Please try again.");
      } finally {
        this.loadingServed = false;
      }
    },
    async recallOrder(kot) {
      if (this.recalling[kot.name]) return;
      this.recalling[kot.name] = true;
      this.actionError = "";
      try {
        await this.call.post("ury.ury.api.ury_kot_display.recall_kot", { name: kot.name });
        this.recentServed = this.recentServed.filter(card => card.name !== kot.name);
        await this.fetchKOT();
      } catch (error) {
        this.actionError = kitchenErrorMessage(error, `Could not recall ${kot.name}. Please try again.`);
      } finally {
        delete this.recalling[kot.name];
      }
    },

    async orderDelayNotify(kot) {
      const now = new Date();
      this.currentTime = now.toLocaleTimeString();

      this.call
        .post(
          "ury.ury.api.ury_kot_notification.order_delay_notification",
          {
            id: kot.name,
          }
        )
        .then((result) => {
          // console.log("call backed ", result);
        })
        .catch((error) => console.error(error));
    },
    toggleItemStrikeThrough(kotitem, kot) {
      kotitem.striked = !kotitem.striked;
      localStorage.setItem(
        `${kot.name}_${kotitem.name}_strike`,
        JSON.stringify(kotitem.striked)
      );
    },

    updateColorandTable(kot, restaurant_table, type, table_takeaway, custom_merged_tables) {
      if (restaurant_table === undefined) {
        kot.tableortakeaway = "Takeaway";
      } else {
        if (table_takeaway == 1) {
          kot.tableortakeaway = "Takeaway";
        } else {
          let label = restaurant_table;
          if (custom_merged_tables) {
            const partners = custom_merged_tables
              .split(",")
              .map((name) => name.trim())
              .filter(Boolean);
            if (partners.length) {
              label = [restaurant_table, ...partners].join(" + ");
            }
          }
          kot.tableortakeaway = label;
        }
      }
      if (type == "Order Modified") {
        kot.color = "bg-[#FFD493] border border-[#FFC700]";
      } else if (type == "Partially cancelled" || type == "Cancelled") {
        kot.color = "bg-[#FFD2D2] border border-[#FAA7A7]";
      } else if (restaurant_table === undefined || table_takeaway == 1) {
        kot.color = "bg-blue-100 border border-blue-200";
      } else {
        kot.color = "bg-white";
      }
      console.log(type,".............type")
    },
    updateQtyColorTable() {
      this.kot.forEach((kot) => {
        console.log(kot,"kot............")
        this.updateColorandTable(
          kot,
          kot.restaurant_table,
          kot.type,
          kot.table_takeaway,
          kot.custom_merged_tables
        );

        kot.kot_items.forEach((kotitem) => {
          const savedState = localStorage.getItem(
            `${kot.name}_${kotitem.name}_strike`
          );
          if (savedState) {
            kotitem.striked = JSON.parse(savedState);
          }
          this.calculateQty(
            kotitem,
            kotitem.quantity,
            kot.type,
            kotitem.cancelled_qty
          );
        });
      });
    },
    calculateQty(kotitem, qty, type, cancelled_qty) {
      kotitem.qty = qty;
      if (type == "Partially cancelled" || type == "Cancelled") {
        kotitem.qty = qty - cancelled_qty;
      }
    },
    removeAllItemsFromLocalStorage(kot) {
      // Get all keys in local storage
      const keys = Object.keys(localStorage);
      // Remove keys that start with `${kot.name}_`
      keys.forEach((key) => {
        if (key.startsWith(`${kot.name}_`)) {
          localStorage.removeItem(key);
        }
      });
    },

    updateTimeRemaining() {
      // console.log("update time", this.kot_channel);
      this.kot.forEach((kot) => {
        kot.timeRemaining = this.calculateTimeRemaining(kot.time);

        const timeRemaining = kot.timeRemaining.split(":");
        const minutes =
          parseInt(timeRemaining[0]) * 60 + parseInt(timeRemaining[1]);

        if (
          minutes === this.kot_alert_time &&
          kot.type !== "Cancelled" &&
          kot.type !== "Partially cancelled"
        ) {
          this.orderDelayNotify(kot);
        }
        kot.late = isKotLate(minutes, this.kot_alert_time);
        if (kot.late) {
          kot.timecolor = "text-[#DC0000]";
        } else {
          kot.timecolor = "text-black";
        }
      });
    },
    calculateTimeRemaining(targetTime) {
      const currentTime = new Date(Date.now() + this.serverTimeOffset);
      const [targetHours, targetMinutes, targetSeconds] = targetTime.split(":");
      const targetDate = new Date(
        currentTime.getFullYear(),
        currentTime.getMonth(),
        currentTime.getDate(),
        targetHours,
        targetMinutes,
        targetSeconds
      );

      const timeDifference = Math.max(0, currentTime - targetDate);
      const hoursRemaining = Math.floor(timeDifference / 3600000);
      const minutesRemaining = Math.floor((timeDifference % 3600000) / 60000);

      return `${hoursRemaining} : ${minutesRemaining}`;
    },
    fetchkotwithmasonry() {
      return this.fetchKOT().then(() => {
        this.masonryLoading();
      });
    },
    redirectToLogin() {
      var currentDomain = window.location.origin;
      window.location.href =
        currentDomain + "/login?redirect-to=mosaic/" + this.production;
    },
    masonryLoading() {
      this.$nextTick(() => {
        this.masonry = new Masonry(this.$el.querySelector(".grid"), {
          itemSelector: ".masonry-item",
          gutter: 28,

          // Other Masonry options can be added here
        });
        this.masonry.layout();
      });
    },
    hideAudioAlertMessage() {
      this.showAudioAlertMessage = false;
    },
    hideKotErrorAlert() {
      this.showKotErrorAlert = false;
      this.kotErrorAlert = null;
    },
    handleOnline() {
      this.isOnline = true;
      this.setStatusMessage("You are online");
      this.hideStatusMessageAfterDelay();
      this.fetchKOT().then(() => {
        this.masonryLoading();
      });
    },
    handleOffline() {
      this.isOnline = false;
      this.setStatusMessage("You are Offline");
    },
    setStatusMessage(message) {
      this.statusMessage = message;
    },
    hideStatusMessageAfterDelay() {
      setTimeout(() => {
        this.statusMessage = "";
      }, 3000);
    },
    handleTransitionEnd() {
      if (!this.isOnline) {
        // Reset the status message after transition end
        this.setStatusMessage("");
      }
    },
  },
  mounted() {
    this.socketActive = true;
    window.addEventListener("online", this.handleOnline);
    window.addEventListener("offline", this.handleOffline);
    document.addEventListener("click", this.hideAudioAlertMessage);
    const self = this;
    window.addEventListener("resize", this.masonryLoading);
    this.masonryLoading();

    this.auth()
      .then(() => {
        self.fetchKOT().then(() => {
          if (!this.socketActive) return;
          this.socketStale = !socket.connected;
          if (this.audio_alert === 1) {
            this.showAudioAlertMessage = true;
          }
          this.socketHandlers = {
            connect: () => {
              const generation = ++this.resyncGeneration;
              this.socketStale = true;
              this.fetchKOT(true).then(() => {
                if (generation === this.resyncGeneration) this.socketStale = !socket.connected;
              }).catch(console.error);
            },
            disconnect: () => { ++this.resyncGeneration; this.socketStale = true; },
          };
          this.socketHandlers[this.kot_channel] = (doc) => {
            if (doc.audio_file) this.lastSocketAudioFile = doc.audio_file;
            if (!rememberKot(this.knownKots, doc.kot)) return;
            if (this.audio_alert === 1) {
              this.playAlertSound(doc.audio_file);
            }
            let kottime = localStorage.getItem("kot_time");
            if (doc.last_kot_time !== null) {
              if (doc.last_kot_time !== kottime) {
                this.fetchKOT().then(() => {
                  this.masonryLoading();
                });
              }
            }
            this.kot.unshift(doc.kot);
            this.masonryLoading();
            this.updateQtyColorTable();
            this.updateTimeRemaining();
            this.printKot(doc.kot);
            setTimeout(()=>{
              if (doc.kot.type === "Cancelled"){
                this.fetchKOT().then(() => {
                  this.masonryLoading();
                });
              }
            },1500)
            localStorage.setItem("kot_time", doc.kot.time);
          };

          // New socket listener for KOT error alerts (delayed orders)
          this.socketHandlers[this.kot_error_channel] = (doc) => {
            // Look up the matching KOT in the local array to get table/order info
            const matchingKot = this.kot.find(k => k.name === doc.kot);

            this.kotErrorAlert = {
              invoice: doc.invoice,
              tableortakeaway: matchingKot?.tableortakeaway || 'Table/Takeaway info unavailable',
              order_no: matchingKot?.order_no || 'N/A',
              timestamp: new Date().toLocaleTimeString()
            };
            this.showKotErrorAlert = true;
            // Auto-hide after 8 seconds
            setTimeout(() => {
              this.hideKotErrorAlert();
            }, 8000);
          };
          for (const [channel, handler] of Object.entries(this.socketHandlers)) socket.on(channel, handler);
        });
      })
      .catch((error) => {
        console.error("Authentication error:", error);
        this.showModal = true;
      });
    this.timeInterval = setInterval(this.updateTimeRemaining, 60000);
  },
  beforeUnmount() {
    this.socketActive = false;
    ++this.resyncGeneration;
    for (const [channel, handler] of Object.entries(this.socketHandlers)) socket.off(channel, handler);
    this.cancelPendingServes();
    clearInterval(this.timeInterval);
    window.removeEventListener("resize", this.masonryLoading);
    window.removeEventListener("online", this.handleOnline);
    window.removeEventListener("offline", this.handleOffline);
    document.removeEventListener("click", this.hideAudioAlertMessage);
  },
  computed: {
    sortedKotItems() {
      return (kot) => {
        return kot.kot_items.sort((a, b) => a.serve_priority - b.serve_priority);
      };
    },
  },
};
</script>
<style>
.bg-gray-100 {
  background-color: rgba(0, 0, 0, 0.2);
}
.kot-grid {
  display: block;
}
.kot-ticket {
  width: 100%;
  margin-bottom: 28px;
}
.kot-card {
  padding: 10px;
  display: flex;
  flex-direction: column;
}
.kot-header {
  gap: 8px;
  align-items: flex-start;
  flex-wrap: wrap;
}
.kot-identity {
  overflow-wrap: normal;
  flex: 1 0 min-content;
}
.kot-dish-detail {
  min-width: 0;
  overflow-wrap: anywhere;
}
.kot-table {
  display: inline;
  font-size: 24px;
  line-height: 1.3;
}
.kot-user {
  display: block;
}
.kot-age {
  flex-shrink: 0;
  font-size: 28px;
  white-space: nowrap;
}
.kot-item {
  gap: 8px;
}
.kot-quantity {
  flex-shrink: 0;
  min-width: 2ch;
  font-size: 24px;
  line-height: 1.4;
}
.kot-dish {
  font-size: 20px;
  line-height: 1.25;
}
.kot-note {
  margin-top: 4px;
  padding: 4px 6px;
  border-left: 4px solid #b45309;
  background-color: #fffbeb;
  color: #111827;
  font-weight: 700;
  font-size: 18px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.kot-note-label {
  display: inline;
  margin-right: 4px;
  font-size: 16px;
  line-height: 1.25;
}
.kot-actions {
  flex-shrink: 0;
  margin-top: 8px;
  padding-top: 8px;
  font-size: 18px;
}
.kot-board button {
  min-height: 44px;
}
@media (min-width: 768px) {
  .kot-ticket {
    width: calc((100% - 28px) / 2);
  }
}
@media (min-width: 1024px) {
  .kot-ticket {
    width: calc((100% - 56px) / 3);
  }
}
@media (min-width: 1280px) {
  .kot-ticket {
    width: calc((100% - 84px) / 4);
  }
}
</style>
