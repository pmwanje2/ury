import type { BundleOverride, ModifierGroup, ModifierOption } from './menu-api';
import type { MenuItem, OrderItem } from '../store/pos-store';

export function buildComboChoice(
  group: ModifierGroup, option: ModifierOption, quantity: number, course: string, prefix?: string | null,
): { override: BundleOverride; fee?: OrderItem } {
  const item = option.item || option.option_code;
  const override = {
    group: group.name, slot_item: option.replaces_bundle_item || '', chosen_item: item,
    option_code: option.option_code, label: option.label, qty: option.qty || 1,
    inherit_parent_routing: group.inherit_parent_routing ? 1 : 0,
    hide_on_receipt: group.hide_unpriced_on_receipt ? 1 : 0,
  };
  const price = Number(option.unit_price ?? option.upcharge_amount ?? 0);
  if (option.price_mode !== 'Combo Upcharge' || !(price > 0)) return { override };
  const code = option.upcharge_item || item;
  return { override, fee: {
    id: code, item: code, name: option.label, item_name: option.label, image: null,
    price, quantity: quantity * override.qty, course: course || '', special_dish: 0, tax_rate: 0,
    isModifier: true, lineRole: 'Combo Upcharge', modifierGroup: group.name,
    modifierOption: option.option_code, modifierPrefix: prefix || null,
    inheritParentRouting: group.inherit_parent_routing, hideUnpricedOnReceipt: false,
  } };
}

/** Rebuild fee rows from choices, including choices that were free in the sent round. */
export function repeatComboChoices(parent: OrderItem, item: MenuItem, previousFees: OrderItem[]) {
  const overrides: BundleOverride[] = [];
  const fees: OrderItem[] = [];
  const unmatched = new Set(previousFees);
  for (const choice of parent.bundleOverrides || []) {
    const group = item.modifier_groups?.find(group => group.name === choice.group);
    const matches = group?.options.filter(option =>
      (group.group_type === 'Combo Choice' || option.replaces_bundle_item) &&
      (!choice.option_code || option.option_code === choice.option_code) &&
      (option.item || option.option_code) === choice.chosen_item &&
      (option.replaces_bundle_item || '') === choice.slot_item);
    if (!group || matches?.length !== 1 || matches[0].available === false) return null;

    const option = matches[0];
    const oldFee = previousFees.find(row =>
      row.modifierGroup === group.name && row.modifierOption === option.option_code);
    if (oldFee) unmatched.delete(oldFee);
    const { override, fee } = buildComboChoice(group, option, parent.quantity, item.course, oldFee?.modifierPrefix);
    overrides.push(override);
    if (fee) fees.push(fee);
  }
  return unmatched.size ? null : { overrides, fees };
}
