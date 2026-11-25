type QzCallback = (resolve: (value: string) => void, reject: (reason?: unknown) => void) => void;

type PromiseExecutor<T> = (resolve: (value: T | PromiseLike<T>) => void, reject: (reason?: unknown) => void) => void;

export interface QzBridge {
  version?: string;
  api?: { setPromiseType(createPromise: <T>(executor: PromiseExecutor<T>) => Promise<T>): void };
  security: {
    setCertificatePromise(callback: QzCallback): void;
    setSignatureAlgorithm(algorithm: 'SHA512'): void;
    setSignaturePromise(callback: (message: string) => QzCallback): void;
  };
  websocket: {
    isActive(): boolean;
    connect(options: { host: string; usingSecure: boolean }): Promise<void>;
    disconnect(): Promise<void>;
  };
  printers: { getDefault(): Promise<string> };
  configs: { create(printer: string | { host: string; port: number }): unknown };
  print(
    config: unknown,
    data: { type: string; format: string; flavor?: string; data: string; options?: Record<string, unknown> }[],
  ): Promise<void>;
}

declare global {
  interface Window { qz?: QzBridge }
}

// QZ 2.2.3 detaches its websocket signing continuation. Observe those QZ
// chains while preserving the rejection for any consumer that awaits them.
class QzPromise<T> extends Promise<T> {
  override then<TResult1 = T, TResult2 = never>(
    onfulfilled?: ((value: T) => TResult1 | PromiseLike<TResult1>) | null,
    onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): Promise<TResult1 | TResult2> {
    const next = super.then(onfulfilled, onrejected);
    void Promise.prototype.then.call(next, undefined, () => undefined);
    return next;
  }
}

const prepared = new WeakSet<QzBridge>();

export function prepareQzSigning(bridge: QzBridge): void {
  if ((bridge.version === '2.2.3' || bridge.version === '2.2.3-SNAPSHOT')
      && bridge.api && !prepared.has(bridge)) {
    bridge.api.setPromiseType((executor) => new QzPromise(executor));
    prepared.add(bridge);
  }
}

let loading: Promise<QzBridge> | undefined;

export function loadQz(): Promise<QzBridge> {
  if (window.qz) return Promise.resolve(window.qz);
  if (!loading) {
    loading = new Promise<QzBridge>((resolve, reject) => {
      const script = document.createElement('script');
      const fail = () => { script.remove(); reject(new Error('QZ_UNAVAILABLE: Print helper did not load')); };
      script.src = '/assets/frappe/node_modules/qz-tray/qz-tray.js';
      script.onload = () => window.qz ? resolve(window.qz) : fail();
      script.onerror = fail;
      document.head.appendChild(script);
    }).finally(() => { loading = undefined; });
  }
  return loading;
}
