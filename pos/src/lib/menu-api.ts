import { call } from '@ury/core';

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
  is_combo_meal?: boolean;
  product_bundle?: string | null;
  bundle_defaults?: BundleDefaultItem[];
  has_modifiers?: boolean;
  has_required_modifiers?: boolean;
  prompt_on_add?: boolean;
  modifier_groups?: ModifierGroup[];
}

export interface GetMenuResponse {
  message: {
    items: MenuItem[];
  };
}

export interface GetAggregatorMenuResponse {
  message: MenuItem[];
}

export const getRestaurantMenu = async (posProfile: string, room: string | null, order_type: string | null) => {
  try {
    const response = await call.get<GetMenuResponse>(
      'ury.ury_pos.api.getRestaurantMenu',
      {
        pos_profile: posProfile,
        room: room,
        order_type: order_type
      }
    );
    return response.message.items;
  } catch (error: any) {
    if (error._server_messages) {
      const messages = JSON.parse(error._server_messages);
      const message = JSON.parse(messages[0]);
      throw new Error(message.message);
    }
    throw error;
  }
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