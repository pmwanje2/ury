import { useEffect, useMemo, useRef, useState } from 'react';
import {
  previewOrderTotals,
  type OrderTotalsPreview,
  type PreviewOrderTotalsRequest,
} from './order-api';

interface UseOrderPreviewOptions {
  enabled: boolean;
  offline?: boolean;
  debounceMs?: number;
  posProfile?: string | null;
  orderType?: string | null;
  customer?: string | null;
  items?: Array<{
    name?: string;
    item: string;
    qty: number;
    rate?: number;
    item_name?: string;
    reservation_line_key?: string;
    custom_parent_line_key?: string;
    custom_line_role?: 'Standard' | 'Modifier' | 'Combo Header' | 'Combo Upcharge';
    custom_is_modifier?: number;
    custom_modifier_prefix?: string;
    custom_modifier_group?: string;
    custom_modifier_option?: string;
    custom_modifiers_json?: string;
    custom_bundle_overrides_json?: string;
    custom_inherit_parent_routing?: number;
    custom_hide_unpriced_on_receipt?: number;
  }>;
  noOfPax?: number;
  invoiceName?: string | null;
  additionalDiscountPercentage?: number;
  couponCode?: string | null;
  room?: string | null;
  table?: string | null;
}

export function useOrderPreview({
  enabled,
  offline = false,
  debounceMs = 150,
  posProfile,
  orderType,
  customer,
  items,
  noOfPax = 0,
  invoiceName,
  additionalDiscountPercentage = 0,
  couponCode,
  room,
  table,
}: UseOrderPreviewOptions) {
  const [preview, setPreview] = useState<OrderTotalsPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const itemsKey = useMemo(
    () =>
      JSON.stringify(
        (items || []).map((r) => [
          r.name ?? null,
          r.reservation_line_key ?? null,
          r.item,
          r.qty,
          r.rate ?? null,
          r.custom_parent_line_key ?? null,
          r.custom_line_role ?? null,
          r.custom_is_modifier ?? 0,
          r.custom_modifier_group ?? null,
          r.custom_modifier_option ?? null,
          r.custom_modifier_prefix ?? null,
          r.custom_modifiers_json ?? null,
          r.custom_bundle_overrides_json ?? null,
        ]),
      ),
    [items],
  );

  useEffect(() => {
    if (!enabled || offline || !posProfile || !orderType) {
      seqRef.current += 1;
      setLoading(false);
      if (!enabled || offline) {
        setPreview(null);
        setError(null);
      }
      return;
    }

    const requestSeq = ++seqRef.current;
    const timer = window.setTimeout(async () => {
      setLoading(true);
      setError(null);
      try {
        const payload: PreviewOrderTotalsRequest = {
          pos_profile: posProfile,
          order_type: orderType,
          ...(customer ? { customer } : {}),
          ...(items && items.length > 0 ? { items } : {}),
          ...(noOfPax ? { no_of_pax: noOfPax } : {}),
          ...(invoiceName ? { invoice_name: invoiceName } : {}),
          ...(additionalDiscountPercentage > 0
            ? { additional_discount_percentage: additionalDiscountPercentage }
            : {}),
          ...(couponCode ? { coupon_code: couponCode } : {}),
          ...(room ? { room } : {}),
          ...(table ? { table } : {}),
        };
        const data = await previewOrderTotals(payload);
        if (seqRef.current === requestSeq) {
          setPreview(data ?? null);
          setLoading(false);
        }
      } catch (err) {
        if (seqRef.current === requestSeq) {
          setLoading(false);
          setError(err instanceof Error ? err.message : 'Preview unavailable');
        }
      }
    }, debounceMs);

    return () => {
      window.clearTimeout(timer);
    };
  }, [
    enabled,
    offline,
    debounceMs,
    posProfile,
    orderType,
    customer,
    itemsKey,
    noOfPax,
    invoiceName,
    additionalDiscountPercentage,
    couponCode,
    room,
    table,
  ]);

  return { preview, loading, error };
}
