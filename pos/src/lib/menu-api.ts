import { call } from '@ury/core';

export interface ActiveServingPeriod {
  period_name: string;
  start_time: string;
  end_time: string;
  menu?: string | null;
  price_list_override?: string | null;
  priority?: number;
}

export interface TaxSummary {
  tax_category?: string | null;
  taxes_and_charges?: string | null;
  label?: string | null;
}

export interface ModifierOption {
  option_code: string;
  item: string | null;
  label: string;
  price_mode: 'Unpriced Prep' | 'Menu Item Price' | 'Combo Upcharge';
  upcharge_amount: number;
  upcharge_item?: string | null;
  replaces_bundle_item?: string | null;
  qty?: number;
  is_default?: boolean;
  unit_price: number;
  available: boolean;
}

export interface ModifierGroup {
  name: string;
  display_title: string;
  group_type: 'Condiment' | 'Combo Choice';
  required: boolean;
  prompt_on_add: boolean;
  min_selections: number;
  max_selections: number;
  allow_prefixes: boolean;
  allowed_prefixes: string[];
  inherit_parent_routing: boolean;
  hide_unpriced_on_receipt: boolean;
  options: ModifierOption[];
}

export interface BundleDefaultItem {
  item_code: string;
  item_name: string;
  qty: number;
}

export interface SelectedModifier {
  group: string;
  option_code: string;
  label: string;
  prefix?: string | null;
  price_mode?: 'Unpriced Prep' | 'Menu Item Price' | 'Combo Upcharge';
  hide_on_receipt?: number;
}

export interface BundleOverride {
  group: string;
  slot_item: string;
  chosen_item: string;
  option_code?: string;
  label: string;
  qty: number;
  inherit_parent_routing?: number;
  hide_on_receipt?: number;
}

export interface MenuItem {
  item: string;
  item_name: string;
  item_image: string | null;
  rate: number | string;
  course: string;
  course_label?: string;
  trending?: boolean;
  popular?: boolean;
  recommended?: boolean;
  description?: string;
  special_dish?: 1 | 0;
  unavailable_daypart?: boolean;
  daypart_label?: string | null;
  promo_rate?: number | null;
  promo_label?: string | null;
  is_combo_meal?: boolean;
  product_bundle?: string | null;
  bundle_defaults?: BundleDefaultItem[];
  has_modifiers?: boolean;
  has_required_modifiers?: boolean;
  prompt_on_add?: boolean;
  modifier_groups?: ModifierGroup[];
  custom_is_open_priced?: number;
  custom_allow_zero_price?: number;
}

export interface MenuResponsePayload {
  items: MenuItem[];
  name?: string;
  modified_time?: string;
  active_serving_period?: ActiveServingPeriod | null;
  price_list?: string | null;
  timezone?: string | null;
  tax_summary?: TaxSummary | null;
}

export interface GetMenuResponse {
  message: MenuResponsePayload;
}

export interface GetAggregatorMenuResponse {
  message: MenuItem[];
}

export const getRestaurantMenuWithMeta = async (
  posProfile: string,
  room: string | null,
  order_type: string | null,
  customer?: string | null,
): Promise<MenuResponsePayload> => {
  try {
    const response = await call.get<GetMenuResponse>(
      'ury.ury_pos.api.getRestaurantMenu',
      {
        pos_profile: posProfile,
        room: room,
        order_type: order_type,
        ...(customer ? { customer } : {}),
      }
    );
    return response.message;
  } catch (error: any) {
    if (error._server_messages) {
      const messages = JSON.parse(error._server_messages);
      const message = JSON.parse(messages[0]);
      throw new Error(message.message);
    }
    throw error;
  }
};

export const getRestaurantMenu = async (posProfile: string, room: string | null, order_type: string | null) => {
  const payload = await getRestaurantMenuWithMeta(posProfile, room, order_type);
  return payload.items;
};

export const getAggregatorMenu = async (aggregator: string) => {
  try {
    const response = await call.get<GetAggregatorMenuResponse>(
      'ury.ury_pos.api.getAggregatorItem',
      {
        aggregator
      }
    );
    return response.message;
  } catch (error: any) {
    if (error._server_messages) {
      const messages = JSON.parse(error._server_messages);
      const message = JSON.parse(messages[0]);
      throw new Error(message.message);
    }
    throw error;
  }
}; 