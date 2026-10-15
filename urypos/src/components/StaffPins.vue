<template>
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="Staff PINs">
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
      <h2 class="mb-4 text-2xl font-semibold">Staff PINs</h2>
      <div v-if="pin" class="space-y-6 text-center">
        <p>Give this PIN privately to {{ issuedFor }}. It is shown only once.</p>
        <p class="text-5xl font-semibold tracking-widest">{{ pin }}</p>
        <button type="button" class="min-h-[44px] w-full rounded-md bg-blue-600 p-3 text-white" @click="pin = ''; issuedFor = ''">Done</button>
      </div>
      <div v-else class="space-y-3">
        <div v-for="staff in users" :key="staff.user" class="flex items-center justify-between gap-4 border-b py-3">
          <span class="break-all">{{ staff.user }}</span>
          <button type="button" :disabled="busy" class="min-h-[44px] shrink-0 rounded-md border px-3 text-blue-700 disabled:opacity-50" @click="issue(staff.user)">Issue new PIN</button>
        </div>
        <p v-if="error" role="alert" class="text-sm text-red-700">{{ error }}</p>
        <button type="button" class="min-h-[44px] w-full rounded-md border p-3" @click="$emit('close')">Close</button>
      </div>
    </div>
  </div>
</template>
<script>
import { useAuthStore } from '@/stores/Auth.js';
import { useInvoiceDataStore } from '@/stores/invoiceData.js';
export default {
  emits: ['close'],
  setup() { return { auth: useAuthStore(), invoice: useInvoiceDataStore() }; },
  data() { return { users: [], pin: '', issuedFor: '', error: '', busy: false }; },
  async mounted() {
    try {
      const { message: profile } = await this.auth.call.get('frappe.client.get', { doctype: 'POS Profile', name: this.invoice.posProfile });
      const { message: branch } = await this.auth.call.get('frappe.client.get', { doctype: 'Branch', name: profile.branch });
      this.users = branch.user || [];
    } catch (_error) { this.error = 'Staff could not be loaded.'; }
  },
  beforeUnmount() { this.pin = ''; },
  methods: {
    async issue(user) {
      this.busy = true; this.error = '';
      try {
        const { message } = await this.auth.call.post('ury.ury_pos.floor_pin.issue_pin', { user });
        this.issuedFor = user; this.pin = message.pin;
      } catch (_error) { this.error = 'A PIN could not be issued. Check that this person is eligible for floor PIN sign-in.'; }
      finally { this.busy = false; }
    },
  },
};
</script>
