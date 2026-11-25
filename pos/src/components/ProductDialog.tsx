import React, { useEffect, useMemo, useState } from 'react';
import { v4 as uuidv4 } from 'uuid';
import { ProductConfigurator, type ModifierGroupView } from '@ury/ui';
import { formatCurrency, db } from '@ury/core';
import { OrderItem, usePOSStore } from '../store/pos-store';
import type { BundleOverride, ModifierGroup, SelectedModifier } from '../lib/menu-api';
import { t } from '../i18n';

interface ProductDialogProps {
  onClose: () => void;
  editMode?: boolean;
  initialVariant?: { id: string; name: string; price: number };
  initialAddons?: Array<{ id: string; name: string; price: number }>;
  initialQuantity?: number;
  itemToReplace?: OrderItem;
}

interface ChosenOption {
  optionCode: string;
  prefix: string | null;
}

function buildInitialSelections(
  groups: ModifierGroup[],
  editMode: boolean,
  itemToReplace: OrderItem | undefined,
  activeOrders: OrderItem[]
): Record<string, ChosenOption[]> {
  const map: Record<string, ChosenOption[]> = {};
  for (const grp of groups) {
    map[grp.name] = [];
  }

  if (editMode && itemToReplace) {
    for (const mod of itemToReplace.selectedModifiers || []) {
      if (map[mod.group]) {
        map[mod.group].push({ optionCode: mod.option_code, prefix: mod.prefix || null });
      }
    }
    for (const ov of itemToReplace.bundleOverrides || []) {
      if (map[ov.group]) {
        const code = ov.option_code || ov.chosen_item;
        if (!map[ov.group].some((c) => c.optionCode === code)) {
          map[ov.group].push({ optionCode: code, prefix: null });
        }
      }
    }
    const parentKey = itemToReplace.reservationLineKey || itemToReplace.uniqueId;
    if (parentKey) {
      for (const row of activeOrders) {
        if (row.isModifier && row.parentLineKey === parentKey && row.modifierGroup && map[row.modifierGroup]) {
          const code = row.modifierOption || row.item;
          if (!map[row.modifierGroup].some((c) => c.optionCode === code)) {
            map[row.modifierGroup].push({ optionCode: code, prefix: row.modifierPrefix || null });
          }
        }
      }
    }
    return map;
  }

  // Fresh selection: pre-select any available default options (e.g. Combo default Fries / Cola)
  for (const grp of groups) {
    const defaults = grp.options.filter((o) => o.is_default && o.available);
    if (defaults.length > 0) {
      const limit = grp.max_selections > 0 ? grp.max_selections : defaults.length;
      map[grp.name] = defaults.slice(0, limit).map((o) => ({ optionCode: o.option_code, prefix: null }));
    }
  }
  return map;
}

const ProductDialog: React.FC<ProductDialogProps> = ({
  onClose,
  editMode = false,
  initialAddons = [],
  initialQuantity,
  itemToReplace,
}) => {
  const {
    selectedItem,
    addToOrder,
    addConfiguredItems,
    removeFromOrder,
    setSelectedItem,
    getItemQuantityFromCart,
    activeOrders,
    menuItems,
    posProfile,
    currency: storeCurrency,
  } = usePOSStore();
  const activeCurrency = posProfile?.currency || storeCurrency;

  // Hydrate modifier_groups from catalog menuItems if editing an existing cart line
  const catalogMatch = useMemo(
    () => (selectedItem ? menuItems.find((m) => m.item === selectedItem.item) : undefined),
    [selectedItem, menuItems]
  );

  const modifierGroups: ModifierGroup[] = useMemo(() => {
    if (selectedItem?.modifier_groups && selectedItem.modifier_groups.length > 0) {
      return selectedItem.modifier_groups;
    }
    return catalogMatch?.modifier_groups || [];
  }, [selectedItem, catalogMatch]);

  const isConfigurable = Boolean(
    modifierGroups.length > 0 ||
      selectedItem?.has_modifiers ||
      selectedItem?.has_required_modifiers ||
      selectedItem?.prompt_on_add ||
      selectedItem?.is_combo_meal ||
      catalogMatch?.is_combo_meal
  );

  const existingCartItem =
    selectedItem && !isConfigurable
      ? activeOrders.find(
          (order) =>
            order.id === selectedItem.id &&
            (!order.selectedAddons ||
              (order.selectedAddons.length === initialAddons.length &&
                order.selectedAddons.every((addon) =>
                  initialAddons.some((initAddon) => initAddon.id === addon.id)
                )))
        )
      : null;

  const [itemDoc, setItemDoc] = useState<Record<string, unknown> | null>(null);
  const [selectedAddons, setSelectedAddons] = useState<Array<{ id: string; name: string; price: number }>>(
    initialAddons
  );
  const [quantity, setQuantity] = useState<string>(
    editMode
      ? initialQuantity?.toString() || '1'
      : isConfigurable
        ? '1'
        : '0'
  );
  const [comments, setComments] = useState<string>(
    itemToReplace?.comment || existingCartItem?.comment || ''
  );
  const [addonsLoading, setAddonsLoading] = useState(false);
  const [addonsError, setAddonsError] = useState<string | null>(null);

  const [selectedByGroup, setSelectedByGroup] = useState<Record<string, ChosenOption[]>>(() =>
    buildInitialSelections(modifierGroups, editMode, itemToReplace, activeOrders)
  );
  const [activePrefixByGroup, setActivePrefixByGroup] = useState<Record<string, string | null>>({});

  useEffect(() => {
    setSelectedByGroup(buildInitialSelections(modifierGroups, editMode, itemToReplace, activeOrders));
    setActivePrefixByGroup({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedItem?.item, editMode, itemToReplace?.uniqueId]);

  useEffect(() => {
    if (!selectedItem) {
      setItemDoc(null);
      return;
    }
    setAddonsLoading(true);
    setAddonsError(null);
    db.getDoc('Item', selectedItem.item)
      .then((doc) => setItemDoc(doc as Record<string, unknown>))
      .catch(() => {
        setAddonsError('Failed to fetch item details');
        setItemDoc(null);
      })
      .finally(() => setAddonsLoading(false));
  }, [selectedItem]);

  useEffect(() => {
    if (!editMode && selectedItem) {
      if (isConfigurable) {
        setQuantity('1');
      } else if (existingCartItem) {
        setQuantity(existingCartItem.quantity.toString());
        setComments(existingCartItem.comment || '');
      } else {
        setQuantity(getItemQuantityFromCart(selectedItem).toString());
      }
    }
  }, [selectedItem, editMode, isConfigurable, getItemQuantityFromCart, existingCartItem]);

  const addonDetails = useMemo(() => {
    const rows = Array.isArray(itemDoc?.custom_pos_add_on_items)
      ? (itemDoc.custom_pos_add_on_items as Array<{ item: string }>)
      : [];
    return rows
      .map((entry) => {
        const menuAddon = menuItems.find((menuItem) => menuItem.item === entry.item);
        return menuAddon
          ? { id: menuAddon.item, name: menuAddon.item_name, price: Number(menuAddon.price) }
          : { id: entry.item, name: entry.item, price: 0 };
      })
      .filter(Boolean);
  }, [itemDoc, menuItems]);

  const variantDetails = useMemo(() => {
    const rows = Array.isArray(itemDoc?.custom_pos_item_variants)
      ? (itemDoc.custom_pos_item_variants as Array<{ item: string }>)
      : [];
    return rows.map((entry) => {
      const menuVariant = menuItems.find((menuItem) => menuItem.item === entry.item);
      return menuVariant
        ? { id: menuVariant.item, name: menuVariant.item_name, price: Number(menuVariant.price) }
        : { id: entry.item, name: entry.item, price: 0 };
    });
  }, [itemDoc, menuItems]);

  const missingRequiredGroups = useMemo(() => {
    const missing: string[] = [];
    for (const grp of modifierGroups) {
      const minReq = Math.max(grp.min_selections || 0, grp.required ? 1 : 0);
      const count = (selectedByGroup[grp.name] || []).length;
      if (count < minReq) {
        missing.push(grp.display_title || grp.name);
      }
    }
    return missing;
  }, [modifierGroups, selectedByGroup]);

  const modifiersUnitTotal = useMemo(() => {
    let sum = 0;
    for (const grp of modifierGroups) {
      const chosen = selectedByGroup[grp.name] || [];
      for (const entry of chosen) {
        const opt = grp.options.find((o) => o.option_code === entry.optionCode);
        if (!opt) continue;
        if (opt.price_mode === 'Menu Item Price') {
          sum += Number(opt.unit_price || 0) * (opt.qty || 1);
        } else if (opt.price_mode === 'Combo Upcharge') {
          sum += Number(opt.unit_price ?? opt.upcharge_amount ?? 0) * (opt.qty || 1);
        }
      }
    }
    return sum;
  }, [modifierGroups, selectedByGroup]);

  const modifierGroupViews: ModifierGroupView[] = useMemo(() => {
    return modifierGroups.map((grp) => {
      const chosen = selectedByGroup[grp.name] || [];
      const chosenByCode = new Map(chosen.map((c) => [c.optionCode, c]));
      return {
        name: grp.name,
        displayTitle: grp.display_title || grp.name,
        groupType: grp.group_type,
        required: grp.required || (grp.min_selections || 0) > 0,
        minSelections: Math.max(grp.min_selections || 0, grp.required ? 1 : 0),
        maxSelections: grp.max_selections || 0,
        allowPrefixes: grp.allow_prefixes,
        allowedPrefixes: grp.allowed_prefixes || [],
        activePrefix: activePrefixByGroup[grp.name] || null,
        selectedCount: chosen.length,
        options: grp.options.map((opt) => {
          const picked = chosenByCode.get(opt.option_code);
          const extraPrice =
            opt.price_mode === 'Combo Upcharge'
              ? Number(opt.unit_price ?? opt.upcharge_amount ?? 0)
              : opt.price_mode === 'Menu Item Price'
                ? Number(opt.unit_price || 0)
                : 0;
          return {
            optionCode: opt.option_code,
            label: opt.label,
            prefix: picked?.prefix || null,
            priceLabel: extraPrice > 0 ? `+${formatCurrency(extraPrice, activeCurrency)}` : null,
            selected: Boolean(picked),
            isDefault: Boolean(opt.is_default),
            available: opt.available !== false,
          };
        }),
      };
    });
  }, [modifierGroups, selectedByGroup, activePrefixByGroup, activeCurrency]);

  if (!selectedItem) return null;

  const basePrice = Number(selectedItem.price) || 0;
  const numericQuantity = quantity === '' ? 0 : parseFloat(quantity);
  const addonsTotal = selectedAddons.reduce((sum, addon) => sum + addon.price, 0);
  const total = (basePrice + addonsTotal + modifiersUnitTotal) * numericQuantity;

  const handleQuantityChange = (value: string) => {
    if (!/^\d*\.?\d*$/.test(value)) return;
    setQuantity(value);
  };

  const handleClose = () => {
    setSelectedItem(null);
    onClose();
  };

  const handleSelectModifierPrefix = (groupName: string, prefix: string | null) => {
    setActivePrefixByGroup((prev) => ({ ...prev, [groupName]: prefix }));
  };

  const handleToggleModifierOption = (groupName: string, optionCode: string) => {
    const grp = modifierGroups.find((g) => g.name === groupName);
    if (!grp) return;
    const opt = grp.options.find((o) => o.option_code === optionCode);
    if (!opt || opt.available === false) return;

    const activePrefix = grp.allow_prefixes ? activePrefixByGroup[groupName] || null : null;
    const minReq = Math.max(grp.min_selections || 0, grp.required ? 1 : 0);
    const maxSel = grp.max_selections || 0;

    setSelectedByGroup((prev) => {
      const current = prev[groupName] || [];
      const existingIdx = current.findIndex((c) => c.optionCode === optionCode);

      if (maxSel === 1) {
        if (existingIdx !== -1) {
          if (current[existingIdx].prefix !== activePrefix) {
            return { ...prev, [groupName]: [{ optionCode, prefix: activePrefix }] };
          }
          if (minReq === 0) {
            return { ...prev, [groupName]: [] };
          }
          return prev;
        }
        return { ...prev, [groupName]: [{ optionCode, prefix: activePrefix }] };
      }

      if (existingIdx !== -1) {
        if (current[existingIdx].prefix !== activePrefix) {
          const updated = [...current];
          updated[existingIdx] = { optionCode, prefix: activePrefix };
          return { ...prev, [groupName]: updated };
        }
        return { ...prev, [groupName]: current.filter((_, i) => i !== existingIdx) };
      }

      if (maxSel > 0 && current.length >= maxSel) {
        return prev;
      }

      return { ...prev, [groupName]: [...current, { optionCode, prefix: activePrefix }] };
    });
  };

  const handleAddToOrder = () => {
    if (isNaN(numericQuantity) || numericQuantity <= 0 || missingRequiredGroups.length > 0) return;

    if (modifierGroups.length > 0) {
      const parentKey = (editMode && itemToReplace?.reservationLineKey) || uuidv4();
      const unpricedMods: SelectedModifier[] = [];
      const bundleOverrides: BundleOverride[] = [];
      const childRows: OrderItem[] = [];
      const isCombo = Boolean(selectedItem.is_combo_meal || catalogMatch?.is_combo_meal);

      for (const grp of modifierGroups) {
        const chosen = selectedByGroup[grp.name] || [];
        for (const sel of chosen) {
          const opt = grp.options.find((o) => o.option_code === sel.optionCode);
          if (!opt) continue;

          if (grp.group_type === 'Combo Choice' || opt.replaces_bundle_item) {
            const chosenItemCode = opt.item || opt.option_code;
            bundleOverrides.push({
              group: grp.name,
              slot_item: opt.replaces_bundle_item || '',
              chosen_item: chosenItemCode,
              option_code: opt.option_code,
              label: opt.label,
              qty: opt.qty || 1,
              inherit_parent_routing: grp.inherit_parent_routing ? 1 : 0,
              hide_on_receipt: grp.hide_unpriced_on_receipt ? 1 : 0,
            });
            const upcharge = Number(opt.unit_price ?? opt.upcharge_amount ?? 0);
            if (opt.price_mode === 'Combo Upcharge' && upcharge > 0) {
              const feeItemCode = opt.upcharge_item || chosenItemCode;
              childRows.push({
                id: feeItemCode,
                item: feeItemCode,
                name: opt.label,
                item_name: opt.label,
                image: null,
                price: upcharge,
                quantity: numericQuantity * (opt.qty || 1),
                course: selectedItem.course || '',
                special_dish: 0,
                tax_rate: 0,
                parentLineKey: parentKey,
                isModifier: true,
                lineRole: 'Combo Upcharge',
                modifierGroup: grp.name,
                modifierOption: opt.option_code,
                modifierPrefix: sel.prefix || null,
                inheritParentRouting: grp.inherit_parent_routing,
                hideUnpricedOnReceipt: false,
              });
            }
          } else if (opt.price_mode === 'Menu Item Price') {
            const modItemCode = opt.item || opt.option_code;
            childRows.push({
              id: modItemCode,
              item: modItemCode,
              name: opt.label,
              item_name: opt.label,
              image: null,
              price: Number(opt.unit_price || 0),
              quantity: numericQuantity * (opt.qty || 1),
              course: selectedItem.course || '',
              special_dish: 0,
              tax_rate: 0,
              parentLineKey: parentKey,
              isModifier: true,
              lineRole: 'Modifier',
              modifierGroup: grp.name,
              modifierOption: opt.option_code,
              modifierPrefix: sel.prefix || null,
              inheritParentRouting: grp.inherit_parent_routing,
              hideUnpricedOnReceipt: false,
            });
          } else {
            // Unpriced Prep: stored on parent ONLY — ZERO extra invoice rows (EFRIS safe!)
            unpricedMods.push({
              group: grp.name,
              option_code: opt.option_code,
              label: opt.label,
              prefix: sel.prefix || null,
              price_mode: opt.price_mode,
              hide_on_receipt: grp.hide_unpriced_on_receipt ? 1 : 0,
            });
          }
        }
      }

      const parentRow: OrderItem = {
        ...selectedItem,
        modifier_groups: modifierGroups,
        quantity: numericQuantity,
        price: basePrice,
        uniqueId: (editMode && itemToReplace?.uniqueId) || parentKey,
        reservationLineKey: parentKey,
        ...(editMode && itemToReplace?.invoiceItemName
          ? { invoiceItemName: itemToReplace.invoiceItemName }
          : {}),
        comment: comments || undefined,
        isModifier: false,
        lineRole: isCombo ? 'Combo Header' : 'Standard',
        selectedModifiers: unpricedMods,
        bundleOverrides,
        hideUnpricedOnReceipt: unpricedMods.some((m) => m.hide_on_receipt === 1),
      };

      void addConfiguredItems(parentRow, childRows, editMode ? itemToReplace?.uniqueId : undefined);
      handleClose();
      return;
    }

    if (editMode && itemToReplace?.uniqueId) removeFromOrder(itemToReplace.uniqueId);

    addToOrder({
      ...selectedItem,
      quantity: numericQuantity,
      price: basePrice,
      comment: comments || undefined,
    });

    selectedAddons.forEach((addon) => {
      const menuAddon = menuItems.find((item) => item.item === addon.id);
      if (menuAddon) {
        addToOrder({ ...menuAddon, quantity: numericQuantity, price: addon.price });
      } else {
        addToOrder({
          id: addon.id,
          name: addon.name,
          price: addon.price,
          quantity: numericQuantity,
          image: null,
          item: addon.id,
          item_name: addon.name,
          course: '',
          description: '',
          special_dish: 0,
          tax_rate: 0,
        } as OrderItem);
      }
    });

    handleClose();
  };

  return (
    <ProductConfigurator
      open
      onOpenChange={(next) => {
        if (!next) handleClose();
      }}
      itemName={selectedItem.item_name}
      itemCode={selectedItem.item}
      courseLabel={selectedItem.course_label || selectedItem.course}
      imageUrl={(itemDoc?.image as string | undefined) || selectedItem.image}
      quantity={quantity}
      onQuantityChange={handleQuantityChange}
      onQuantityBlur={() => {
        if (quantity === '') setQuantity('0');
      }}
      onIncrement={() => {
        const current = quantity === '' ? 0 : parseFloat(quantity);
        if (current < 99) setQuantity(String(Math.round((current + 1) * 1000) / 1000));
      }}
      onDecrement={() => {
        const current = quantity === '' ? 0 : parseFloat(quantity);
        if (current > 0) setQuantity(String(Math.round((current - 1) * 1000) / 1000));
      }}
      comments={comments}
      onCommentsChange={setComments}
      variants={variantDetails.map((v) => ({
        id: v.id,
        name: v.name,
        priceLabel: formatCurrency(v.price, activeCurrency),
      }))}
      selectedVariantId={(itemDoc?.item as string | undefined) || selectedItem.item}
      onSelectVariant={(id) => {
        const menuVariant = menuItems.find((m) => m.item === id);
        if (menuVariant) setSelectedItem(menuVariant);
      }}
      addons={addonDetails.map((a) => ({
        id: a.id,
        name: a.name,
        priceLabel: formatCurrency(a.price, activeCurrency),
      }))}
      selectedAddonIds={selectedAddons.map((a) => a.id)}
      onToggleAddon={(id) => {
        const addon = addonDetails.find((a) => a.id === id);
        if (!addon) return;
        setSelectedAddons((current) =>
          current.some((item) => item.id === id)
            ? current.filter((item) => item.id !== id)
            : [...current, addon]
        );
      }}
      addonsLoading={addonsLoading}
      addonsError={addonsError}
      modifierGroups={modifierGroupViews}
      onSelectModifierPrefix={handleSelectModifierPrefix}
      onToggleModifierOption={handleToggleModifierOption}
      missingRequiredGroups={missingRequiredGroups}
      totalLabel={formatCurrency(total, activeCurrency)}
      submitDisabled={numericQuantity <= 0 || missingRequiredGroups.length > 0}
      onSubmit={handleAddToOrder}
      labels={{
        specialInstructions: t('product_dialog.special_instructions'),
        specialInstructionsPlaceholder: t('product_dialog.special_instructions_placeholder'),
        quantity: t('product_dialog.quantity'),
        variants: t('product_dialog.variants'),
        addons: t('product_dialog.addons'),
        loadingAddons: t('product_dialog.loading_addons'),
        noAddons: t('product_dialog.no_addons'),
        total: t('product_dialog.total'),
        submit:
          editMode || existingCartItem
            ? t('product_dialog.update_order')
            : t('product_dialog.add_to_order'),
        requiredBadge: t('product_dialog.required_badge'),
        optionalBadge: t('product_dialog.optional_badge'),
        includedBadge: t('product_dialog.included_badge'),
        soldOutBadge: t('product_dialog.sold_out_badge'),
        regularPrefix: t('product_dialog.regular_prefix'),
        prefixStripLabel: t('product_dialog.prefix_strip_label'),
        selectRequiredPrompt: t('product_dialog.select_required_prompt'),
      }}
    />
  );
};

export default ProductDialog;
