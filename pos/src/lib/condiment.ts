import type { ModifierGroup, ModifierOption, SelectedModifier } from './menu-api';
import type { MenuItem, OrderItem } from '../store/pos-store';

export function pricedCondiment(
  group: ModifierGroup, option: ModifierOption, quantity: number, course: string, prefix?: string | null,
): OrderItem {
  const code = option.item || option.option_code;
  return {
    id: code, item: code, name: option.label, item_name: option.label,
    image: null, price: Number(option.unit_price || 0), quantity: quantity * (option.qty || 1),
    course: course || '', special_dish: 0, tax_rate: 0, isModifier: true, lineRole: 'Modifier',
    modifierGroup: group.name, modifierOption: option.option_code, modifierPrefix: prefix || null,
    inheritParentRouting: group.inherit_parent_routing, hideUnpricedOnReceipt: false,
  };
}

export function prepChoice(group: ModifierGroup, option: ModifierOption, prefix?: string | null): SelectedModifier {
  return {
    group: group.name, option_code: option.option_code, label: option.label, prefix: prefix || null,
    price_mode: option.price_mode, hide_on_receipt: group.hide_unpriced_on_receipt ? 1 : 0,
  };
}

function currentCondiment(
  item: MenuItem, groupName: string | null | undefined, code: string | null | undefined,
  mode: ModifierOption['price_mode'], prefix?: string | null,
) {
  const group = item.modifier_groups?.find(group => group.name === groupName);
  const option = group?.options.find(option => option.option_code === code);
  if (!group || !option || option.available === false || option.price_mode !== mode ||
      group.group_type === 'Combo Choice' || option.replaces_bundle_item) return null;
  const currentPrefix = prefix && group.allow_prefixes
    ? group.allowed_prefixes.find(value => value.toLowerCase() === prefix.toLowerCase()) : null;
  if (prefix && !currentPrefix) return null;
  return { group, option, prefix: currentPrefix };
}

export function repeatCondiments(parent: OrderItem, item: MenuItem, pricedRows: OrderItem[]) {
  const selectedModifiers: SelectedModifier[] = [];
  const children: OrderItem[] = [];
  for (const saved of parent.selectedModifiers || []) {
    const match = currentCondiment(item, saved.group, saved.option_code, 'Unpriced Prep', saved.prefix);
    if (!match) return null;
    selectedModifiers.push(prepChoice(match.group, match.option, match.prefix));
  }
  for (const row of pricedRows) {
    const match = currentCondiment(item, row.modifierGroup, row.modifierOption, 'Menu Item Price', row.modifierPrefix);
    if (!match) return null;
    children.push(pricedCondiment(match.group, match.option, parent.quantity, item.course, match.prefix));
  }
  return { selectedModifiers, children };
}
