export function isKotLate(minutes, warningMinutes) {
  const threshold = Number(warningMinutes);
  return Number.isFinite(threshold) && threshold > 0 && minutes >= threshold;
}

export function normalizeRecallWindowMinutes(windowMinutes) {
  const parsed = Number(windowMinutes);
  if (!Number.isFinite(parsed) || parsed <= 0) return 15;
  return Math.max(1, Math.min(240, Math.floor(parsed)));
}

export function canRecallKot(kot, now, windowMinutes = 15) {
  if (kot.order_status !== "Served" || kot.production_time == null || kot.production_time === "") {
    return false;
  }
  const productionMinutes = Number(kot.production_time);
  const creation = new Date(kot.creation.replace(" ", "T")).getTime();
  const elapsed = now.getTime() - (creation + productionMinutes * 60000);
  const limitMs = normalizeRecallWindowMinutes(kot.recall_window_minutes ?? windowMinutes) * 60000;
  return Number.isFinite(productionMinutes) && productionMinutes >= 0 && elapsed >= 0 && elapsed <= limitMs;
}

export function sortKotsByPriority(kots) {
  return [...(kots || [])].sort((a, b) => {
    const aRush = Number(a?.custom_is_rush) === 1 ? 0 : 1;
    const bRush = Number(b?.custom_is_rush) === 1 ? 0 : 1;
    if (aRush !== bRush) return aRush - bRush;
    const aVip = Number(a?.custom_is_vip) === 1 ? 0 : 1;
    const bVip = Number(b?.custom_is_vip) === 1 ? 0 : 1;
    return aVip - bVip;
  });
}

export function groupKotItemsByCourse(items) {
  const sorted = [...(items || [])].sort(
    (a, b) => (Number(a?.serve_priority) || 0) - (Number(b?.serve_priority) || 0),
  );
  const groups = [];
  const index = new Map();
  for (const item of sorted) {
    const course = String(item?.course || "").trim();
    const prio = Number(item?.serve_priority) || 0;
    const key = `${prio}::${course}`;
    if (!index.has(key)) {
      const entry = { course, serve_priority: prio, items: [] };
      index.set(key, entry);
      groups.push(entry);
    }
    index.get(key).items.push(item);
  }
  return groups;
}

export function buildAllDaySummary(kots) {
  const totals = new Map();
  for (const kot of kots || []) {
    if (kot?.type === "Cancelled" || kot?.type === "Partially cancelled") continue;
    const isRush = Number(kot?.custom_is_rush) === 1;
    const isVip = Number(kot?.custom_is_vip) === 1;
    for (const item of kot?.kot_items || []) {
      if (item?.state === "SERVED") continue;
      const rawQty = item?.qty !== undefined ? Number(item.qty) : Number(item?.quantity) - Number(item?.cancelled_qty || 0);
      if (!Number.isFinite(rawQty) || rawQty <= 0) continue;
      const itemName = String(item?.item_name || item?.item || "").trim();
      if (!itemName) continue;
      const course = String(item?.course || "").trim();
      const key = `${itemName}::${course}`;
      if (!totals.has(key)) {
        totals.set(key, { item_name: itemName, course, total_qty: 0, rush_qty: 0, vip_qty: 0, tickets: new Set() });
      }
      const entry = totals.get(key);
      entry.total_qty = Math.round((entry.total_qty + rawQty) * 1e4) / 1e4;
      if (isRush) entry.rush_qty = Math.round((entry.rush_qty + rawQty) * 1e4) / 1e4;
      if (isVip) entry.vip_qty = Math.round((entry.vip_qty + rawQty) * 1e4) / 1e4;
      if (kot?.name) entry.tickets.add(kot.name);
    }
  }
  return [...totals.values()]
    .map(({ tickets, ...rest }) => ({ ...rest, ticket_count: tickets.size }))
    .sort((a, b) => (b.rush_qty - a.rush_qty) || (b.total_qty - a.total_qty) || a.item_name.localeCompare(b.item_name));
}

export function buildExpoOrders(kots) {
  const byInvoice = new Map();
  for (const kot of kots || []) {
    if (kot?.type === "Cancelled" || kot?.type === "Partially cancelled") continue;
    const invoice = kot?.invoice || kot?.name;
    if (!invoice) continue;
    if (!byInvoice.has(invoice)) {
      byInvoice.set(invoice, {
        invoice,
        restaurant_table: kot.restaurant_table,
        table_takeaway: kot.table_takeaway,
        tableortakeaway: kot.tableortakeaway,
        custom_merged_tables: kot.custom_merged_tables,
        custom_guest_label: kot.custom_guest_label,
        order_no: kot.order_no,
        user: kot.user,
        time: kot.time,
        timeRemaining: kot.timeRemaining,
        late: Boolean(kot.late),
        custom_is_rush: 0,
        custom_is_vip: 0,
        stations: [],
        active_kot_names: [],
      });
    }
    const order = byInvoice.get(invoice);
    if (Number(kot.custom_is_rush) === 1) order.custom_is_rush = 1;
    if (Number(kot.custom_is_vip) === 1) order.custom_is_vip = 1;
    if (kot.late) order.late = true;

    const items = kot.kot_items || [];
    const states = items.map(it => String(it?.state || "QUEUED").toUpperCase());
    const readyCount = states.filter(st => st === "READY" || st === "SERVED").length;
    let status = "Queued";
    let allItemsReady = false;
    if (kot.order_status === "Served") {
      status = "Served";
      allItemsReady = true;
    } else if (!items.length || readyCount === items.length) {
      status = "Ready";
      allItemsReady = true;
    } else if (states.some(st => st === "IN_PREPARATION" || st === "READY" || st === "SERVED")) {
      status = "Preparing";
    }
    order.stations.push({
      kot_name: kot.name,
      production: kot.production || "",
      order_status: kot.order_status,
      status,
      all_items_ready: allItemsReady,
      ready_items: readyCount,
      total_items: items.length,
      kot_items: items,
      course_groups: groupKotItemsByCourse(items),
    });
    if (kot.order_status !== "Served" && kot.name) {
      order.active_kot_names.push(kot.name);
    }
  }
  const orders = [...byInvoice.values()].map(order => ({
    ...order,
    all_stations_ready: order.stations.length > 0 && order.stations.every(st => st.all_items_ready),
  }));
  return sortKotsByPriority(orders);
}

export function kitchenErrorMessage(error, fallback) {
  const data = error?.response?.data || error;
  let message = data?.message || error?.message;
  if (data?._server_messages) {
    try {
      const messages = JSON.parse(data._server_messages);
      const first = typeof messages[0] === "string" ? JSON.parse(messages[0]) : messages[0];
      message = first?.message || message;
    } catch {
      // A malformed server response should still leave a useful visible error.
    }
  }
  return typeof message === "string" && message.trim()
    ? message.replace(/<[^>]*>/g, "")
    : fallback;
}

export function rememberKot(seen, kot) {
  if (seen.has(kot.name)) return false;
  seen.add(kot.name);
  return true;
}
