import { StateCreator } from 'zustand';
import { readLoggedUser, getSessionUser, getUserRoles } from '@ury/core';
import { offlineShellActive } from '../../pwa/register-offline';
import { getFloorPad } from '../../lib/floor-session';

export interface User {
  name: string; // This stores the user ID
  roles: string[];
  full_name?: string;
  // Kept from the cookie because the server could not be reached on an
  // opted-in till. Revalidated on the next 'online' event.
  provisional?: boolean;
}

export interface AuthState {
  user: User | null;
  isLoading: boolean;
  error: string | null;
}

export interface AuthActions {
  checkAuth: () => Promise<void>;
  setUser: (user: User | null) => void;
  clearAuth: () => void;
}

export type AuthSlice = AuthState & AuthActions;

const initialState: AuthState = {
  user: null,
  isLoading: false,
  error: null,
};

export const createAuthSlice: StateCreator<AuthSlice> = (set, get) => ({
  ...initialState,

  checkAuth: async () => {
    try {
      set({ isLoading: true, error: null });
      const { user: serverUser, networkFailure } = await readLoggedUser();
      let userName = serverUser;
      let provisional = false;
      // No application answer (transport failure or 502/503/504/52x) on a till
      // whose /pos worker is active may keep the cookie session. 401/403 always
      // go to login.
      if (!userName && networkFailure && offlineShellActive()) {
        const cookieUser = getSessionUser();
        if (cookieUser && cookieUser !== 'Guest') {
          userName = cookieUser;
          provisional = true;
        }
      }

      if (!userName) {
        set({ user: null, isLoading: false });
        const floorPad = getFloorPad();
        if (floorPad.paired && floorPad.till_open) return;
        // If no user is logged in, redirect to login
        window.location.href = '/login?redirect-to=%2Fpos';
        return;
      }

      // Get user roles
      const roles = await getUserRoles(userName);

      set({
        user: {
          name: userName, // Store the user ID in name field
          full_name: roles.full_name,
          roles: roles.roles,
          ...(provisional ? { provisional: true } : {}),
        },
        isLoading: false,
      });
      if (provisional) {
        window.addEventListener('online', () => {
          void get().checkAuth();
        }, { once: true });
      }
    } catch (error) {
      set({ 
        error: (error as Error).message,
        isLoading: false,
        user: null,
      });
      // Redirect to login on error
      window.location.href = '/login?redirect-to=%2Fapp';
    }
  },

  setUser: (user) => {
    set({ user });
  },

  clearAuth: () => {
    set(initialState);
  },
});
