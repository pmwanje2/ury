<template>
  <div class="kot-board mx-auto p-6 mb-16 relative" style="font-family: var(--s)">
    <div v-if="socketStale" role="status" class="fixed bottom-0 inset-x-0 p-3 text-center z-50" style="background: var(--rd); color: #fff">
      {{ __("Realtime offline or resyncing — board may be stale.") }}
    </div>
    <div
      v-if="openServiceRequests().length"
      data-testid="service-request-alert"
      role="alert"
      class="fixed top-0 inset-x-0 w-full z-50 border-b-4 border-red-800 bg-red-600 px-4 py-2 text-white shadow-lg"
    >
      <div
        v-for="request in openServiceRequests()"
        :key="request.name"
        :data-escalated="serviceRequestEscalated(request) ? 'true' : 'false'"
        :class="[
          'my-1 flex flex-wrap items-center gap-4 rounded-md px-3 py-2 text-2xl font-bold',
          serviceRequestEscalated(request) ? 'animate-pulse bg-red-900 ring-4 ring-yellow-300' : 'bg-amber-500 text-gray-950',
        ]"
      >
        <span aria-hidden="true">🔔</span>
        <span>{{ request.table }}</span>
        <span>{{ request.request_type === "Bill" ? __("Bill") : __("Call waiter") }}</span>
        <span>{{ serviceRequestMinutes(request) }} min</span>
      </div>
    </div>
    <!-- Alert Modal div start-->
    <div
      v-if="this.showModal"
      class="fixed inset-0 z-10 overflow-y-auto"
      style="background: rgba(13, 13, 15, 0.2)"
    >
      <div class="flex items-center justify-center">
        <div class="w-full rounded-lg p-6 md:max-w-md" style="background: var(--panel); border: 1px solid var(--hair); box-shadow: 0 8px 30px rgba(0,0,0,.12)">
          <p
            class="block text-start text-xl font-medium"
            style="color: var(--t1)"
          >
            <span
              class="w-3 h-3 rounded-full inline-block me-1"
              style="background: var(--rd)"
            ></span>
            {{ __("Not Permitted") }}
          </p>
          <hr style="border-color: var(--hair)" />

          <p class="text-start text-xl mt-6 font-medium" style="color: var(--t2)">
            {{ __("Log in to access this page.") }}
          </p>

          <div class="flex justify">
            <button
              @click="
                this.showModal = false;
                this.redirectToLogin();
              "
              class="mt-8 rounded px-3 py-2"
              style="background: var(--ac); color: #fff"
            >
              {{ __("Login") }}
            </button>
          </div>
        </div>
      </div>
    </div>
    <!-- Alert Modal div end-->

    <div v-if="actionError" role="alert" class="mb-4 rounded-lg p-4" style="background: var(--rd-t); border: 1px solid var(--rd); color: var(--rd)">
      {{ actionError }}
    </div>

    <div v-if="failedPrints.length" role="status" class="mb-4 rounded-lg p-4 flex justify-between items-center gap-4" style="background: var(--rd-t); border: 1px solid var(--rd); color: var(--rd)">
      <span>{{ __("{0} tickets not printed — Retry", failedPrints.length) }}</span>
      <button @click="retryFailedPrints" class="rounded px-4 py-2 font-semibold" style="background: var(--rd); color: #fff">{{ __("Retry") }}</button>
    </div>
    <p v-if="printError" role="alert" class="mb-4 rounded p-3" style="background: var(--rd-t); border: 1px solid var(--rd); color: var(--rd)">{{ printError }}</p>
    <button v-if="branch" @click="openPrintSettings" class="mb-4 rounded border px-4 py-2 font-semibold" style="border-color: var(--ac); color: var(--ac)">{{ __("Manager settings") }}</button>
    <AvailabilityDrawer :branch="availabilityBranch" :roles="availabilityRoles" :call="call" :socket="availabilitySocket" />

    <div v-if="showPrintSettings && canManagePrintSettings" class="fixed inset-0 z-30 flex items-center justify-center p-4 overflow-y-auto" style="background: rgba(13, 13, 15, 0.4)">
      <form @submit.prevent="savePrintSettings" role="dialog" aria-modal="true" aria-labelledby="station-print-title" class="w-full max-w-lg rounded-lg p-6" style="background: var(--panel); border: 1px solid var(--hair); box-shadow: 0 8px 30px rgba(0,0,0,.12)">
        <h2 id="station-print-title" class="text-xl font-bold" style="color: var(--t1)">{{ __("Manager station printing settings") }}</h2>
        <p class="mt-2 text-sm" style="color: var(--t2)">{{ __("{0} · This browser only. Use one station per unit. New tickets print from the time this is enabled.", production) }}</p>
        <p v-if="stationSettingsError" role="alert" class="mt-3" style="color: var(--rd)">{{ stationSettingsError }}</p>
        <label class="mt-4 block font-semibold">{{ __("Printer") }}
          <select v-model="stationSettings.printer" class="mt-1 w-full rounded border p-2">
            <option value="">{{ __("Network printer (host and port)") }}</option>
            <option v-if="stationSettings.printer && !installedPrinters.includes(stationSettings.printer)" :value="stationSettings.printer">{{ __("{0} (saved, not found)", stationSettings.printer) }}</option>
            <option v-for="name in installedPrinters" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
        <p v-if="printerListError" class="mt-1 text-sm" style="color: var(--t2)">{{ printerListError }}</p>
        <template v-if="!stationSettings.printer">
          <label class="mt-3 block font-semibold">{{ __("Printer host") }}
            <input v-model="stationSettings.host" class="mt-1 w-full rounded border p-2" type="text" autocomplete="off" required>
          </label>
          <label class="mt-3 block font-semibold">{{ __("Printer port") }}
            <input v-model.number="stationSettings.port" class="mt-1 w-full rounded border p-2" type="number" min="1" max="65535" required>
          </label>
        </template>
        <label class="mt-3 block font-semibold">{{ __("Print format") }}
          <input v-model="stationSettings.printFormat" class="mt-1 w-full rounded border p-2" type="text" required>
        </label>
        <label class="mt-3 block font-semibold">{{ __("Paper width") }}
          <select v-model.number="stationSettings.pageWidth" class="mt-1 w-full rounded border p-2">
            <option value="576">80 mm</option>
            <option value="384">58 mm</option>
          </select>
        </label>
        <label class="mt-3 block font-semibold">{{ __("Printer command mode") }}
          <select v-model="stationSettings.commandMode" class="mt-1 w-full rounded border p-2">
            <option value="standard">Standard (ESC *)</option>
            <option value="raster">Raster (GS v 0)</option>
          </select>
        </label>
        <label class="mt-3 block font-semibold">{{ __("Backup printer") }}
          <input
            v-model="stationSettings.backupPrinter"
            @input="onBackupPrinterInput"
            data-testid="backup-printer-input"
            class="mt-1 w-full rounded border p-2"
            type="text"
            autocomplete="off"
            :placeholder="__('None')"
          >
        </label>
        <template v-if="!stationSettings.backupPrinter">
          <label class="mt-3 block font-semibold">{{ __("Backup printer host") }}
            <input
              v-model="stationSettings.backupHost"
              data-testid="backup-host-input"
              class="mt-1 w-full rounded border p-2"
              type="text"
              autocomplete="off"
              :placeholder="__('None')"
            >
          </label>
          <label class="mt-3 block font-semibold">{{ __("Backup printer port") }}
            <input
              v-model.number="stationSettings.backupPort"
              data-testid="backup-port-input"
              class="mt-1 w-full rounded border p-2"
              type="number"
              min="1"
              max="65535"
            >
          </label>
        </template>
        <label class="mt-4 flex items-center gap-2 font-semibold">
          <input v-model="stationSettings.enabled" type="checkbox"> {{ __("Enable station printing") }}
        </label>
        <div class="mt-6 flex justify-end gap-3">
          <button type="button" @click="showPrintSettings = false" class="rounded border px-4 py-2">{{ __("Cancel") }}</button>
          <button type="submit" class="rounded px-4 py-2 font-semibold" style="background: var(--ac); color: #fff">{{ __("Save") }}</button>
        </div>
      </form>
    </div>

    <!-- '' until kot_list answers; an all-branch admin board answers with Branch null. -->
    <div v-if="branch !== ''" class="mb-4">
      <button @click="fetchRecentServed" :disabled="loadingServed" class="rounded px-3 py-2 disabled:opacity-50" style="background: var(--ac); color: #fff">
        {{ loadingServed ? __("Loading…") : __("Recently served · Recall") }}
      </button>
      <div v-if="showRecentServed" class="mt-3 rounded-lg border p-3" style="border-color: var(--hair)">
        <p class="mb-2" style="color: var(--t3)">{{ __("Recall is available for {0} minutes after serving.", recallWindowMinutes) }}</p>
        <p v-if="!recentServed.length && !loadingServed" style="color: var(--t3)">{{ __("No recently served tickets.") }}</p>
        <div v-for="served in recentServed" :key="served.name" class="flex justify-between items-center py-2">
          <span>{{ served.table_takeaway || !served.restaurant_table ? __("Takeaway") : served.restaurant_table }} · {{ daily_order_number ? served.order_no : served.invoice.slice(-4) }}</span>
          <button @click="recallOrder(served)" :disabled="recalling[served.name]" class="rounded px-3 py-2 disabled:opacity-50" style="background: var(--ac); color: #fff">
            {{ recalling[served.name] ? __("Recalling…") : __("Recall") }}
          </button>
        </div>
      </div>
    </div>

    <!-- Sticky collapsible All-Day production summary bar -->
    <div
      v-if="allDaySummary.length"
      data-testid="all-day-summary"
      class="sticky top-12 z-20 mb-4 rounded-lg border p-3"
      style="background: var(--panel); border-color: var(--hair2)"
    >
      <div class="flex items-center justify-between gap-2">
        <button
          type="button"
          @click="allDayExpanded = !allDayExpanded"
          class="flex items-center gap-2 font-bold text-sm"
          style="color: var(--t1); min-height: 36px"
        >
          <span>{{ __("All-Day ({0})", allDaySummary.length) }}</span>
          <span aria-hidden="true">{{ allDayExpanded ? "▾" : "▸" }}</span>
        </button>
        <span v-if="isExpo" class="rounded px-2 py-0.5 text-xs font-bold uppercase" style="background: var(--ac); color: #fff">
          {{ __("Expo Station") }}
        </span>
      </div>
      <div v-if="allDayExpanded" class="mt-2 flex flex-wrap gap-2">
        <span
          v-for="row in allDaySummary"
          :key="`${row.item_name}-${row.course}`"
          class="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-sm font-semibold"
          :style="row.rush_qty > 0 ? { background: 'var(--rd-t)', borderColor: 'var(--rd)', color: 'var(--rd)' } : { background: 'var(--bg)', borderColor: 'var(--hair)', color: 'var(--t1)' }"
        >{{ row.total_qty }}× {{ row.item_name }}</span>
      </div>
    </div>

    <!-- Expo Mode Board: unified multi-station cards per check -->
    <div v-if="isExpo" data-testid="expo-board">
      <div v-if="expoOrders.length === 0 && !loadingKots" class="text-center py-10 text-xl" style="color: var(--t3)">
        {{ __("No active orders for {0}", production) }}
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <div
          v-for="order in expoOrders"
          :key="order.invoice"
          data-testid="expo-order-card"
          :class="[{ 'ring-4 ring-red-600': Number(order.custom_is_rush) === 1 || order.late }]"
          class="kot-card rounded-lg border"
          style="background: var(--panel); border-color: var(--hair2)"
        >
          <div class="kot-header flex justify-between items-start">
            <div class="kot-identity text-sm">
              <div v-if="Number(order.custom_is_rush) === 1 || Number(order.custom_is_vip) === 1" class="mb-1 flex flex-wrap gap-1">
                <span v-if="Number(order.custom_is_rush) === 1" data-testid="kot-rush-badge" class="inline-block rounded px-2 py-0.5 text-xs font-extrabold uppercase tracking-wide" style="background: var(--rd); color: #fff">{{ __("RUSH") }}</span>
                <span v-if="Number(order.custom_is_vip) === 1" data-testid="kot-vip-badge" class="inline-block rounded px-2 py-0.5 text-xs font-extrabold uppercase tracking-wide" style="background: #d97706; color: #fff">{{ __("VIP") }}</span>
              </div>
              <span class="kot-table font-semibold" style="color: var(--t1)">
                {{ order.tableortakeaway === "Takeaway" || order.table_takeaway == 1 || !order.restaurant_table ? __("Takeaway") : order.tableortakeaway || order.restaurant_table }}
              </span>
              <span class="ms-2 text-sm font-semibold" style="color: var(--t2); font-family: var(--m)">
                {{ daily_order_number ? order.order_no : String(order.invoice || "").slice(-4) }}
              </span>
              <div v-if="order.custom_guest_label" class="text-sm font-semibold" style="color: var(--t1)">
                {{ __("Guest") }}: {{ order.custom_guest_label }}
              </div>
            </div>
            <span
              class="rounded px-2 py-1 text-xs font-bold"
              :style="order.all_stations_ready ? { background: 'var(--gr-t)', color: 'var(--gr)' } : { background: 'var(--am-t)', color: 'var(--am)' }"
            >
              {{ order.all_stations_ready ? __("All stations ready") : __("Waiting on stations") }}
            </span>
          </div>

          <div class="mt-3 flex flex-wrap gap-2">
            <span
              v-for="station in order.stations"
              :key="station.kot_name"
              data-testid="expo-station-chip"
              class="inline-flex items-center gap-1 rounded px-2 py-1 text-xs font-bold border"
              :style="expoStationChipStyle(station.status)"
            >
              <span>{{ station.production }}:</span>
              <span>{{ __(station.status) }}</span>
            </span>
          </div>

          <div class="mt-3 space-y-2">
            <div v-for="station in order.stations" :key="`items-${station.kot_name}`" class="rounded border p-2" style="border-color: var(--hair)">
              <div class="text-xs font-bold uppercase tracking-wide mb-1" style="color: var(--t3)">{{ station.production }}</div>
              <template v-for="group in station.course_groups" :key="`${station.kot_name}-${group.course}`">
                <div v-if="group.course" data-testid="kot-course-header" class="text-xs font-bold uppercase tracking-wide px-1.5 py-0.5 rounded my-1" style="background: var(--bg); color: var(--t2)">
                  {{ group.course }}
                </div>
                <div v-for="kotitem in group.items" :key="kotitem.name" class="flex items-center justify-between text-base font-semibold py-0.5" style="color: var(--t1)">
                  <span><span dir="ltr" style="font-family: var(--m)">{{ kotitem.qty ?? kotitem.quantity }}</span> {{ kotitem.item_name }}</span>
                  <span class="px-1.5 py-0.5 rounded text-xs font-semibold" :style="itemStatusStyle(kotitem.state)">{{ itemStatusLabel(kotitem.state) }}</span>
                </div>
              </template>
            </div>
          </div>

          <footer class="kot-actions mt-4 border-t pt-3" style="border-color: var(--hair)">
            <button
              type="button"
              data-testid="expo-bump-btn"
              @click.stop="expoBumpOrder(order)"
              :disabled="!!bumpingExpo[order.invoice]"
              class="w-full py-2 px-6 rounded-md font-semibold disabled:opacity-50"
              style="background: var(--gr); color: #fff"
            >
              {{ bumpingExpo[order.invoice] ? __("Bumping…") : __("Expo Bump") }}
            </button>
          </footer>
        </div>
      </div>
    </div>

    <template v-else>
    <div v-if="visibleKots.length === 0 && !loadingKots" class="text-center py-10 text-xl" style="color: var(--t3)">
      {{ __("No active orders for {0}", production) }}
    </div>

    <div class="grid kot-grid">
      <template v-for="kot in visibleKots" :key="kot.name">
        <div class="kot-ticket masonry-item">
        <div
          :class="[kot.color, { 'ring-4 ring-red-600': kot.late || Number(kot.custom_is_rush) === 1, 'ring-2 ring-amber-500': !kot.late && Number(kot.custom_is_rush) !== 1 && Number(kot.custom_is_vip) === 1 }]"
          class="kot-card rounded-lg border"
          style="border-color: var(--hair2)"
        >
          <div class="check">
              <!-- Card Header: Table Name and Order Number -->
              <div class="kot-header flex justify-between" @click="rotateCard(kot)">
                <div class="kot-identity text-sm" style="font-family: var(--s)">
                  <div v-if="Number(kot.custom_is_rush) === 1 || Number(kot.custom_is_vip) === 1" class="mb-1 flex flex-wrap gap-1">
                    <span v-if="Number(kot.custom_is_rush) === 1" data-testid="kot-rush-badge" class="inline-block rounded px-2 py-0.5 text-xs font-extrabold uppercase tracking-wide" style="background: var(--rd); color: #fff">{{ __("RUSH") }}</span>
                    <span v-if="Number(kot.custom_is_vip) === 1" data-testid="kot-vip-badge" class="inline-block rounded px-2 py-0.5 text-xs font-extrabold uppercase tracking-wide" style="background: #d97706; color: #fff">{{ __("VIP") }}</span>
                  </div>
                  <span
                    v-if="kot.tableortakeaway !== 'Takeaway'"
                    class="text-sm font-medium me-1"
                    style="color: var(--t2)"
                    >{{ __("Table") }}
                  </span>
                  <span class="kot-table font-semibold" style="color: var(--t1)">
                    {{ kot.tableortakeaway === "Takeaway" ? __("Takeaway") : kot.tableortakeaway }}
                    <span class="kot-user text-sm font-medium" style="color: var(--t2)"
                      >( {{ kot.user }} )</span
                    ></span
                  >
                  <span v-if="kot.is_aggregator" class="text-sm font-medium" style="color: var(--t2)">{{ __("Aggregator") }}</span>
                  <span v-if="kot.is_aggregator" class="ms-2 font-semibold" style="color: var(--t1)"
                    >{{ kot.customer_name }}
                  </span><br v-if="kot.is_aggregator" />
                  <span v-if="kot.is_aggregator" class="text-sm font-medium" style="color: var(--t2)">{{ __("Aggregator ID") }}</span>
                  <span v-if="kot.is_aggregator" class="ms-2 font-semibold" style="color: var(--t1)"
                    >{{ kot.aggregator_id }}
                  </span><br v-if="kot.is_aggregator"/>
                  <template v-if="kot.custom_guest_label">
                    <span class="text-sm font-medium" style="color: var(--t2)">{{ __("Guest") }}</span>
                    <span class="kot-guest ms-2 font-semibold" style="color: var(--t1)">{{ kot.custom_guest_label }}</span><br />
                  </template>
                  <span class="text-sm font-medium" style="color: var(--t2)">{{ __("Order") }}</span>
                  <span class="ms-2 font-semibold" style="color: var(--t1); font-family: var(--m)"
                    >{{ this.daily_order_number ? kot.order_no : kot.invoice.slice(-4) }}
                  </span>
                  <span
                    class="ms-2 font-semibold"
                    style="color: var(--t1)"
                    v-if="
                      kot.type === 'Partially cancelled' ||
                      kot.type === 'Cancelled'
                    "
                  >
                    ( {{ __(kot.type) }} )</span
                  >
                </div>
                <div
                  :class="kot.timecolor"
                  dir="ltr" class="kot-age font-semibold leading-10"
                  style="font-family: var(--m); font-variant-numeric: tabular-nums"
                >
                  {{ formatElapsed(kot.timeRemaining) }}
                  <span v-if="kot.late" class="block text-sm leading-4" style="color: var(--rd)">{{ __("LATE") }}</span>
                </div>
              </div>
              <p v-if="kot.type === 'Order Modified'" class="font-semibold mt-2">{{ __("Order Modified") }}</p>
              <div
                v-if="kot.type === 'Duplicate'"
                class="font-medium"
                style="color: var(--rd); font-family: var(--s)"
              >
                ( {{ __("Duplicate KOT ( CHECK WITH CAPTAIN )") }} )
              </div>
              <div v-show="kot.comments" class="kot-note">
                <span class="kot-note-label">{{ kot.type === 'Cancelled' || kot.type === 'Partially cancelled' ? __("Cancellation remarks") : __("Special request") }}</span>
                ( {{ kot.comments }} )
              </div>
              <div>
                <template v-for="group in groupedKotItems(kot)" :key="`${kot.name}-course-${group.course}`">
                  <div
                    v-if="group.course"
                    data-testid="kot-course-header"
                    class="mt-2 mb-1 rounded px-2 py-0.5 text-xs font-bold uppercase tracking-wide"
                    style="background: var(--bg); color: var(--t2); border-left: 3px solid var(--ac)"
                  >
                    {{ group.course }}
                  </div>
                  <div
                    class="font-semibold"
                    v-for="kotitem in group.items"
                    :key="kotitem.name"
                  >
                    <div
                      @click="
                        () => {
                          toggleItemStrikeThrough(kotitem, kot);
                        }
                      "
                      :class="{
                        'line-through': kotitem.striked,
                      }"
                      :style="{ color: kotitem.striked ? 'var(--gr)' : 'var(--t1)' }"
                      class="kot-item flex font-semibold items-start"
                    >
                      <div class="kot-quantity" dir="ltr" style="font-family: var(--m); font-variant-numeric: tabular-nums">
                        <span>{{ kotitem.qty }}</span>
                      </div>
                      <div class="kot-dish-detail">
                        <span class="kot-dish">{{
                          kotitem.item_name
                        }}<span v-if="kotitem.seat_no" class="text-xs ms-1 px-1 rounded" style="background: var(--bg); color: var(--t2)">S{{ kotitem.seat_no }}</span><span v-show="kotitem.indicate_course" class="text-sm ms-1" style="color: var(--t3)"> ( {{kotitem.course}} )</span>
                        </span
                        ><br />
                        <span
                          class="ms-2"
                          v-if="
                            kot.type === 'Partially cancelled' ||
                            kot.type === 'Cancelled'
                          "
                          >[{{ __("Old Qty = {0}", kotitem.quantity) }}]</span
                        >
                      </div>
                      <span
                        data-testid="kot-item-status"
                        class="ms-auto px-1.5 py-0.5 rounded text-xs font-semibold"
                        :style="itemStatusStyle(kotitem.state)"
                        >{{ itemStatusLabel(kotitem.state) }}</span
                      >
                    </div>
                    <div>
                      <p
                        v-show="kotitem.comments"
                        class="kot-note"
                      >
                        <span class="kot-note-label">{{ kot.type === 'Cancelled' || kot.type === 'Partially cancelled' ? __("Cancellation remarks") : __("Special request") }}</span>
                        {{ kotitem.comments }}
                      </p>
                      <!-- Cancellation disposition: return-to-stock vs waste -->
                      <div
                        v-if="Number(kotitem.cancelled_qty) > 0 && (!kotitem.disposition || kotitem.disposition === 'Pending')"
                        class="ms-2 mt-1 flex gap-2"
                        @click.stop
                      >
                        <span class="text-sm font-medium" style="color: var(--t2)">{{ __("Disposition:") }}</span>
                        <button
                          @click="resolveDisposition(kot, kotitem, 'return_to_stock')"
                          class="px-2 py-1 rounded text-xs font-semibold"
                          style="background: var(--gr); color: #fff"
                        >
                          {{ __("Return to Stock") }}
                        </button>
                        <button
                          @click="resolveDisposition(kot, kotitem, 'waste')"
                          class="px-2 py-1 rounded text-xs font-semibold"
                          style="background: var(--rd); color: #fff"
                        >
                          {{ __("Mark as Wasted") }}
                        </button>
                      </div>
                      <div
                        v-else-if="kotitem.disposition && kotitem.disposition !== 'Pending'"
                        class="ms-2 mt-1 text-sm font-medium"
                        style="color: var(--t2)"
                      >
                        ( {{ __("Disposition: {0}", __(kotitem.disposition)) }} )
                      </div>
                      <hr class="my-1 mt-2" style="border-color: var(--hair)" />
                    </div>
                  </div>
                </template>
              </div>
          </div>
          <footer @click.self="kot.isRotated && rotateCard(kot)" class="kot-actions mt-4 border-t pt-4" style="border-color: var(--hair)" :class="{ 'ring-2 ring-blue-600 rounded-lg': kot.isRotated }">
            <button
              v-if="!pendingServes[kot.name]"
              @click.stop="
                kot.type === 'Cancelled' || kot.type === 'Partially cancelled'
                  ? confirmOrder(kot)
                  : serveOrder(kot)
              "
              :disabled="!!pendingServes[kot.name] || !!confirming[kot.name]"
              class="w-full py-2 px-6 rounded-md transition duration-300 ease-in-out disabled:opacity-50"
              style="background: var(--ac); color: #fff; font-family: var(--s); font-weight: 550"
            >
              {{ kot.type === "Cancelled" || kot.type === "Partially cancelled" ? __("Confirm") : __("Serve") }}
            </button>
            <button @click.stop="printKot(kot, { retry: true })" :disabled="!stationSettings.enabled || printingKots.includes(kot.name) || printedKots.includes(kot.name)" class="mt-2 w-full rounded border py-2 px-6 font-semibold disabled:opacity-50" style="border-color: var(--ac); color: var(--ac)">{{ __("Print") }}</button>
            <div v-if="pendingServes[kot.name]" class="flex justify-between items-center gap-3" style="color: var(--ac)">
              <span>{{ pendingServes[kot.name].sending ? __("Serving…") : __("Serving in 5 seconds…") }}</span>
              <button v-if="!pendingServes[kot.name].sending" @click.stop="undoServe(kot)" class="rounded border px-3 py-1 font-semibold" style="border-color: var(--ac)">{{ __("Undo") }}</button>
            </div>
          </footer>
        </div>
        </div>
      </template>
    </div>
    </template>

    <!-- Audio Alert Message -->
    <div
      v-if="showAudioAlertMessage"
      class="absolute top-1 left-1/2 transform -translate-x-1/2 p-2 font-bold text-2xl text-center"
      style="color: var(--rd)"
    >
      {{ __("Audio notifications disabled. Click anywhere to enable.") }}
    </div>

    <!-- KOT Delay Error Alert Banner -->
    <div
      v-if="showKotErrorAlert && kotErrorAlert"
      class="fixed top-0 inset-x-0 mx-auto p-4 z-40 flex justify-between items-center"
      style="background: var(--rd-t); border-bottom: 3px solid var(--rd)"
    >
      <div class="flex items-center gap-3">
        <div class="flex-shrink-0">
          <span class="text-3xl">⚠️</span>
        </div>
        <div class="flex-1">
          <p class="font-bold" style="color: var(--rd)">{{ __("Order Delayed") }}</p>
          <p class="text-sm mt-1" style="color: var(--rd)">
            <span v-if="!daily_order_number">{{ __("Invoice: {0}", kotErrorAlert.invoice.slice(-4)) }}</span>
            <span v-else>{{ __("Order #: {0}", kotErrorAlert.order_no) }}</span>
            | {{ __("Table: {0}", kotErrorAlert.tableortakeaway) }} | {{ __("Time: {0}", kotErrorAlert.timestamp) }}
            <span v-if="kotErrorAlert.guest_label"> | {{ __("Guest: {0}", kotErrorAlert.guest_label) }}</span>
          </p>
        </div>
      </div>
      <button
        @click="hideKotErrorAlert"
        class="ms-4 font-bold text-xl flex-shrink-0"
        style="color: var(--rd)"
      >
        ✕
      </button>
    </div>

    <div
      v-if="statusMessage"
      :class="[
        'fixed',
        'bottom-10',
        'end-10',
        'p-4',
        'rounded',
        'text-white',
        {
          'bg-[var(--gr)]': isOnline,
          'bg-[var(--rd)]': !isOnline,
        },
      ]"
      @transitionend="handleTransitionEnd"
    >
      {{ statusMessage }}
    </div>
  </div>
</template>

<script>
import { clientMessage } from "../../../packages/core/src/brand.ts";
import { FrappeApp } from "frappe-js-sdk";
import Masonry from "masonry-layout";
import io from "socket.io-client";
import { t, isRTL } from "../i18n";
import { canRecallKot, isKotLate, kitchenErrorMessage, recentServedQuery, rememberKot } from "./kitchen-actions.js";
import { createStationPrinter } from "./station-print.js";
import AvailabilityDrawer from "./AvailabilityDrawer.vue";

let host = window.location.hostname;
let port = window.location.port;
let protocol = window.location.protocol;
let url = port ? `${protocol}//${host}:${port}` : `${protocol}//${host}`;
window.globalSiteName = '';
// Same semantics as POS badges: queued=red, preparing=amber, ready=info(blue), served=green.
const ITEM_STATUS = {
  QUEUED: { label: "Queued", bg: "var(--rd-t)", fg: "var(--rd)", bd: "var(--rd-b)" },
  IN_PREPARATION: { label: "Preparing", bg: "var(--am-t)", fg: "var(--am)", bd: "var(--am-b)" },
  READY: { label: "Ready", bg: "#EAF2FB", fg: "#1D5FA8", bd: "#C3D9F0" },
  SERVED: { label: "Served", bg: "var(--gr-t)", fg: "var(--gr)", bd: "var(--gr-b)" },
};
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
  components: { AvailabilityDrawer },
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
      availabilityBranch: "",
      availabilityRoles: [],
      kot_channel: "",
      kot_error_channel: "",
      kot_priority_channel: "",
      kot_branch_priority_channel: "",
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
      serviceRequests: [],
      serviceRequestSettings: { sound: 1, escalate_minutes: 3 },
      serviceRequestPoll: null,
      serviceRequestChime: null,
      serverTimeOffset: 0,
      serviceRequestHandlers: null,
      actionError: "",
      pendingServes: {},
      confirming: {},
      recalling: {},
      recentServed: [],
      showRecentServed: false,
      loadingServed: false,
      timeInterval: null,
      stationPrinter: null,
      stationSettings: { enabled: false, enabledAt: null, printer: "", host: "", port: 9100, backupPrinter: "", backupHost: "", backupPort: 9100, printFormat: "", pageWidth: 576, commandMode: "standard" },
      installedPrinters: [],
      printerListError: "",
      failedPrints: [],
      printingKots: [],
      printedKots: [],
      printError: "",
      canManagePrintSettings: false,
      showPrintSettings: false,
      stationSettingsError: "",
      recallWindowMinutes: 15,
      isExpo: false,
      allDayExpanded: true,
      bumpingExpo: {},
      serverDeviceConfig: null,
      heartbeatInterval: null,
    };
  },
  methods: {
    // Station printing: this browser prints its unit's new KOTs through signed QZ jobs and records
    // [KOT-PRINTED] / [KOT-PRINT-FAILED] Comments on the KOT.
    setupStationPrinting() {
      if (!this.stationPrinter) {
        this.stationPrinter = createStationPrinter({
          production: this.production, user: this.loggeduser, call: this.call,
          serverSettings: this.serverDeviceConfig,
          now: () => Date.now() + this.serverTimeOffset,
          onChange: state => {
            this.failedPrints = state.failed;
            this.printingKots = state.pending;
            this.printedKots = state.printed;
            if (state.error) this.printError = state.error;
          },
        });
      } else if (this.serverDeviceConfig && typeof this.stationPrinter.hydrateFromServer === "function") {
        this.stationPrinter.hydrateFromServer(this.serverDeviceConfig);
      }
      this.stationSettings = this.stationPrinter.settings;
    },
    async printKot(kot, options = {}) {
      try {
        this.setupStationPrinting();
        await this.stationPrinter.process(kot, options);
        this.failedPrints = this.stationPrinter.failed();
      } catch (error) {
        this.printError = kitchenErrorMessage(error, t("Station printing is unavailable."));
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
        const { message } = await this.call.get("ury.ury.doctype.ury_order.ury_order.get_captain_context", {});
        const roles = message.roles || [];
        this.canManagePrintSettings = this.loggeduser === "Administrator" || roles.some(role => ["System Manager", "URY Admin", "URY Manager"].includes(role));
        if (!this.canManagePrintSettings) throw Error(t("A manager must sign in to change station printing settings."));
        this.setupStationPrinting();
        this.stationSettingsError = "";
        this.showPrintSettings = true;
        this.loadInstalledPrinters();
      } catch (error) {
        this.printError = kitchenErrorMessage(error, t("Could not open station printing settings."));
      }
    },
    async loadInstalledPrinters() {
      this.printerListError = "";
      try {
        const names = await this.stationPrinter.listPrinters();
        this.installedPrinters = Array.isArray(names) ? names : [names].filter(Boolean);
      } catch (error) {
        this.printerListError = kitchenErrorMessage(error, t("Installed printers are unavailable. Start QZ Tray, or use a network printer."));
      }
    },
    onBackupPrinterInput() {
      if (this.stationSettings.backupPrinter && this.stationSettings.backupPrinter.trim()) {
        this.stationSettings.backupHost = "";
      }
    },
    savePrintSettings() {
      if (!this.canManagePrintSettings) return;
      try {
        const next = { ...this.stationSettings };
        if (next.printer) next.host = "";
        if (String(next.backupPrinter || "").trim()) next.backupHost = "";
        this.stationSettings = this.stationPrinter.configure(next);
        this.showPrintSettings = false;
        if (this.call && typeof this.call.post === "function" && this.production) {
          this.call.post("ury.ury.api.ury_kot_display.save_station_device_config", {
            production_unit: this.production,
            config: JSON.stringify(this.stationSettings),
          }).catch(() => {});
        }
      } catch (error) {
        this.stationSettingsError = clientMessage(error.message || t("Could not load printer settings."));
      }
    },
    sendStationHeartbeat() {
      if (!this.production || !this.call || typeof this.call.post !== "function") return;
      return this.call
        .post("ury.ury.api.ury_kot_display.record_station_heartbeat", {
          production_unit: this.production,
        })
        .catch(() => {});
    },
    startStationHeartbeat() {
      this.stopStationHeartbeat();
      if (!this.production || !this.serverDeviceConfig?.auto_failover_enabled) return;
      const timeoutSec = Number(this.serverDeviceConfig.heartbeat_timeout_seconds) || 120;
      const intervalMs = Math.max(10000, Math.min(30000, Math.floor((timeoutSec * 1000) / 3)));
      this.sendStationHeartbeat();
      this.heartbeatInterval = setInterval(() => {
        if (this.socketActive) this.sendStationHeartbeat();
      }, intervalMs);
    },
    stopStationHeartbeat() {
      if (this.heartbeatInterval) {
        clearInterval(this.heartbeatInterval);
        this.heartbeatInterval = null;
      }
    },
    playAlertSound(path) {
      var currentDomain = window.location.origin;
      var audio_path = currentDomain + path;
      const audio = new Audio(audio_path);
      audio.play();
    },
    // Guest calls (Oracle Simphony guest-request alerts): shown on every board
    // until the floor acknowledges them. The server publishes to each branch staff
    // user's own room, so the board joins no shared room; the 15 s poll is the
    // fallback. Sound and escalation come from the POS Profile.
    async refreshServiceRequests() {
      if (!this.branch) return;
      try {
        const { message } = await this.call.get("ury.ury.api.service_requests.list_open_service_requests", { branch: this.branch });
        this.serviceRequests = Array.isArray(message) ? message : [];
      } catch (error) {
        console.error("Service requests unavailable:", error);
      }
      this.updateServiceRequestChime();
    },
    async startServiceRequests() {
      try {
        const { message } = await this.call.get("ury.ury.api.service_requests.get_service_request_alert_settings", { branch: this.branch });
        if (message) this.serviceRequestSettings = message;
      } catch (error) {
        console.error("Service request settings unavailable:", error);
      }
      try {
        const { message } = await this.call.get("ury.ury.api.ury_server_time.get_server_time", {});
        const serverNow = new Date(String(message).replace(" ", "T")).getTime();
        if (Number.isFinite(serverNow)) this.serverTimeOffset = serverNow - Date.now();
      } catch (error) {
        console.error("Server time unavailable:", error);
      }
      clearInterval(this.serviceRequestPoll);
      this.serviceRequestPoll = setInterval(this.refreshServiceRequests, 15000);
      if (socket) {
        this.serviceRequestHandlers = {
          ury_service_request: (doc) => {
            if (doc && doc.branch && doc.branch !== this.branch) return;
            this.refreshServiceRequests();
          },
          connect: () => { this.refreshServiceRequests(); },
        };
        for (const [event, handler] of Object.entries(this.serviceRequestHandlers)) socket.on(event, handler);
      }
      await this.refreshServiceRequests();
    },
    stopServiceRequests() {
      clearInterval(this.serviceRequestPoll);
      clearInterval(this.serviceRequestChime);
      this.serviceRequestChime = null;
      if (socket && this.serviceRequestHandlers) {
        for (const [event, handler] of Object.entries(this.serviceRequestHandlers)) socket.off(event, handler);
      }
      this.serviceRequestHandlers = null;
    },
    openServiceRequests() {
      return this.serviceRequests.filter(request => request.status === "Open");
    },
    serviceRequestMinutes(request) {
      const at = new Date(String(request.requested_at).replace(" ", "T")).getTime();
      if (!Number.isFinite(at)) return 0;
      return Math.max(0, Math.floor((Date.now() + this.serverTimeOffset - at) / 60000));
    },
    serviceRequestEscalated(request) {
      return this.serviceRequestMinutes(request) >= Number(this.serviceRequestSettings.escalate_minutes || 3);
    },
    updateServiceRequestChime() {
      const ringing = this.openServiceRequests().length > 0 && Number(this.serviceRequestSettings.sound) === 1;
      if (ringing && !this.serviceRequestChime) {
        const chime = () => { try { this.playAlertSound("/assets/frappe/sounds/alert.mp3"); } catch (error) { console.error(error); } };
        chime();
        this.serviceRequestChime = setInterval(chime, 10000);
      } else if (!ringing && this.serviceRequestChime) {
        clearInterval(this.serviceRequestChime);
        this.serviceRequestChime = null;
      }
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
            .get("ury.ury.api.ury_kot_display.kot_list", { production_unit: this.production })
            .then((result) => {
              // A newer connect/disconnect (or unmount) owns the board; drop this older snapshot.
              if (generation !== this.resyncGeneration) { resolve(); return; }
              const missedTickets = resync && result.message.KOT.some(
                kot => kot.production === this.production && !this.knownKots.has(kot.name)
              );
              this.branch = result.message.Branch;
              this.availabilityBranch = result.message.availability_branch ?? this.branch ?? "";
              this.availabilityRoles = result.message.roles ?? [];
              const serverNow = new Date(String(result.message.server_time || "").replace(" ", "T")).getTime();
              if (Number.isFinite(serverNow)) this.serverTimeOffset = serverNow - Date.now();
              this.kot_alert_time = result.message.kot_alert_time;
              this.audio_alert = result.message.audio_alert;
              this.daily_order_number = result.message.daily_order_number;
              const configuredRecall = Number(result.message.recall_window_minutes);
              if (Number.isFinite(configuredRecall) && configuredRecall > 0) {
                this.recallWindowMinutes = Math.max(1, Math.min(240, Math.floor(configuredRecall)));
              }
              this.isExpo = Boolean(result.message.is_expo);
              if (result.message.station_device_config) {
                this.serverDeviceConfig = result.message.station_device_config;
                if (this.stationPrinter && typeof this.stationPrinter.hydrateFromServer === "function") {
                  this.stationSettings = this.stationPrinter.hydrateFromServer(this.serverDeviceConfig);
                }
              }
              this.kot_channel = `kot_update_${this.branch}_${this.production}`;
              this.kot_error_channel = `kot_error_${this.branch}_${this.production}`;
              const priorityBranch = this.branch || this.availabilityBranch;
              this.kot_priority_channel = priorityBranch ? `kot_priority_${priorityBranch}_${this.production}` : "";
              this.kot_branch_priority_channel = priorityBranch ? `kot_priority_${priorityBranch}` : "";
              this.kot = result.message.KOT;
              this.kot.forEach(kot => this.knownKots.add(kot.name));
              this.loadingKots = false;
              this.updateQtyColorTable();
              this.updateTimeRemaining();
              this.masonryLoading();
              this.printTickets(this.kot);
              // Socket.IO does not replay tickets sent while disconnected: chime once for all of them.
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
    itemStatusLabel(state) {
      return t(ITEM_STATUS[state || "QUEUED"]?.label || "Queued");
    },
    itemStatusStyle(state) {
      const s = ITEM_STATUS[state || "QUEUED"] || ITEM_STATUS.QUEUED;
      return { background: s.bg, color: s.fg, border: `1px solid ${s.bd}` };
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
        this.actionError = kitchenErrorMessage(error, t("Could not confirm {0}. Please try again.", kot.name));
      } finally {
        delete this.confirming[kot.name];
        this.masonryLoading();
      }
    },
    // Serve commits after a five-second Undo window. A failed serve stays on the board with a visible error.
    serveOrder(kot) {
      if (this.pendingServes[kot.name]) return;
      this.actionError = "";
      kot.isRotated = false;
      this.pendingServes[kot.name] = { timer: null, sending: false };
      const pending = this.pendingServes[kot.name];
      pending.timer = setTimeout(async () => {
        pending.sending = true;
        try {
          await this.serveItemExecutions(kot);
          await this.call.post("ury.ury.api.ury_kot_display.serve_kot", { name: kot.name });
          this.kot = this.kot.filter(card => card.name !== kot.name);
          this.removeAllItemsFromLocalStorage(kot);
          if (this.showRecentServed) await this.fetchRecentServed();
        } catch (error) {
          console.error("Failed to serve KOT", error);
          this.actionError = kitchenErrorMessage(error, t("Could not serve {0}. Please try again.", kot.name));
        } finally {
          delete this.pendingServes[kot.name];
          this.masonryLoading();
        }
      }, 5000);
      this.masonryLoading();
    },
    // Drive every KOT item through the execution-service lifecycle (READY, then SERVED) so the recipe
    // snapshot / stock-authority trigger fires. Both transitions are idempotent server-side.
    async serveItemExecutions(kot) {
      const genIdempotencyKey = () => {
        if (window.crypto && window.crypto.randomUUID) {
          return window.crypto.randomUUID();
        }
        // Fallback for non-secure contexts where crypto.randomUUID is unavailable.
        return `${kot.name}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      };
      for (const kotitem of kot.kot_items || []) {
        await this.call.post("ury.ury.api.ury_kot_item_execution_service.mark_item_ready", {
          kot_item: kotitem.name,
          idempotency_key: genIdempotencyKey(),
        });
        await this.call.post("ury.ury.api.ury_kot_item_execution_service.serve_item_execution", {
          kot_item: kotitem.name,
          idempotency_key: genIdempotencyKey(),
        });
      }
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
        const query = recentServedQuery(
          this.branch,
          this.isExpo ? null : this.production,
          now,
          this.recallWindowMinutes,
        );
        const result = await this.call.get("frappe.client.get_list", query);
        this.recentServed = result.message.filter(kot => canRecallKot(kot, now, this.recallWindowMinutes));
      } catch (error) {
        this.actionError = kitchenErrorMessage(error, t("Could not load recently served tickets. Please try again."));
      } finally {
        this.loadingServed = false;
      }
    },
    async expoBumpOrder(order) {
      if (!order?.invoice || this.bumpingExpo[order.invoice]) return;
      this.bumpingExpo[order.invoice] = true;
      this.actionError = "";
      try {
        await this.call.post("ury.ury.api.ury_kot_display.expo_bump_order", {
          invoice: order.invoice,
          production_unit: this.production,
        });
        this.kot = this.kot.filter(card => card.invoice !== order.invoice);
        if (this.showRecentServed) await this.fetchRecentServed();
      } catch (error) {
        this.actionError = kitchenErrorMessage(error, t("Could not bump order {0}. Please try again.", order.invoice));
      } finally {
        delete this.bumpingExpo[order.invoice];
      }
    },
    expoStationChipStyle(status) {
      if (status === "Ready") return { background: "#EAF2FB", color: "#1D5FA8", borderColor: "#C3D9F0" };
      if (status === "Served") return { background: "var(--gr-t)", color: "var(--gr)", borderColor: "var(--gr-b)" };
      if (status === "Preparing") return { background: "var(--am-t)", color: "var(--am)", borderColor: "var(--am-b)" };
      return { background: "var(--rd-t)", color: "var(--rd)", borderColor: "var(--rd-b)" };
    },
    groupedKotItems(kot) {
      const sorted = [...(kot?.kot_items || [])].sort(
        (a, b) => (Number(a?.serve_priority) || 0) - (Number(b?.serve_priority) || 0),
      );
      const groups = [];
      const byKey = new Map();
      for (const item of sorted) {
        const course = String(item?.course || "").trim();
        const prio = Number(item?.serve_priority) || 0;
        const key = `${prio}::${course}`;
        if (!byKey.has(key)) {
          const entry = { course, serve_priority: prio, items: [] };
          byKey.set(key, entry);
          groups.push(entry);
        }
        byKey.get(key).items.push(item);
      }
      return groups;
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
        this.actionError = kitchenErrorMessage(error, t("Could not recall {0}. Please try again.", kot.name));
      } finally {
        delete this.recalling[kot.name];
      }
    },

    resolveDisposition(kot, kotitem, disposition) {
      this.call
        .post("ury.ury.api.ury_kot_cancellation_service.resolve_cancellation_disposition", {
          kot: kot.name,
          item_row_name: kotitem.name,
          disposition: disposition,
          qty: kotitem.cancelled_qty,
        })
        .then((result) => {
          kotitem.disposition = disposition === "return_to_stock" ? "Returned to Stock" : "Wasted";
          this.masonryLoading();
        })
        .catch((error) => {
          console.error(error);
          alert(clientMessage(error?.message || t("Failed to resolve item disposition.")));
        });
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
        kot.color = "bg-[var(--am-t)] border-[var(--am-b)]";
      } else if (type == "Partially cancelled" || type == "Cancelled") {
        kot.color = "bg-[var(--rd-t)] border-[var(--rd-b)]";
      } else if (restaurant_table === undefined || table_takeaway == 1) {
        kot.color = "bg-[var(--ac-t)] border-[var(--ac-b)]";
      } else {
        kot.color = "bg-[var(--panel)]";
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
      // quantity / cancelled_qty are Float fields on URY KOT Items (older
      // payloads may still carry strings) and cancelled_qty is null/absent on rows that never recorded it, so coerce
      // explicitly instead of relying on implicit JS arithmetic (-> NaN).
      const toNum = (v) => {
        const n = parseFloat(v);
        return Number.isFinite(n) ? n : 0;
      };
      const total = toNum(qty);
      kotitem.qty = total;
      if (type == "Partially cancelled" || type == "Cancelled") {
        // Round to the 9 decimal places the Float column stores, which
        // removes binary subtraction residue (0.3 - 0.1) without cutting data.
        kotitem.qty = Math.round((total - toNum(cancelled_qty)) * 1e9) / 1e9;
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
      this.kot.forEach((kot) => {
        kot.timeRemaining = this.calculateTimeRemaining(kot.time);
        const [hours, minutes] = kot.timeRemaining.split(":").map(part => parseInt(part, 10) || 0);
        const totalMinutes = hours * 60 + minutes;

        if (
          totalMinutes === this.kot_alert_time &&
          kot.type !== "Cancelled" &&
          kot.type !== "Partially cancelled"
        ) {
          this.orderDelayNotify(kot);
        }
        kot.late = isKotLate(totalMinutes, this.kot_alert_time);
        kot.timecolor = kot.late ? "text-[var(--rd)]" : "text-[var(--t1)]";
      });
    },
    // `targetTime` is the KOT's creation time ("HH:MM:SS") on the site clock, always in the past.
    // Returns how long ago that was as "H : M", measured on the site clock (kot_list server_time),
    // not the browser clock, so a kitchen tablet in another timezone or with clock drift agrees.
    calculateTimeRemaining(targetTime) {
      if (!targetTime) return "0 : 0";
      const currentTime = new Date(Date.now() + this.serverTimeOffset);
      const [targetHours, targetMinutes, targetSeconds] = String(targetTime)
        .split(".")[0]
        .split(":")
        .map((num) => Number(num) || 0);
      const targetDate = new Date(
        currentTime.getFullYear(),
        currentTime.getMonth(),
        currentTime.getDate(),
        targetHours,
        targetMinutes,
        targetSeconds
      );
      // More than 12 hours "in the future" means the KOT was created yesterday (just before midnight).
      if (targetDate.getTime() - currentTime.getTime() > 12 * 60 * 60 * 1000) {
        targetDate.setDate(targetDate.getDate() - 1);
      }
      // Clamp at 0 so a not-yet-overdue KOT never renders a negative value.
      const elapsedMs = Math.max(0, currentTime.getTime() - targetDate.getTime());
      const hoursElapsed = Math.floor(elapsedMs / 3600000);
      const minutesElapsed = Math.floor((elapsedMs % 3600000) / 60000);
      return `${hoursElapsed} : ${minutesElapsed}`;
    },
    // "18 min" below an hour, "1 h 05 min" above it.
    formatElapsed(value) {
      const [hours, minutes] = String(value || "0 : 0").split(":").map(part => Number(part) || 0);
      return hours ? `${hours} h ${String(minutes).padStart(2, "0")} min` : `${minutes} min`;
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
          isOriginLeft: !isRTL,

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
      this.setStatusMessage(t("You are online"));
      this.hideStatusMessageAfterDelay();
      this.fetchKOT().then(() => {
        this.masonryLoading();
      });
    },
    handleOffline() {
      this.isOnline = false;
      this.setStatusMessage(t("You are Offline"));
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
    window.addEventListener("resize", this.masonryLoading);
    this.masonryLoading();

    this.auth()
      .then(() => {
        this.fetchKOT().then(() => {
          // Leaving the station during authentication or the first snapshot installs no listeners.
          if (!this.socketActive) return;
          this.startServiceRequests();
          this.startStationHeartbeat();
          this.socketStale = !socket?.connected;
          if (this.audio_alert === 1) {
            this.showAudioAlertMessage = true;
          }
          this.socketHandlers = {
            // Resync on every reconnect; the board stays marked stale until that snapshot lands.
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

          // KOT error alerts (delayed orders)
          this.socketHandlers[this.kot_error_channel] = (doc) => {
            // Look up the matching KOT in the local array to get table/order info
            const matchingKot = this.kot.find(k => k.name === doc.kot);

            this.kotErrorAlert = {
              invoice: doc.invoice,
              tableortakeaway: matchingKot?.tableortakeaway || 'Table/Takeaway info unavailable',
              order_no: matchingKot?.order_no || 'N/A',
              guest_label: matchingKot?.custom_guest_label || '',
              timestamp: new Date().toLocaleTimeString()
            };
            this.showKotErrorAlert = true;
            // Auto-hide after 8 seconds
            setTimeout(() => {
              this.hideKotErrorAlert();
            }, 8000);
          };
          const handlePriorityUpdate = () => {
            this.fetchKOT()
              .then(() => {
                this.masonryLoading();
              })
              .catch(console.error);
          };
          if (this.kot_priority_channel) {
            this.socketHandlers[this.kot_priority_channel] = handlePriorityUpdate;
          }
          if (this.isExpo && this.kot_branch_priority_channel) {
            this.socketHandlers[this.kot_branch_priority_channel] = handlePriorityUpdate;
          }
          if (socket) {
            for (const [channel, handler] of Object.entries(this.socketHandlers)) socket.on(channel, handler);
          }
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
    if (socket) {
      for (const [channel, handler] of Object.entries(this.socketHandlers)) socket.off(channel, handler);
    }
    this.stopServiceRequests();
    this.cancelPendingServes();
    clearInterval(this.timeInterval);
    this.stopStationHeartbeat();
    window.removeEventListener("resize", this.masonryLoading);
    window.removeEventListener("online", this.handleOnline);
    window.removeEventListener("offline", this.handleOffline);
    document.removeEventListener("click", this.hideAudioAlertMessage);
  },
  computed: {
    availabilitySocket() {
      return this.availabilityBranch ? socket : undefined;
    },
    visibleKots() {
      const filtered = (this.kot || []).filter(k => !k.showDiv && k.production === this.production);
      return [...filtered].sort((a, b) => {
        const aRush = Number(a?.custom_is_rush) === 1 ? 0 : 1;
        const bRush = Number(b?.custom_is_rush) === 1 ? 0 : 1;
        if (aRush !== bRush) return aRush - bRush;
        const aVip = Number(a?.custom_is_vip) === 1 ? 0 : 1;
        const bVip = Number(b?.custom_is_vip) === 1 ? 0 : 1;
        return aVip - bVip;
      });
    },
    allDaySummary() {
      const source = this.isExpo ? (this.kot || []).filter(k => !k.showDiv) : this.visibleKots;
      const totals = new Map();
      for (const kot of source) {
        if (kot?.type === "Cancelled" || kot?.type === "Partially cancelled") continue;
        const isRush = Number(kot?.custom_is_rush) === 1;
        const isVip = Number(kot?.custom_is_vip) === 1;
        for (const item of kot?.kot_items || []) {
          if (item?.state === "SERVED") continue;
          const rawQty = item?.qty !== undefined ? Number(item.qty) : Number(item?.quantity) - Number(item?.cancelled_qty || 0);
          if (!Number.isFinite(rawQty) || rawQty <= 0) continue;
          const itemName = String(item?.item_name || item?.item || "").trim();
          if (!itemName) continue;
          const course = String(item?.course || "").trim();
          const key = `${itemName}::${course}`;
          if (!totals.has(key)) {
            totals.set(key, { item_name: itemName, course, total_qty: 0, rush_qty: 0, vip_qty: 0 });
          }
          const entry = totals.get(key);
          entry.total_qty = Math.round((entry.total_qty + rawQty) * 1e4) / 1e4;
          if (isRush) entry.rush_qty = Math.round((entry.rush_qty + rawQty) * 1e4) / 1e4;
          if (isVip) entry.vip_qty = Math.round((entry.vip_qty + rawQty) * 1e4) / 1e4;
        }
      }
      return [...totals.values()].sort(
        (a, b) => (b.rush_qty - a.rush_qty) || (b.total_qty - a.total_qty) || a.item_name.localeCompare(b.item_name),
      );
    },
    expoOrders() {
      if (!this.isExpo) return [];
      const byInvoice = new Map();
      for (const kot of this.kot || []) {
        if (kot?.showDiv || kot?.type === "Cancelled" || kot?.type === "Partially cancelled") continue;
        const invoice = kot?.invoice || kot?.name;
        if (!invoice) continue;
        if (!byInvoice.has(invoice)) {
          byInvoice.set(invoice, {
            invoice,
            restaurant_table: kot.restaurant_table,
            table_takeaway: kot.table_takeaway,
            tableortakeaway: kot.tableortakeaway,
            custom_guest_label: kot.custom_guest_label,
            order_no: kot.order_no,
            user: kot.user,
            late: Boolean(kot.late),
            custom_is_rush: 0,
            custom_is_vip: 0,
            stations: [],
          });
        }
        const order = byInvoice.get(invoice);
        if (Number(kot.custom_is_rush) === 1) order.custom_is_rush = 1;
        if (Number(kot.custom_is_vip) === 1) order.custom_is_vip = 1;
        if (kot.late) order.late = true;
        const items = kot.kot_items || [];
        const states = items.map(it => String(it?.state || "QUEUED").toUpperCase());
        const readyCount = states.filter(st => st === "READY" || st === "SERVED").length;
        let status = "Queued";
        let allItemsReady = false;
        if (kot.order_status === "Served") {
          status = "Served";
          allItemsReady = true;
        } else if (!items.length || readyCount === items.length) {
          status = "Ready";
          allItemsReady = true;
        } else if (states.some(st => st === "IN_PREPARATION" || st === "READY" || st === "SERVED")) {
          status = "Preparing";
        }
        order.stations.push({
          kot_name: kot.name,
          production: kot.production || "",
          status,
          all_items_ready: allItemsReady,
          course_groups: this.groupedKotItems(kot),
        });
      }
      return [...byInvoice.values()]
        .map(order => ({
          ...order,
          all_stations_ready: order.stations.length > 0 && order.stations.every(st => st.all_items_ready),
        }))
        .sort((a, b) => {
          const aRush = Number(a.custom_is_rush) === 1 ? 0 : 1;
          const bRush = Number(b.custom_is_rush) === 1 ? 0 : 1;
          if (aRush !== bRush) return aRush - bRush;
          const aVip = Number(a.custom_is_vip) === 1 ? 0 : 1;
          const bVip = Number(b.custom_is_vip) === 1 ? 0 : 1;
          return aVip - bVip;
        });
    },
    sortedKotItems() {
      return (kot) => {
        return [...(kot.kot_items || [])].sort((a, b) => (a.serve_priority || 0) - (b.serve_priority || 0));
      };
    },
  },
};
</script>
<style>
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
/* Notes keep at least 7:1 contrast on the light ticket (kot-review.test.js). */
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
[dir="rtl"] .kot-note {
  border-left: 0;
  border-right: 4px solid #b45309;
}
.kot-note-label {
  display: inline;
  margin-inline-end: 4px;
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
