type QzCallback = (resolve: (value: string) => void, reject: (reason?: string) => void) => void;

interface QzBridge {
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
  configs: { create(printer: string): unknown };
  print(config: unknown, data: { type: string; format: string; data: string }[]): Promise<void>;
}

declare global {
  interface Window { qz?: QzBridge }
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
