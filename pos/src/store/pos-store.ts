import { create } from 'zustand';
import { v4 as uuidv4 } from 'uuid';
import { ownedSessionKey, getSessionUser, isLinkDown, storage, siteCurrencyCode, currencyDisplaySymbol } from '@ury/core';
// Subpath import: keeps this pure helper independent of tests that mock the '@ury/core' barrel.
import { resolveDefaultCustomer, profileLacksDefaultCustomerConfig } from '@ury/core/default-customer';
import * as menuApi from '../lib/menu-api';
import type {
  ActiveServingPeriod,
  TaxSummary,
  MenuItem as APIMenuItem,
  BundleOverride,
  SelectedModifier,
} from '../lib/menu-api';
import { getCurrencyInfo, PosProfileCombined, getCombinedPosProfile, getPosProfileOrSelection } from '../lib/pos-profile-api';
import { getAvailableOutlets, setActiveOutlet, type AvailableOutlet } from '../lib/outlet-api';
import { getMenuCourses } from '../lib/menu-course-api';
import { t } from '../i18n';
import { getCustomerGroups, getCustomerTerritories } from '../lib/customer-api';
import { DEFAULT_ORDER_TYPE, OrderType } from '../data/order-types';
import { getTableOrder, TableOrder } from '../lib/order-api';
import { getPaymentModes, getPaymentModeDetails, type PaymentModeConfig } from '../lib/payment-api';
import { beginOpenTableRead, openTableReadCurrent, rememberOpenTableCheck, forgetOpenTableCheck } from '../offline/open-table-checks';
import { offlineCaptureEnabled } from '../pwa/runtime-caching';
import { bindOfflineUser, cachedOfflineProfile, syncOfflineCapture } from '../pwa/register-offline';

// Constants
const MAX_QUANTITY = 99;
const MIN_QUANTITY = 0;

// Custom error class for cart operations
class CartError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CartError';
  }
}

// Extend the API MenuItem to include UI-specific properties
export interface MenuItem extends Omit<APIMenuItem, 'rate' | 'item_image'> {
  id: string;
  name: string;
  image: string | null;
  price: number;
  quantity?: number;
  description?: string;
  special_dish?: 1 | 0;
  category?: string;
  variants?: Array<{ id: string; name: string; price: number }>;
  addons?: Array<{ id: string; name: string; price: number; category: 'sides' | 'drinks' | 'desserts' }>;
  selectedVariant?: { id: string; name: string; price: number };
  selectedAddons?: Array<{ id: string; name: string; price: number }>;
  uniqueId?: string;
  tax_rate?: number;
  unavailable_daypart?: boolean;
  daypart_label?: string | null;
  promo_rate?: number | null;
  promo_label?: string | null;
}

export interface Customer {
  id: string;
  name: string;
  phone: string;
}

/**
 * Where the current customer came from:
 *  - 'auto'   preselected from the POS Profile default for the order type; follows order-type changes
 *  - 'manual' picked or cleared by the user; never overwritten by order-type changes
 *  - 'order'  loaded from an existing order; never overwritten
 */
export type CustomerSource = 'auto' | 'manual' | 'order';

export interface OrderItem extends MenuItem {
  quantity: number;
  selectedVariant?: { id: string; name: string; price: number };
  selectedAddons?: { id: string; name: string; price: number }[];
  uniqueId?: string;
  comment?: string;
  /**
   * Stable server-side line identity for this cart line.
   *
   * For a line loaded from an existing order this is the value persisted on
   * the invoice row (`POS Invoice Item-reservation_line_key`); for a line
   * added in this session it is the locally generated `uniqueId`, which the
   * server then persists. Either way the SAME string is sent back on every
   * subsequent `sync_order`, which is what lets the backend see a quantity
   * change as a delta on an existing line rather than as a remove + add
   * (B02b). Never derive it from the invoice child row's `name`.
   */
  reservationLineKey?: string;
  /**
   * The real POS Invoice Item child-table row `name` for a line loaded from
   * an already-saved/printed invoice (via `loadTableOrder`) — undefined for
   * a line only staged locally and not yet synced to the server. This is
   * the authoritative selector `reduce_order_item_qty` matches on for an
   * IMMEDIATE reduce-and-notify action taken right after a fresh load;
   * unlike `reservationLineKey`, it is NOT safe to carry across an
   * intervening `sync_order` save (the child row is regenerated then) — only
   * use it against data just returned by `loadTableOrder`/`getTableOrder`.
   * `id`/`uniqueId` are locally-derived keys (item_code + variant/addons)
   * and must never be sent to that API as the row selector.
   */
  invoiceItemName?: string;
  ury_held?: number;
  fireAt?: string | null;
  fireConfirmationPending?: boolean;
  parentLineKey?: string | null;
  lineRole?: 'Standard' | 'Modifier' | 'Combo Header' | 'Combo Upcharge';
  isModifier?: boolean;
  modifierPrefix?: string | null;
  modifierGroup?: string | null;
  modifierOption?: string | null;
  selectedModifiers?: SelectedModifier[];
  bundleOverrides?: BundleOverride[];
  inheritParentRouting?: boolean;
  hideUnpricedOnReceipt?: boolean;
}

export interface PaymentMode {
  id: string;
  name: string;
  enabled: boolean;
}

export interface Category {
  name: string;
  label: string;
  icon?: string;
}

function categoriesFromItems(items: MenuItem[]): Category[] {
  const seen = new Map<string, string>();
  for (const item of items) {
    if (item.course && !seen.has(item.course)) seen.set(item.course, item.course_label || item.course);
  }
  return Array.from(seen, ([name, label]) => ({ name, label }));
}

function keepSelection(selected: string, categories: Category[]): string {
  return !selected || categories.some((c) => c.name === selected) ? selected : '';
}

// A server course list wins; a list derived from items follows each menu load.
function withMenuItems(
  state: { categories: Category[]; categoriesDerived: boolean; selectedCategory: string },
  menuItems: MenuItem[],
) {
  const categories = state.categories.length && !state.categoriesDerived
    ? state.categories : categoriesFromItems(menuItems);
  return {
    menuItems, categories, categoriesDerived: categories !== state.categories,
    selectedCategory: keepSelection(state.selectedCategory, categories),
  };
}

export interface Order {
  id: string;
  cartId: string;
  customerId?: string;
  paymentModeId: string;
  paymentMode: string;
  orderType: OrderType;
  status: 'pending' | 'paid' | 'preparing' | 'ready' | 'completed' | 'cancelled';
  totalAmount: number;
  paidAmount: number;
  createdAt: string;
  updatedAt: string;
}

interface CartTotals {
  subtotal: number;
  tax: number;
  total: number;
  itemCount: number;
}

interface Aggregator {
  customer: string;
}

interface POSState {
  menuItems: MenuItem[];
  categories: Category[];
  categoriesDerived: boolean;
  activeOrders: OrderItem[];
  selectedCategory: string;
  selectedTable: string | null;
  selectedRoom: string | null;
  searchQuery: string;
  selectedCustomer: Customer | null;
  customerSource: CustomerSource;
  selectedOrderType: OrderType;
  quickFilter: 'all' | 'special';
  selectedItem: MenuItem | null;
  cartId: string | null;
  loading: boolean;
  menuLoading: boolean;
  orderLoading: boolean;
  profileLoading: boolean;
  error: string | null;
  paymentModes: string[];
  paymentModeDetails: Record<string, PaymentModeConfig>;
  orders: Order[];
  selectedAggregator: Aggregator | null;
  currency: string;
  currencySymbol: string | null;
  editCustomer: { invoice: string; customer: string } | null;
  isUpdatingOrder: boolean;
  orderId: string | null;
  originalSentItems: Array<{
    invoice_item_name?: string;
    item_code: string;
    item_name: string;
    reservation_line_key: string;
    qty: number;
    rate: number;
    comment: string;
    ury_held?: number;
  }>;
  posProfile: PosProfileCombined | null;
  availableOutlets: AvailableOutlet[];
  requiresOutletSelection: boolean;
  customerGroups: string[];
  territories: string[];
  tableOrder: TableOrder | null;
  isInitializing: boolean;
  orderComment: string;
  /** Short name for the order or check (Susan, Seat 2); not a Customer. */
  guestLabel: string;
  noOfPax: number;
  lastModifiedTime: string | null;
  /**
   * Voluntarily-triggered POS Closing dialog (Header menu "Close Shift"),
   * distinct from POSOpeningProvider's own forced-closure blocking states.
   * Read by POSOpeningProvider's normal-render branch so the dialog can be
   * shown as an overlay without unmounting the app underneath it.
   */
  showVoluntaryClosing: boolean;
  activeServingPeriod: ActiveServingPeriod | null;
  activeTaxSummary: TaxSummary | null;
  activePriceList: string | null;
}

interface POSStore extends POSState {
  fetchMenuItems: () => Promise<void>;
  fetchAggregatorMenu: (aggregator: string) => Promise<void>;
  fetchCategories: () => Promise<void>;
  fetchPaymentModes: () => Promise<void>;
  addToOrder: (item: OrderItem) => Promise<void>;
  addConfiguredItems: (parent: OrderItem, children: OrderItem[], replaceParentUniqueId?: string) => Promise<void>;
  removeFromOrder: (uniqueId: string) => Promise<void>;
  updateQuantity: (uniqueId: string, quantity: number) => Promise<void>;
  clearOrder: () => Promise<void>;
  setSelectedCategory: (category: string) => void;
  setSearchQuery: (query: string) => void;
  setSelectedCustomer: (customer: Customer | null) => void;
  setSelectedTable: (table: string | null, room: string | null, doNotLoadOrder?: boolean) => void;
  setSelectedOrderType: (type: OrderType) => void;
  /** Preselect the profile's default customer for the current order type (new, non-manual orders only). */
  applyAutoCustomer: () => void;
  setQuickFilter: (filter: 'all' | 'special') => void;
  setSelectedItem: (item: MenuItem | null) => void;
  initializeCart: () => Promise<void>;
  processPayment: (paymentMode: string, amount: number) => Promise<void>;
  updateOrderStatus: (orderId: string, status: Order['status']) => Promise<void>;
  fetchPosProfile: () => Promise<void>;
  fetchAvailableOutlets: () => Promise<void>;
  switchOutlet: (posProfileName: string) => Promise<void>;
  fetchCustomerGroups: () => Promise<void>;
  fetchTerritories: () => Promise<void>;
  fetchCurrencySymbol: () => Promise<void>;
  getCartTotals: () => CartTotals;
  itemExistsInCart: (uniqueId: string) => boolean;
  validateQuantity: (quantity: number) => boolean;
  getItemPrice: (item: OrderItem) => number;
  getItemQuantityFromCart: (item: MenuItem) => number;
  loadTableOrder: (table: string) => Promise<void>;
  clearTableOrder: () => void;
  isMenuInteractionDisabled: () => boolean;
  isOrderInteractionDisabled: () => boolean;
  initializeApp: () => Promise<void>;
  setOrderForUpdate: (
    orderId: string | null,
    customer?: string,
    modified?: string,
    originalItems?: POSState['originalSentItems'],
  ) => void;
  resetOrderState: () => void;
  setSelectedAggregator: (aggregator: Aggregator | null) => void;
  setOrderComment: (comment: string) => void;
  setGuestLabel: (label: string) => void;
  setNoOfPax: (pax: number) => void;
  /**
   * Updates only the comment/note on an existing cart line, leaving quantity
   * and everything else untouched. Additive — used by the Captain order
   * screen (`pos/src/captain`) to support note-only edits on already-sent
   * items without going through the full `ProductDialog` edit flow (which
   * removes+re-adds the line and would also expose variant/addon controls
   * that don't apply to a sent item). Does not affect existing Cashier
   * `OrderPanel`/`ProductDialog` behavior, which never calls this.
   */
  updateItemComment: (uniqueId: string, comment: string) => void;
  setItemHeld: (uniqueId: string, held: boolean) => void;
  setShowVoluntaryClosing: (show: boolean) => void;
}

const generateUniqueId = (item: OrderItem): string => {
  if (
    item.isModifier ||
    item.parentLineKey ||
    item.lineRole === 'Combo Header' ||
    (item.selectedModifiers && item.selectedModifiers.length > 0) ||
    (item.bundleOverrides && item.bundleOverrides.length > 0)
  ) {
    return item.uniqueId || item.reservationLineKey || `${item.id}-${uuidv4()}`;
  }
  const variantId = item.selectedVariant?.id || 'default';
  const addonIds = item.selectedAddons?.map(addon => addon.id).sort().join('-') || 'no-addons';
  return `${item.id}-${variantId}-${addonIds}`;
};

function parseJsonArray<T>(raw?: string | null): T[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

const calculateItemPrice = (item: OrderItem): number => {
  const basePrice = item.selectedVariant?.price || item.price;
  const addonsTotal = item.selectedAddons?.reduce((sum, addon) => sum + addon.price, 0) || 0;
  return basePrice + addonsTotal;
};

export const usePOSStore = create<POSStore>((set, get) => ({
  menuItems: [],
  categories: [],
  categoriesDerived: false,
  activeOrders: [],
  selectedCategory: '',
  selectedTable: null,
  selectedRoom: null,
  searchQuery: '',
  selectedCustomer: null,
  customerSource: 'auto' as CustomerSource,
  selectedOrderType: DEFAULT_ORDER_TYPE as OrderType,
  quickFilter: "all",
  selectedItem: null,
  cartId: null,
  loading: false,
  menuLoading: false,
  orderLoading: false,
  profileLoading: false,
  error: null,
  paymentModes: ['Cash'],
  paymentModeDetails: {},
  orders: [],
  posProfile: null,
  availableOutlets: [],
  requiresOutletSelection: false,
  customerGroups: [],
  territories: [],
  selectedAggregator: null,
  currency: siteCurrencyCode() || storage.getItem('currency') || 'INR',
  currencySymbol: storage.getItem('currencySymbol') || null,
  tableOrder: null,
  isInitializing: true,
  editCustomer: null,
  isUpdatingOrder: false,
  orderId: null,
  originalSentItems: [],
  showVoluntaryClosing: false,
  activeServingPeriod: null,
  activeTaxSummary: null,
  activePriceList: null,
  orderComment: '',
  guestLabel: '',
  noOfPax: 1,
  lastModifiedTime: null,

  initializeApp: async () => {
    try {
      set({ isInitializing: true, error: null });
      
      await bindOfflineUser(getSessionUser());
      const [profileResult, _outletsResult, menuResult, categoriesResult, paymentModesResult] = await Promise.allSettled([
        get().fetchPosProfile(),
        get().fetchAvailableOutlets(),
        get().fetchMenuItems(),
        get().fetchCategories(),
        get().fetchPaymentModes()
      ]);

      if (get().requiresOutletSelection) {
        set({ isInitializing: false, error: null });
        return;
      }

      if (profileResult.status === 'rejected' ||
          !get().posProfile ||
          menuResult.status === 'rejected' || 
          categoriesResult.status === 'rejected' ||
          paymentModesResult.status === 'rejected') {
        set({ 
          error: 'Failed to initialize app. Please refresh the page.',
          isInitializing: false 
        });
        return;
      }

      set({ isInitializing: false });
    } catch (error) {
      set({ 
        error: 'Failed to initialize app. Please refresh the page.',
        isInitializing: false 
      });
    }
  },

  fetchAvailableOutlets: async () => {
    try {
      const owner = getSessionUser();
      const outlets = await getAvailableOutlets();
      if (owner !== getSessionUser()) return;
      if (outlets.length > 0) {
        set({ availableOutlets: outlets });
      }
    } catch (error) {
      console.error('Error fetching available outlets:', error);
    }
  },

  switchOutlet: async (posProfileName: string) => {
    const owner = getSessionUser();
    await setActiveOutlet(posProfileName);
    if (owner !== getSessionUser()) return;

    // Clear outlet-scoped browser caches so every panel reloads for the new outlet.
    const profileKey = ownedSessionKey('posProfile');
    if (profileKey) sessionStorage.removeItem(profileKey);
    const paymentKey = ownedSessionKey('payment_modes');
    if (paymentKey) sessionStorage.removeItem(paymentKey);
    sessionStorage.removeItem('menuCategories');
    storage.removeItem('currencySymbol');

    set({
      requiresOutletSelection: false,
      activeOrders: [],
      selectedTable: null,
      selectedRoom: null,
      selectedAggregator: null,
      tableOrder: null,
      isUpdatingOrder: false,
      editCustomer: null,
      orderId: null,
      originalSentItems: [],
      orderComment: '',
      guestLabel: '',
      noOfPax: 1,
      lastModifiedTime: null,
      menuItems: [],
      categories: [],
      selectedCategory: '',
      activeServingPeriod: null,
      activeTaxSummary: null,
      activePriceList: null,
      currencySymbol: null,
      error: null,
    });

    await get().fetchPosProfile();
    await Promise.allSettled([
      get().fetchAvailableOutlets(),
      get().fetchMenuItems(),
      get().fetchCategories(),
      get().fetchPaymentModes(),
      get().fetchCurrencySymbol(),
    ]);

    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('pos:outlet-switched', { detail: { posProfile: posProfileName } }),
      );
    }
  },

  fetchPosProfile: async () => {
    try {
      let currencySymbolHandled = false;
      const owner = getSessionUser();
      const key = ownedSessionKey('posProfile');
      const cached = key && sessionStorage.getItem(key);
      if (cached) {
        const profile = JSON.parse(cached);
        if (profile && profile.country && 'custom_invoice_warning_time' in profile && !profileLacksDefaultCustomerConfig(profile) && profile.enable_payment_without_printing !== undefined) {
          // Stale-while-revalidate: populate state from the cache immediately so
          // the UI stays responsive, then fall through and revalidate against
          // the server so desk configuration changes take effect on reload.
          set({
            posProfile: profile,
            profileLoading: false,
            currency: profile.currency || siteCurrencyCode() || 'INR'
          });
          get().applyAutoCustomer();
          if (!storage.getItem('currencySymbol')) {
            currencySymbolHandled = true;
            get().fetchCurrencySymbol();
          }
        }
      }

      if (!get().posProfile) {
        set({ profileLoading: true, error: null });
      }
      const bootstrap = typeof getPosProfileOrSelection === 'function'
        ? await getPosProfileOrSelection()
        : { requiresOutletSelection: false, availableOutlets: [], profile: await getCombinedPosProfile() };
      if (owner !== getSessionUser()) return;

      if (bootstrap.requiresOutletSelection || !bootstrap.profile) {
        if (key) sessionStorage.removeItem(key);
        set({
          posProfile: null,
          requiresOutletSelection: true,
          availableOutlets: bootstrap.availableOutlets,
          profileLoading: false,
          error: null,
        });
        return;
      }

      const combinedProfile = bootstrap.profile;
      // The loaded till's ury_offline_capture decides the /pos worker; off unregisters it.
      try {
        await syncOfflineCapture(combinedProfile as { ury_offline_capture?: unknown }, getSessionUser());
      } catch (offlineError) {
        console.error('Offline capture setup failed', offlineError);
      }

      if (owner !== getSessionUser()) return;
      if (key) sessionStorage.setItem(key, JSON.stringify(combinedProfile));
      set({
        posProfile: combinedProfile,
        requiresOutletSelection: false,
        profileLoading: false,
        currency: combinedProfile.currency || siteCurrencyCode() || 'INR'
      });
      get().applyAutoCustomer();

      if (!currencySymbolHandled && !storage.getItem('currencySymbol')) {
        await get().fetchCurrencySymbol();
      }
    } catch (error) {
      if (isLinkDown(error) && cachedOfflineProfile(getSessionUser()) && get().posProfile) {
        set({ profileLoading: false });
        return;
      }
      console.error('Error fetching POS profile:', error);
      set({
        error: 'Failed to fetch POS profile',
        profileLoading: false
      });
    }
  },

  fetchCurrencySymbol: async () => {
    try {
      const currency = get().currency;
      const response = await getCurrencyInfo(currency);
      const symbol = currencyDisplaySymbol(currency, response.symbol);
      
      set({ currencySymbol: symbol });
      storage.setItem('currencySymbol', symbol);
    } catch (error) {
      console.error('Error fetching currency symbol:', error);
      const fb = currencyDisplaySymbol(get().currency);
      set({ currencySymbol: fb });
      storage.setItem('currencySymbol', fb);
    }
  },

  fetchMenuItems: async () => {
    const { posProfile, selectedRoom, selectedOrderType, selectedCustomer } = get();
    if (!posProfile?.restaurant) return;

    try {
      set({ menuLoading: true, error: null });
      const payload = Object.prototype.hasOwnProperty.call(menuApi, 'getRestaurantMenuWithMeta')
        ? await menuApi.getRestaurantMenuWithMeta(
            posProfile.name,
            selectedRoom,
            selectedOrderType,
            selectedCustomer?.id || null,
          )
        : {
            items: await menuApi.getRestaurantMenu(posProfile.name, selectedRoom, selectedOrderType),
            active_serving_period: null,
            tax_summary: null,
            price_list: null,
          };
      const items = payload.items || [];
      
      const menuItems: MenuItem[] = items.map((item: any) => ({
        id: item.item,
        name: item.item_name,
        image: item.item_image || null,
        price: typeof item.rate === 'string' ? parseFloat(item.rate) : item.rate || 0,
        item: item.item,
        item_name: item.item_name,
        item_image: item.item_image,
        course: item.course,
        course_label: item.course_label || item.course,
        description: item.description || '',
        special_dish: item.special_dish || 0,
        custom_is_open_priced: Number(item.custom_is_open_priced || 0),
        custom_allow_zero_price: Number(item.custom_allow_zero_price || 0),
        tax_rate: 0,
        unavailable_daypart: Boolean(item.unavailable_daypart),
        daypart_label: item.daypart_label ?? null,
        promo_rate: item.promo_rate != null ? Number(item.promo_rate) : null,
        promo_label: item.promo_label ?? null,
        is_combo_meal: Boolean(item.is_combo_meal),
        product_bundle: item.product_bundle || null,
        bundle_defaults: Array.isArray(item.bundle_defaults) ? item.bundle_defaults : [],
        has_modifiers: Boolean(item.has_modifiers),
        has_required_modifiers: Boolean(item.has_required_modifiers),
        prompt_on_add: Boolean(item.prompt_on_add),
        modifier_groups: Array.isArray(item.modifier_groups) ? item.modifier_groups : [],
      }));

      set((state) => ({
        ...withMenuItems(state, menuItems),
        activeServingPeriod: payload.active_serving_period ?? null,
        activeTaxSummary: payload.tax_summary ?? null,
        activePriceList: payload.price_list ?? null,
      }));
    } catch (error) {
      set({ error: 'Failed to load menu items' });
      console.error('Error loading menu items:', error);
    } finally {
      set({ menuLoading: false });
    }
  },

  fetchAggregatorMenu: async (aggregator: string) => {
    try {
      set({ menuLoading: true, error: null });
      const items = await menuApi.getAggregatorMenu(aggregator);
      
      const menuItems: MenuItem[] = items.map((item: any) => ({
        ...item,
        id: item.item,
        name: item.item_name,
        image: item.item_image || null,
        price: typeof item.rate === 'string' ? parseFloat(item.rate) : item.rate || 0,
        category: item.course
      }));

      set((state) => ({ ...withMenuItems(state, menuItems), menuLoading: false }));
    } catch (error) {
      set({ error: 'Failed to load aggregator menu', menuLoading: false });
      console.error('Error loading aggregator menu:', error);
    }
  },

  fetchCategories: async () => {
    try {
      // Serve the shared cache, then always revalidate: a list cached before the
      // menu had courses would otherwise hide them for the whole session.
      const cached = sessionStorage.getItem('menuCategories');
      if (cached) set({ categories: JSON.parse(cached), categoriesDerived: false });
      let courses: Category[];
      try {
        courses = await getMenuCourses();
      } catch (error) {
        if (cached) return;
        throw error;
      }
      sessionStorage.setItem('menuCategories', JSON.stringify(courses));
      const categories = courses.length ? courses : categoriesFromItems(get().menuItems);
      set((state) => ({
        categories, categoriesDerived: !courses.length,
        selectedCategory: keepSelection(state.selectedCategory, categories),
      }));
    } catch (error) {
      set({ error: 'Failed to load menu categories' });
      throw error;
    }
  },

  fetchPaymentModes: async () => {
    try {
      const owner = getSessionUser();
      let details: PaymentModeConfig[] = [];
      let modes: string[] = [];
      try {
        details = await getPaymentModeDetails();
        modes = details.map((row) => row.mode_of_payment);
      } catch {
        modes = await getPaymentModes();
      }
      if (owner !== getSessionUser()) return;
      const detailsMap: Record<string, PaymentModeConfig> = {};
      for (const row of details) {
        if (row?.mode_of_payment) detailsMap[row.mode_of_payment] = row;
      }
      set({ paymentModes: modes, paymentModeDetails: detailsMap });
    } catch (error) {
      console.error('Failed to fetch payment modes:', error);
    }
  },

  initializeCart: async () => {
    set({ cartId: uuidv4() });
  },

  addToOrder: async (item: OrderItem) => {
    try {
      if (!get().validateQuantity(item.quantity)) {
        throw new CartError(`Quantity must be between ${MIN_QUANTITY} and ${MAX_QUANTITY}`);
      }

      const uniqueId = generateUniqueId(item);
      const existingItemIndex = get().activeOrders.findIndex(orderItem => !item.invoiceItemName && !orderItem.invoiceItemName
        && generateUniqueId(orderItem) === uniqueId && !!orderItem.ury_held === !!item.ury_held);

      if (existingItemIndex !== -1) {
        const existingItem = get().activeOrders[existingItemIndex];
        const newQuantity = existingItem.quantity + item.quantity;
        const newComment = item.comment !== undefined ? item.comment : existingItem?.comment || "";

        if (!get().validateQuantity(newQuantity)) {
          throw new CartError(`Cannot add item. Total quantity would exceed ${MAX_QUANTITY}`);
        }

        const newOrders = [...get().activeOrders];
        newOrders[existingItemIndex] = {
          ...existingItem,
          quantity: newQuantity,
          comment: newComment
        };
        
        set({ activeOrders: newOrders });
      } else {
        const rowId = item.invoiceItemName || (get().activeOrders.some(row => row.uniqueId === uniqueId || row.reservationLineKey === uniqueId) ? uuidv4() : uniqueId);
        const newOrders = [
          ...get().activeOrders,
          // A brand-new cart line owns its identity from creation: the
          // server persists this key on the invoice row and we echo it back
          // on every later sync (see OrderItem.reservationLineKey).
          { ...item, uniqueId: rowId, reservationLineKey: item.reservationLineKey || rowId },
        ];
        set({ activeOrders: newOrders });
      }
    } catch (error) {
      if (error instanceof CartError) {
        set({ error: error.message });
      } else {
        set({ error: 'Failed to add item to cart' });
      }
    }
  },

  addConfiguredItems: async (parent: OrderItem, children: OrderItem[], replaceParentUniqueId?: string) => {
    try {
      if (!get().validateQuantity(parent.quantity)) {
        throw new CartError(`Quantity must be between ${MIN_QUANTITY} and ${MAX_QUANTITY}`);
      }
      const current = get().activeOrders;
      let parentKey = parent.reservationLineKey || parent.uniqueId || uuidv4();
      let parentUniqueId = parent.uniqueId || parentKey;
      let parentInvoiceItemName = parent.invoiceItemName;
      let heldState = {
        ury_held: parent.ury_held,
        fireAt: parent.fireAt,
        fireConfirmationPending: parent.fireConfirmationPending,
      };
      let oldChildren: OrderItem[] = [];
      let baseOrders = current;
      let insertAt = current.length;

      if (replaceParentUniqueId) {
        const oldIdx = current.findIndex((o) => o.uniqueId === replaceParentUniqueId);
        if (oldIdx !== -1) {
          insertAt = oldIdx;
          const oldParent = current[oldIdx];
          const oldKey = oldParent.reservationLineKey || oldParent.uniqueId;
          parentKey = oldKey || parentKey;
          parentUniqueId = oldParent.uniqueId || parentKey;
          parentInvoiceItemName = oldParent.invoiceItemName ?? parentInvoiceItemName;
          heldState = {
            ury_held: oldParent.ury_held,
            fireAt: oldParent.fireAt,
            fireConfirmationPending: oldParent.fireConfirmationPending,
          };
          oldChildren = oldKey
            ? current.filter((o) => o.uniqueId !== replaceParentUniqueId && o.parentLineKey === oldKey)
            : [];
          baseOrders = current.filter(
            (o) => o.uniqueId !== replaceParentUniqueId && (!oldKey || o.parentLineKey !== oldKey)
          );
        }
      }

      const normalizedParent: OrderItem = {
        ...parent,
        ...heldState,
        uniqueId: parentUniqueId,
        reservationLineKey: parentKey,
        ...(parentInvoiceItemName ? { invoiceItemName: parentInvoiceItemName } : {}),
        isModifier: false,
      };
      const remainingOldChildren = [...oldChildren];
      const normalizedChildren: OrderItem[] = children.map((child, idx) => {
        let matchedIdx = remainingOldChildren.findIndex(
          (prev) =>
            prev.item === child.item &&
            (prev.modifierGroup || '') === (child.modifierGroup || '') &&
            (prev.modifierOption || '') === (child.modifierOption || '')
        );
        if (matchedIdx === -1) {
          matchedIdx = remainingOldChildren.findIndex(
            (prev) => prev.item === child.item && (prev.lineRole || '') === (child.lineRole || '')
          );
        }
        const matchedPrev = matchedIdx !== -1 ? remainingOldChildren.splice(matchedIdx, 1)[0] : undefined;
        const childResKey =
          matchedPrev?.reservationLineKey ||
          child.reservationLineKey ||
          matchedPrev?.uniqueId ||
          child.uniqueId ||
          `${parentKey}-m-${idx + 1}-${uuidv4().slice(0, 6)}`;
        const childUniqueId = matchedPrev?.uniqueId || child.uniqueId || childResKey;
        const childInvoiceItemName = matchedPrev?.invoiceItemName ?? child.invoiceItemName;
        return {
          ...child,
          ...heldState,
          uniqueId: childUniqueId,
          reservationLineKey: childResKey,
          ...(childInvoiceItemName ? { invoiceItemName: childInvoiceItemName } : {}),
          parentLineKey: parentKey,
          isModifier: true,
        };
      });

      const nextOrders = [...baseOrders];
      nextOrders.splice(Math.min(insertAt, nextOrders.length), 0, normalizedParent, ...normalizedChildren);
      set({ activeOrders: nextOrders });
    } catch (error) {
      if (error instanceof CartError) {
        set({ error: error.message });
      } else {
        set({ error: 'Failed to add configured item to cart' });
      }
    }
  },

  removeFromOrder: async (uniqueId: string) => {
    try {
      const current = get().activeOrders;
      const target = current.find((item) => item.uniqueId === uniqueId);
      const targetKey = target && !target.isModifier ? (target.reservationLineKey || target.uniqueId) : null;
      const newOrders = current.filter(
        (item) => item.uniqueId !== uniqueId && (!targetKey || item.parentLineKey !== targetKey)
      );
      set({ activeOrders: newOrders });
    } catch (error) {
      set({ error: 'Failed to remove item from cart' });
    }
  },

  updateQuantity: async (uniqueId: string, quantity: number) => {
    try {
      if (!get().validateQuantity(quantity)) {
        throw new CartError(`Quantity must be between ${MIN_QUANTITY} and ${MAX_QUANTITY}`);
      }

      const current = get().activeOrders;
      const target = current.find((item) => item.uniqueId === uniqueId);
      const targetKey = target && !target.isModifier ? (target.reservationLineKey || target.uniqueId) : null;
      const ratio = target && target.quantity > 0 ? quantity / target.quantity : 1;

      const newOrders = current.map((item) => {
        if (item.uniqueId === uniqueId) {
          return { ...item, quantity };
        }
        if (targetKey && item.parentLineKey === targetKey) {
          const scaled = Math.max(1, Math.round(item.quantity * ratio * 1000) / 1000);
          return { ...item, quantity: Math.min(MAX_QUANTITY, scaled) };
        }
        return item;
      });
      set({ activeOrders: newOrders });
    } catch (error) {
      if (error instanceof CartError) {
        set({ error: error.message });
      } else {
        set({ error: 'Failed to update quantity' });
      }
    }
  },

  updateItemComment: (uniqueId: string, comment: string) => {
    const newOrders = get().activeOrders.map(item =>
      item.uniqueId === uniqueId ? { ...item, comment } : item
    );
    set({ activeOrders: newOrders });
  },

  setItemHeld: (uniqueId, held) => {
    if (navigator.onLine === false || !get().posProfile?.ury_enable_hold_fire) throw new Error(t('hold_fire.offline'));
    const items = get().activeOrders;
    const target = items.find(item => item.uniqueId === uniqueId);
    if (!target || target.invoiceItemName || target.parentLineKey) return;
    const key = target.reservationLineKey || uniqueId;
    set({ activeOrders: items.map(item =>
      !item.invoiceItemName && (item === target || item.parentLineKey === key)
        ? { ...item, ury_held: held ? 1 : 0 }
        : item
    ) });
  },

  clearOrder: async () => {
    try {
      set({ activeOrders: [] });
    } catch (error) {
      set({ error: 'Failed to clear cart' });
    }
  },

  setSelectedCategory: (category) => set({ selectedCategory: category }),
  setSearchQuery: (query) => set({ searchQuery: query }),
  setSelectedCustomer: (customer) => set({ selectedCustomer: customer, customerSource: 'manual' }),
  applyAutoCustomer: () => {
    const { isUpdatingOrder, customerSource, selectedOrderType, posProfile } = get();
    if (isUpdatingOrder || customerSource !== 'auto') return;
    set({ selectedCustomer: resolveDefaultCustomer(selectedOrderType, posProfile) });
  },
  setSelectedTable: (table: string | null, room: string | null, doNotLoadOrder: boolean = false) => {
    set({ selectedTable: table, selectedRoom: room });
    if (table ) {
      if (!doNotLoadOrder) 
        get().loadTableOrder(table);
    } else {
      get().clearTableOrder();
    }
    if (room) {
      get().fetchMenuItems();
    }
  },
  setSelectedOrderType: (type) => {
    const { fetchMenuItems, isUpdatingOrder, customerSource, posProfile } = get();

    // Re-resolve the default customer only while it is auto-set on a new order.
    // A manual pick, a customer loaded from an existing order, and any customer
    // on an order being updated are never touched. Aggregators resolve to null
    // (the aggregator flow owns its customer).
    const followDefault = !isUpdatingOrder && customerSource === 'auto';

    set({ 
      activeOrders: [],
      selectedOrderType: type,
      isUpdatingOrder: false,
      editCustomer: null,
      orderId: null,
      ...(type !== 'Dine In' ? { selectedTable: null, selectedRoom: null } : {}),
      ...(followDefault ? { selectedCustomer: resolveDefaultCustomer(type, posProfile) } : {}),
    });
    
    if (type !== 'Aggregators') {
      fetchMenuItems();
    }
  },
  setQuickFilter: (filter) => set({ quickFilter: filter }),
  setSelectedItem: (item) => set({ selectedItem: item }),
  setSelectedAggregator: (aggregator) => set({ selectedAggregator: aggregator }),
  setOrderComment: (comment: string) => set({ orderComment: comment }),
  setGuestLabel: (label: string) => set({ guestLabel: label }),
  setNoOfPax: (pax: number) => set({ noOfPax: pax }),
  setShowVoluntaryClosing: (show: boolean) => set({ showVoluntaryClosing: show }),

  processPayment: async (paymentMode: string, amount: number) => {
    try {
      const { cartId, selectedCustomer, selectedOrderType } = get();
      
      const order: Order = {
        id: uuidv4(),
        cartId: cartId!,
        customerId: selectedCustomer?.id,
        paymentModeId: paymentMode,
        paymentMode,
        orderType: selectedOrderType,
        status: 'paid',
        totalAmount: amount,
        paidAmount: amount,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString()
      };

      const newOrders = [...get().orders, order];
      set({ orders: newOrders });
      
      await get().clearOrder();
    } catch (error) {
      set({ error: (error as Error).message });
    }
  },

  updateOrderStatus: async (orderId: string, status: Order['status']) => {
    try {
      const newOrders = get().orders.map(order => 
        order.id === orderId 
          ? { ...order, status, updatedAt: new Date().toISOString() }
          : order
      );
      set({ orders: newOrders });
    } catch (error) {
      set({ error: (error as Error).message });
    }
  },

  fetchCustomerGroups: async () => {
    const owner = getSessionUser();
    const key = ownedSessionKey('customerGroups');
    const cached = key && sessionStorage.getItem(key);
    if (cached) {
      set({ customerGroups: JSON.parse(cached) });
      return;
    }
    const groups = await getCustomerGroups();
    const names = groups.map((g: any) => g.name);
    if (owner !== getSessionUser()) return;
    set({ customerGroups: names });
    if (key) sessionStorage.setItem(key, JSON.stringify(names));
  },

  fetchTerritories: async () => {
    const owner = getSessionUser();
    const key = ownedSessionKey('territories');
    const cached = key && sessionStorage.getItem(key);
    if (cached) {
      set({ territories: JSON.parse(cached) });
      return;
    }
    const terrs = await getCustomerTerritories();
    const names = terrs.map((t: any) => t.name);
    if (owner !== getSessionUser()) return;
    set({ territories: names });
    if (key) sessionStorage.setItem(key, JSON.stringify(names));
  },

  getCartTotals: (): CartTotals => {
    const items = get().activeOrders;
    const itemCount = items.reduce((sum, item) => sum + item.quantity, 0);
    
    const subtotal = items.reduce((sum, item) => {
      const itemPrice = calculateItemPrice(item);
      return sum + (itemPrice * item.quantity);
    }, 0);

    const tax = items.reduce((sum, item) => {
      const itemPrice = calculateItemPrice(item);
      const taxRate = item.tax_rate || 0;
      return sum + (itemPrice * item.quantity * (taxRate / 100));
    }, 0);

    return {
      subtotal,
      tax,
      total: subtotal + tax,
      itemCount
    };
  },

  itemExistsInCart: (uniqueId: string): boolean => {
    return get().activeOrders.some(item => item.uniqueId === uniqueId);
  },

  validateQuantity: (quantity: number): boolean => {
    return !isNaN(quantity) && quantity >= MIN_QUANTITY && quantity <= MAX_QUANTITY;
  },

  getItemPrice: (item: OrderItem): number => {
    return calculateItemPrice(item);
  },

  getItemQuantityFromCart: (item: MenuItem): number => {
    const uniqueId = generateUniqueId(item as OrderItem);
    const cartItem = get().activeOrders.find(orderItem => orderItem.uniqueId === uniqueId);
    return cartItem?.quantity || 0;
  },

  loadTableOrder: async (table: string) => {
    const read = beginOpenTableRead(getSessionUser());
    const stale = () => read ? !openTableReadCurrent(read, getSessionUser()) : !!getSessionUser();
    try {
      set({ orderLoading: true, error: null });
      const response = await getTableOrder(table, true);
      if (stale()) return;
      const order = response.message;
      if (order && order.name && order.items && order.items.length > 0) {
        const orderItems: OrderItem[] = order.items.map(item => {
          const orderItem = {
            id: item.item_code,
            name: item.item_name,
            price: item.rate,
            quantity: item.qty,
            amount: item.amount,
            image: item.image || null,
            item: item.item_code,
            item_name: item.item_name,
            item_image: null,
            course: item.custom_course || '',
            invoiceItemName: item.name,
            ury_held: item.ury_held || 0,
            fireAt: item.fire_at,
            fireConfirmationPending: !!item.fire_confirmation_pending,
            description: item.description || '',
            special_dish: 0 as 0 | 1,
            tax_rate: 0,
            comment: item.comment || '',
            parentLineKey: item.custom_parent_line_key || null,
            lineRole: item.custom_line_role || 'Standard',
            isModifier: Number(item.custom_is_modifier || 0) === 1,
            modifierPrefix: item.custom_modifier_prefix || null,
            modifierGroup: item.custom_modifier_group || null,
            modifierOption: item.custom_modifier_option || null,
            selectedModifiers: parseJsonArray<SelectedModifier>(item.custom_modifiers_json),
            bundleOverrides: parseJsonArray<BundleOverride>(item.custom_bundle_overrides_json),
            inheritParentRouting: item.custom_inherit_parent_routing !== 0,
            hideUnpricedOnReceipt: Number(item.custom_hide_unpriced_on_receipt || 0) === 1,
          };
          const uniqueId = item.name || generateUniqueId(orderItem as OrderItem);
          return {
            ...orderItem,
            uniqueId,
            // Prefer the key the server already persisted for this row, so
            // the identity survives the round trip byte-for-byte. Only a
            // legacy row saved before the key was persisted falls back to
            // the locally regenerated uniqueId (the server then adopts it —
            // see ury_order.py::_backfill_previous_line_keys).
            reservationLineKey: item.reservation_line_key || uniqueId,
          } as OrderItem;
        });

        if (read && order.ury_client_order_id) {
          rememberOpenTableCheck(read.user, {
            table, clientOrderId: order.ury_client_order_id, invoice: order.name,
            grandTotal: order.grand_total, customer: order.customer, orderType: order.order_type,
            noOfPax: order.no_of_pax, room: order.custom_restaurant_room,
          });
        }
        set({
          tableOrder: response,
          activeOrders: orderItems,
          selectedCustomer: order.customer ? {
            id: order.customer,
            name: order.customer_name,
            phone: order.mobile_number,
          } : null,
          customerSource: 'order',
          isUpdatingOrder: true,
          orderId: order.name,
          originalSentItems: order.items.map((item) => ({
            invoice_item_name: item.name,
            item_code: item.item_code,
            item_name: item.item_name,
            reservation_line_key: item.reservation_line_key || '',
            qty: Number(item.qty || 0),
            rate: Number(item.rate || 0),
            comment: item.comment || '',
            ury_held: Number(item.ury_held || 0),
          })),
          noOfPax: order.no_of_pax || 1,
          lastModifiedTime: order.modified || null,
          orderComment: order.custom_comments || '',
          guestLabel: order.custom_guest_label || '',
        });
      } else {
        const state = get();
        if (read && !order?.name) forgetOpenTableCheck(read.user, table);
        const orderCustomer = order && order.customer ? {
          id: order.customer,
          name: order.customer_name || order.customer,
          phone: order.mobile_number || '',
        } : null;
        // Entering an empty table: a saved (empty) order keeps its own customer, otherwise
        // preselect the default for the current order type (entering a table always reset
        // the customer before, so a previous table's pick never carries over).
        set({
          tableOrder: order ? response : null,
          activeOrders: [],
          ...(orderCustomer
            ? { selectedCustomer: orderCustomer, customerSource: 'order' as CustomerSource }
            : {
                selectedCustomer: resolveDefaultCustomer(state.selectedOrderType, state.posProfile),
                customerSource: 'auto' as CustomerSource,
              }),
          isUpdatingOrder: false,
          orderId: order?.name || null,
          noOfPax: order?.no_of_pax || 1,
          lastModifiedTime: order?.modified || null,
          orderComment: order?.custom_comments || '',
          guestLabel: order?.custom_guest_label || '',
        });
      }
    } catch (error) {
      if (stale()) return;
      const down = isLinkDown(error) && offlineCaptureEnabled(get().posProfile);
      set({
        error: down ? null : 'Failed to load table order',
        tableOrder: null,
        activeOrders: [],
        selectedCustomer: null,
        customerSource: 'auto',
        isUpdatingOrder: false,
        editCustomer: null,
        orderId: null,
        noOfPax: 1,
        lastModifiedTime: null,
        orderComment: '',
        guestLabel: '',
      });
    } finally {
      set({ orderLoading: false });
    }
  },

  clearTableOrder: () => {
    const { selectedOrderType, posProfile } = get();
    set({
      tableOrder: null,
      activeOrders: [],
      selectedCustomer: resolveDefaultCustomer(selectedOrderType, posProfile),
      customerSource: 'auto',
      isUpdatingOrder: false,
      editCustomer: null,
      orderId: null,
      noOfPax: 1,
      lastModifiedTime: null,
      orderComment: '',
      guestLabel: '',
    });
  },

  setOrderForUpdate: (orderId, customer, modified, originalItems) => {
    set({ 
      editCustomer: orderId && customer ? { invoice: orderId, customer } : null,
      lastModifiedTime: modified ?? null,
      isUpdatingOrder: orderId !== null,
      orderId,
      originalSentItems: originalItems ?? [],
    });
  },

  resetOrderState: () => {
    const { fetchMenuItems, posProfile } = get();
    
    set({
      selectedCustomer: resolveDefaultCustomer(DEFAULT_ORDER_TYPE, posProfile),
      customerSource: 'auto',
      selectedTable: null,
      selectedRoom: null,
      selectedAggregator: null,
      isUpdatingOrder: false,
      editCustomer: null,
      orderId: null,
      originalSentItems: [],
      activeOrders: [],
      selectedItem: null,
      orderLoading: false,
      menuItems: [],
      error: null,
      selectedOrderType: DEFAULT_ORDER_TYPE,
      orderComment: '',
      guestLabel: '',
      noOfPax: 1,
      lastModifiedTime: null,
    });

    fetchMenuItems();
  },

  isMenuInteractionDisabled: () => {
    const state = get();
    return state.menuLoading || state.profileLoading;
  },

  isOrderInteractionDisabled: () => {
    const state = get();
    return state.orderLoading;
  }
})); 