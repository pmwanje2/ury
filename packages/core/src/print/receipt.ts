export { prepareReceipt, completeReceipt, dispatchReceipt } from '../../../../ury/public/js/receipt-delivery.js';

export async function confirmBrowserDelivery(url: string, beforeNavigate?: () => Promise<void>): Promise<void> {
  const initialUrl = beforeNavigate ? 'about:blank' : url;
  let popup = window.open(initialUrl, '_blank');
  if (!popup) {
    // The QZ attempt may have outlived browser user activation. An actual click
    // opens printview without repeating the receipt authorization.
    popup = await new Promise<Window>((resolve, reject) => {
      const dialog = document.createElement('dialog');
      const open = document.createElement('button');
      open.textContent = 'Open receipt for browser printing';
      const cancel = document.createElement('button');
      cancel.textContent = 'Cancel';
      dialog.append(open, cancel);
      document.body.append(dialog);
      const dismiss = () => {
        dialog.remove();
        reject(new Error('Browser printing was cancelled.'));
      };
      cancel.onclick = dismiss;
      dialog.oncancel = dismiss;
      open.onclick = () => {
        const receipt = window.open(initialUrl, '_blank');
        if (!receipt) {
          open.textContent = 'Allow pop-ups, then open receipt';
          return;
        }
        dialog.remove();
        resolve(receipt);
      };
      dialog.showModal();
    });
  }
  popup.opener = null;
  if (beforeNavigate) {
    try {
      await beforeNavigate();
      popup.location.replace(url);
    } catch (error) {
      popup.close();
      throw error;
    }
  }
  // Frappe closes printview after the native dialog. Browsers cannot report
  // whether the operator printed or cancelled, so confirm the physical receipt.
  while (!popup.closed) await new Promise(resolve => setTimeout(resolve, 300));
  if (!window.confirm('Did the browser print the receipt?')) throw new Error('Receipt delivery was not confirmed.');
}

export function assertNetworkAcknowledgement(result?: string | {status?: string; message?: string} | null) {
  if (result === 'Success' || (typeof result === 'object' && result?.status === 'Success')) return;
  throw new Error((typeof result === 'string' ? result : result?.message) || 'The printer did not acknowledge the job.');
}
