// Reopening is driven by invoice rows. A menu item can represent several
// kitchen lines, and may have been removed from the menu since service.
export function invoiceCartRows(rows, menu) {
  const menuByCode = new Map(menu.map(item => [item.item, item]));
  return (rows || []).map(row => {
    const item = {
      ...menuByCode.get(row.item_code),
      item: row.item_code,
      item_name: row.item_name || menuByCode.get(row.item_code)?.item_name || row.item_code,
      qty: row.qty,
      rate: row.rate,
      comment: row.comment || '',
      name: row.name,
      reservation_line_key: row.reservation_line_key,
    };
    for (const field of [
      'ury_held', 'custom_course', 'custom_seat_no', 'custom_parent_line_key', 'custom_line_role',
      'custom_is_modifier', 'custom_modifier_prefix', 'custom_modifier_group', 'custom_modifier_option',
      'custom_modifiers_json', 'custom_bundle_overrides_json', 'custom_inherit_parent_routing',
      'custom_hide_unpriced_on_receipt',
    ]) {
      if (row[field] != null) item[field] = row[field];
    }
    return item;
  });
}
