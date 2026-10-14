import { defineStore } from "pinia";
import { useTableStore } from "./Table.js";
import { useMenuStore } from "./Menu.js";
import { useInvoiceDataStore } from "./invoiceData.js";
import frappe from "./frappeSdk.js";

import axios from "axios";
import { useAlert } from "./Alert.js";
import router from "../router";
import { disconnectQzPrinter } from "./utils/PrintWithQz";
import { logoutSession } from "../../../packages/core/src/logout.ts";
import { clearAppStorage } from "../../../packages/core/src/storage.ts";
import { watchIdle } from "../../../packages/core/src/idle.ts";

axios.defaults.baseURL = frappe.url;

const RECENT_WAITERS_KEY = "ury_recent_waiters";

function readRecentWaiters() {
  try {
    const stored = JSON.parse(localStorage.getItem(RECENT_WAITERS_KEY) || "[]");
    return Array.isArray(stored)
      ? stored.filter((waiter) => waiter && typeof waiter.username === "string" && typeof waiter.displayName === "string")
        .map(({ username, displayName }) => ({ username, displayName })).slice(0, 6)
      : [];
  } catch (_error) {
    return [];
  }
}

export const useAuthStore = defineStore("auth", {
  state: () => ({
    userId: "",
    userName: "",
    sessionUser: "",
    currentPassword: "",
    userRole: [],
    isPosOpen: true,
    hasAccess: false,
    showPassword: false,
    activeDropdown: false,
    loggingOut: false,
    cashierisPosOpen: false,
    cashier: null,
    staySignedIn: null,
    viewItemImage: null,
    viewAllStatus: null,
    restrictTableOrder: null,
    removeTableOrderItem: null,
    db: frappe.db(),
    call: frappe.call(),
    auth: frappe.auth(),
    alert: useAlert(),
    menu: useMenuStore(),
    table: useTableStore(),
    invoiceData: useInvoiceDataStore(),
    userAuth: localStorage.getItem("userAuth"),
    idleStop: null,
    recentWaiters: readRecentWaiters(),
  }),
  getters: {
    isAuthenticated(state) {
      state.isAuthenticated;
    },
    passwordFieldType() {
      return this.showPassword ? "text" : "password";
    },
  },
  actions: {
    //Login
    async login() {
      try {
        const response = await this.auth.loginWithUsernamePassword({
          username: this.userId,
          password: this.currentPassword,
        });
        this.rememberRecentWaiter(
          this.userId,
          response.full_name || this.userId
        );
        this.userAuth = true;
        localStorage.setItem("userAuth", "true");
        this.currentPassword = "";
        await this.fetchUserDetails();
      } catch (error) {
        this.alert.createAlert("Message", error.message, "OK");
      } finally {
        this.currentPassword = "";
      }
    },
    rememberRecentWaiter(username, displayName) {
      if (!username) return;
      const waiters = readRecentWaiters().filter((waiter) => waiter.username !== username);
      waiters.unshift({ username, displayName: displayName || username });
      this.recentWaiters = waiters.slice(0, 6);
      localStorage.setItem(RECENT_WAITERS_KEY, JSON.stringify(this.recentWaiters));
    },
    selectRecentWaiter(waiter) {
      this.userId = waiter.username;
      this.currentPassword = "";
    },
    checkAuthState() {
      if (this.userAuth && localStorage.getItem("userAuth")) {
        this.userAuth = true;
      }
    },
    getLoginAvatar() {
      const atIndex = this.sessionUser.indexOf("@");
      if (atIndex > -1) {
        this.userName = this.sessionUser.substring(0, atIndex);
      } else {
        this.userName = "";
      }
      return this.userName;
    },
    fetchUserDetails() {
      const hasLocalAuth = localStorage.getItem("userAuth") === "true";
      return this.auth
        .getLoggedInUser()
        .then(async (user) => {
          this.sessionUser = user;
          if (!this.sessionUser) {
            this.userAuth = false;
            localStorage.removeItem("userAuth");
            return;
          }

          await this.invoiceData.fetchInvoiceDetails();
          if (!hasLocalAuth) {
            await this.fetchUserRole();
            // A waiter's surviving cookie must still end at the PIN pad.
            if (this.staySignedIn !== true) {
              await this.logOut();
              return;
            }
          }

          this.userAuth = true;
          localStorage.setItem("userAuth", "true");
          router.push("/Table");
          this.table.fetchRoom();
          if (hasLocalAuth) this.fetchUserRole();
        })
        .catch((error) => {
          this.userAuth = false;
          localStorage.removeItem("userAuth", "true");
          router.push("/login");
        });
    },
    fetchUserRole() {
      //Fetching role based on logged user
      this.stopIdle();
      this.staySignedIn = null;
      return this.db
        .getDoc("User", this.sessionUser)
        .then((doc) => {
          this.userRole = doc.roles.map((item) => item.role);
          const getPosProfile = {
            doctype: "POS Profile",
            name: this.invoiceData.posProfile,
          };
          return this.call
            .get("frappe.client.get", getPosProfile)
            .then((result) => {
              var billingRoles = result.message.role_allowed_for_billing.map(
                (role) => role.role
              );
              this.cashier = billingRoles.some((role) =>
                this.userRole.includes(role)
              );
              const stayRoles = (result.message.custom_floor_stay_signed_in_roles || []).map(
                (role) => role.role
              );
              this.staySignedIn = stayRoles.some((role) =>
                this.userRole.includes(role)
              );
              if (this.cashier) {
                this.menu.pickOrderType();
                // this.menu.fetchItems();
              }
              this.isPosOpenChecking();
              this.isPosCloseCheck();
              var transferRoles = result.message.transfer_role_permissions.map(
                (role) => role.role
              );
              this.hasAccess = transferRoles.some((role) =>
                this.userRole.includes(role)
              );
              var restrictOrder =
                result.message.role_restricted_for_table_order.map(
                  (role) => role.role
                );
              this.restrictTableOrder = restrictOrder.some((role) =>
                this.userRole.includes(role)
              );
              this.viewAllStatus = result.message.view_all_status;
              this.removeTableOrderItem = result.message.remove_items;
              this.viewItemImage = result.message.show_image;
              const idleSeconds = Number(
                result.message.custom_floor_idle_signout_seconds ??
                0
              );
              if (this.staySignedIn === false && idleSeconds > 0) {
                this.idleStop = watchIdle(() => this.logOut(), idleSeconds * 1000);
              }
            })
            .catch((error) => console.error(error));
        })
        .catch((error) => {
          console.error(error);
        });
    },
    routeToHome() {
      var currentDomain = window.location.protocol + "//" + window.location.hostname;
      window.location.href = currentDomain + "/app/";
    },

    isPosOpenChecking() {
      if (this.invoiceData.multipleCashier) {
        this.call
          .get("ury.ury.doctype.ury_order.ury_order.pos_opening_check")
          .then((result) => {
            var currentDomain = window.location.origin;
            if (!result.message.opening_exists) {
              this.alert.createAlert("Message", "POS Opening Entry is not created", "OK").then(() => {
                window.location.href = currentDomain + "/app/";
              });
            }

          })
          .catch((error) => {
            var currentDomain = window.location.origin;
            const serverMessages = JSON.parse(error._server_messages);
            const innerMessageString = serverMessages[0];
            const innerMessage = JSON.parse(innerMessageString);
            const message = innerMessage.message;
            this.alert.createAlert("Message", message, "OK").then(() => {
              window.location.href = currentDomain + "/app/";
            });

          });
      } 
      else {
        this.call
          .get("ury.ury_pos.api.posOpening")
          .then((result) => {
            const serverMessages = JSON.parse(result._server_messages);
            const innerMessageString = serverMessages[0];
            const innerMessage = JSON.parse(innerMessageString);
            const message = innerMessage.message;
            // if (this.cashier) {
            //   this.alert.createAlert("Message", message, "OK").then(() => {
            //     router.push("/posOpen");
            //   });
            // } else {
            var currentDomain = window.location.origin;
            this.alert.createAlert("Message", message, "OK").then(() => {
              window.location.href = currentDomain + "/app/";
            });
            // }
          })
          .catch((error) => {
            // console.error(error)
          });
      }
    },
    isPosCloseCheck() {
      const getPosProfile = {
        pos_profile: this.invoiceData.posProfile,
      };
      this.call
        .get("ury.ury_pos.api.validate_pos_close", getPosProfile)
        .then((result) => {
          if (result.message === "Failed") {
            var currentDomain = window.location.origin;
            this.alert
              .createAlert("Message", "Please close previous POS Entry", "OK")
              .then(() => {
                window.location.href = currentDomain + "/app/";
              });
          }
        })
        .catch((error) => {
          console.error(error);
        });
    },
    toggleDropdown() {
      if (this.activeDropdown) {
        this.hideDropdown();
      } else {
        this.activeDropdown = true;
      }
    },
    hideDropdown() {
      this.activeDropdown = false;
    },

    stopIdle() {
      if (this.idleStop) {
        this.idleStop();
        this.idleStop = null;
      }
    },

    switchWaiter() {
      return this.logOut();
    },

    async logOut() {
      if (this.loggingOut) return;
      this.loggingOut = true;
      this.stopIdle();
      try {
        await logoutSession(window.csrf_token);
      } catch (_error) {
        // The server session may survive, but the shared tablet must lock locally.
      } finally {
        await clearAppStorage();
        this.invoiceData.clearDataAfterUpdate();
        this.userAuth = false;
        this.currentPassword = "";
        disconnectQzPrinter();
        window.location.href = "/urypos/login";
      }
    },
  },
});
