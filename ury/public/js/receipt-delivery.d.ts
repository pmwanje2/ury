type Post = (path: string, args: Record<string, unknown>) => Promise<{ message: unknown }>;
type ReceiptArgs = { invoice: string; print_format?: string | null; letterhead?: string | null;
  approval_token?: string; reason?: string; reason_note?: string };
interface Delivery {
  delivery_id: string;
  invoice_revision: string;
  delivered: boolean;
  html: string;
  print_url: string;
  print_number: number;
  copy_label: string;
}
export function prepareReceipt(post: Post, args: ReceiptArgs): Promise<Delivery>;
export function completeReceipt(post: Post, invoice: string, delivery: Delivery): Promise<void>;
export function dispatchReceipt(post: Post, args: ReceiptArgs, print: (html: string) => Promise<unknown>): Promise<void>;
