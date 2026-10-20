<template>
  <div class="flex flex-col items-center justify-center">
    <div class="mt-20 w-full max-w-md rounded-lg bg-white px-6 py-8 shadow-md">
      <div class="mb-8 flex justify-center">
        <img :src="brand.logo" :alt="brand.name" class="h-8 w-auto" />
      </div>
      <div v-if="showPad" class="space-y-6 text-center">
        <h1 class="text-2xl font-semibold text-gray-900">Staff sign-in</h1>
        <p class="text-gray-600">Enter your 4-digit PIN</p>
        <div aria-label="4-digit PIN" class="h-12 text-4xl tracking-widest text-gray-900">{{ '●'.repeat(pin.length) }}</div>
        <div class="grid grid-cols-3 gap-3">
          <button v-for="digit in ['1','2','3','4','5','6','7','8','9']" :key="digit" type="button"
            :disabled="busy" class="min-h-[64px] rounded-lg border border-gray-200 text-2xl font-medium hover:bg-gray-50 disabled:opacity-50" @click="press(digit)">{{ digit }}</button>
          <button type="button" :disabled="busy" class="min-h-[64px] rounded-lg text-gray-600" @click="pin = ''">Clear</button>
          <button type="button" :disabled="busy" class="min-h-[64px] rounded-lg border border-gray-200 text-2xl font-medium hover:bg-gray-50" @click="press('0')">0</button>
          <button type="button" :disabled="busy" aria-label="Delete digit" class="min-h-[64px] rounded-lg text-gray-600" @click="pin = pin.slice(0, -1)">⌫</button>
        </div>
        <p v-if="error" role="alert" class="text-sm text-red-700">{{ error }}</p>
        <button type="button" class="min-h-[44px] text-sm text-blue-600" @click="usernameForm = true; pin = ''">Sign in with username</button>
      </div>
      <form v-else class="space-y-6" @submit.prevent="auth.login">
        <div>
          <label for="user_id" class="mb-2 block text-sm font-medium text-gray-700">Username</label>
          <input id="user_id" v-model="auth.userId" name="user_id" required autocomplete="username"
            class="block w-full rounded-md border border-gray-200 px-3 py-3 text-sm" placeholder="Your username" />
        </div>
        <div>
          <label for="currentPassword" class="mb-2 block text-sm font-medium text-gray-700">Password</label>
          <div class="relative">
            <input id="currentPassword" v-model="auth.currentPassword" :type="auth.showPassword ? 'text' : 'password'"
              name="currentPassword" required autocomplete="current-password"
              class="block w-full rounded-md border border-gray-200 py-3 pl-3 pr-16 text-sm" placeholder="Your password" />
            <button type="button" class="absolute inset-y-0 right-0 px-3 text-sm text-gray-500" @click="auth.showPassword = !auth.showPassword">{{ auth.showPassword ? 'Hide' : 'Show' }}</button>
          </div>
        </div>
        <button type="submit" class="flex w-full justify-center rounded-md bg-blue-600 px-4 py-3 text-sm font-medium text-white hover:bg-blue-700">Login</button>
        <a v-if="!floorPad.paired" href="/login#forgot" class="block text-center text-sm text-blue-600">Forgot password?</a>
      </form>
    </div>
  </div>
</template>

<script>
import { useAuthStore } from "@/stores/Auth.js";
import uriPosImage from "@/assets/logos/URY_POS.jpg";
import { resolveBrand } from "../../../packages/core/src/brand.ts";

export default {
  setup() { return { auth: useAuthStore() }; },
  data() {
    return { brand: resolveBrand({ name: "URY POS logo", logo: uriPosImage }),
      floorPad: window.floor_pad || {}, usernameForm: false, pin: '', busy: false, error: '' };
  },
  computed: {
    showPad() { return this.floorPad.paired && this.floorPad.till_open && !this.usernameForm; },
  },
  methods: {
    async press(digit) {
      if (this.busy) return;
      this.error = '';
      this.pin += digit;
      if (this.pin.length !== 4) return;
      this.busy = true;
      try { await this.auth.pinSignIn(this.pin); }
      catch (_error) { this.error = "That PIN didn't work. Ask a manager for a new PIN."; }
      finally { this.pin = ''; this.busy = false; }
    },
  },
};
</script>
