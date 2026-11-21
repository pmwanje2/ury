export function qtyReductionMessage(item: string, qty: number, cancellations: string[]) {
  const change = qty === 0 ? `${item} removed from the order.` : `${item} reduced to qty ${qty}.`;
  return cancellations.length ? `${change} Cancel-KOT ${cancellations.join(', ')} sent to kitchen.` : change;
}
