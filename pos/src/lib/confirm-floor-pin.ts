import { call } from '@ury/core';

export interface FloorPinProvider {
  mode: 'identity_provider';
  start: string;
  purpose: 'floor_pin';
  label: string;
}

export function confirmFloorPin(provider: FloorPinProvider, signal: AbortSignal): Promise<void> {
  const popup = window.open('about:blank', '_blank', 'popup,width=520,height=680');
  if (!popup) return Promise.reject(new Error('Popup blocked'));
  return new Promise((resolve, reject) => {
    let navigated = false;
    let finished = false;
    function finish(ok: boolean) {
      if (finished) return;
      finished = true;
      window.clearInterval(timer);
      window.removeEventListener('message', receive);
      signal.removeEventListener('abort', cancel);
      popup!.close();
      if (ok) resolve();
      else reject(new Error('Confirmation incomplete'));
    }
    function cancel() { finish(false); }
    function receive(event: MessageEvent) {
      if (navigated && event.source === popup && event.origin === window.location.origin &&
          event.data?.type === 'siteops-reauth' && event.data?.purpose === 'floor_pin') {
        finish(event.data.ok === true);
      }
    }
    const timer = window.setInterval(() => { if (popup.closed) cancel(); }, 400);
    window.addEventListener('message', receive);
    signal.addEventListener('abort', cancel, { once: true });
    if (signal.aborted || provider.purpose !== 'floor_pin' || !/^[a-z_]\w*(\.[a-z_]\w*)+$/i.test(provider.start)) {
      cancel();
      return;
    }
    void call.post(provider.start, { purpose: 'floor_pin' }).then(({ message }) => {
      if (finished) return;
      if (!message?.authorize_url) return cancel();
      const url = new URL(message.authorize_url, window.location.origin);
      if (url.protocol !== 'https:' && !(url.protocol === 'http:' && url.origin === window.location.origin)) return cancel();
      popup.location = url.href;
      navigated = true;
    }).catch(cancel);
  });
}
