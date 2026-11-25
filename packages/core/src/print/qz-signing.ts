import { prepareQzSigning, type QzBridge } from './qz-runtime.ts';

type PostSignature = (method: string, args: { toSign: string }) => Promise<{ message?: unknown }>;

export class QzSigningRefusedError extends Error {
  constructor() {
    super('Printer signing was refused. Check your printing permissions and try again.');
    this.name = 'QzSigningRefusedError';
  }
}

export function configureQzSignature(
  bridge: QzBridge,
  post: PostSignature,
  onRefusal?: (error: QzSigningRefusedError) => void,
): void {
  bridge.security.setSignatureAlgorithm('SHA512');
  bridge.security.setSignaturePromise((toSign) => (resolve, reject) => {
    void Promise.resolve()
      .then(() => post('ury.ury.api.ury_print.signature_promise', { toSign }))
      .then(({ message }) => {
        if (typeof message !== 'string' || !message.trim()) {
          throw new Error('QZ signature is unavailable');
        }
        return message;
      })
      .then(resolve, () => {
        const error = new QzSigningRefusedError();
        onRefusal?.(error);
        reject(error);
      });
  });
}

const pending = new WeakMap<QzBridge, Promise<void>>();

export function runSignedQz<T>(
  bridge: QzBridge,
  post: PostSignature,
  operation: () => Promise<T>,
): Promise<T> {
  let closing: Promise<void> | undefined;
  const result = (pending.get(bridge) ?? Promise.resolve()).then(async () => {
    prepareQzSigning(bridge);
    let active = true;
    let refuse!: (error: QzSigningRefusedError) => void;
    const refusal = new Promise<never>((_, reject) => { refuse = reject; });

    configureQzSignature(bridge, post, (error) => {
      if (!active) return;
      active = false;
      refuse(error);
      // The older bridge leaves its socket request pending after the signer
      // rejects. Closing it drains that request before another job reconnects.
      closing = Promise.resolve()
        .then(() => bridge.websocket.isActive() ? bridge.websocket.disconnect() : undefined)
        .catch(() => undefined);
    });

    try {
      return await Promise.race([refusal, Promise.resolve().then(operation)]);
    } finally {
      active = false;
    }
  });
  // QZ's signer belongs to the whole bridge, so only one operation can own it.
  pending.set(bridge, result.then(() => undefined, () => closing));
  return result;
}
