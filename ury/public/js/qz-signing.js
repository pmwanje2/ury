// Shared by the Desk script and all bundled print clients.

export class QzSigningRefusedError extends Error {
  constructor() {
    super('Printer signing was refused. Check your printing permissions and try again.');
    this.name = 'QzSigningRefusedError';
  }
}

// QZ 2.2.3 detaches its websocket signing continuation. Observe those QZ
// chains while preserving the rejection for any consumer that awaits them.
class QzPromise extends Promise {
  then(onFulfilled, onRejected) {
    const next = super.then(onFulfilled, onRejected);
    void Promise.prototype.then.call(next, undefined, () => undefined);
    return next;
  }
}

const prepared = new WeakSet();

function prepareQzSigning(bridge) {
  if ((bridge.version === '2.2.3' || bridge.version === '2.2.3-SNAPSHOT')
      && bridge.api && !prepared.has(bridge)) {
    bridge.api.setPromiseType((executor) => new QzPromise(executor));
    prepared.add(bridge);
  }
}

export function configureQzSignature(bridge, post, onRefusal) {
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

const pending = new WeakMap();

export function runSignedQz(bridge, post, operation, signal) {
  let closing;
  const result = (pending.get(bridge) ?? Promise.resolve()).then(async () => {
    signal?.throwIfAborted();
    prepareQzSigning(bridge);

    let active = true;
    let cancelRequest;
    const cancelled = new Promise((_, reject) => { cancelRequest = reject; });

    const cancel = (error) => {
      if (!active) return;
      active = false;
      cancelRequest(error);
      // The pinned bridge leaves its socket request pending on refusal.
      // Close it before the next operation reconnects.
      closing = Promise.resolve()
        .then(() => bridge.websocket.isActive() ? bridge.websocket.disconnect() : undefined)
        .catch(() => undefined);
    };

    configureQzSignature(bridge, async (method, args) => {
      signal?.throwIfAborted();
      const response = await post(method, args);
      // A timed-out job may have been waiting for its signature.
      signal?.throwIfAborted();
      return response;
    }, cancel);

    const abort = () => cancel(signal.reason);
    signal?.addEventListener('abort', abort, { once: true });
    try {
      return await Promise.race([cancelled, Promise.resolve().then(operation)]);
    } finally {
      active = false;
      signal?.removeEventListener('abort', abort);
    }
  });
  // QZ's signer belongs to the whole bridge, so only one operation can own it.
  pending.set(bridge, result.then(() => undefined, () => closing));
  return result;
}

// Let the operator accept trust before starting the spooler's own deadline.
export function withQzDeadline(operation) {
  const controller = new AbortController();
  let timer;
  let startPrinting;
  const deadline = new Promise((_, reject) => {
    const arm = (ms, code) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const error = new Error(code);
        controller.abort(error);
        reject(error);
      }, ms);
    };
    arm(60000, 'QZ_TRUST_TIMEOUT');
    startPrinting = () => arm(30000, 'PRINTER_TIMEOUT');
  });
  return Promise.race([Promise.resolve().then(() => operation(controller.signal, startPrinting)), deadline])
    .finally(() => clearTimeout(timer));
}

const connecting = new WeakMap();

export function connectQz(bridge, certificate, host, signal) {
  signal?.throwIfAborted();
  if (bridge.websocket.isActive()) return Promise.resolve();
  const existing = connecting.get(bridge);
  if (existing) return existing;

  const controller = new AbortController();
  let rejectConnection;
  const cancelled = new Promise((_, reject) => { rejectConnection = reject; });
  function abort(reason) {
    controller.abort(reason);
    if (connecting.get(bridge) === connection) connecting.delete(bridge);
    rejectConnection(reason);
  }
  const onAbort = () => abort(signal.reason);
  const timer = setTimeout(() => abort(new Error('QZ_TRUST_TIMEOUT')), 60000);
  const open = async () => {
    let pem;
    try {
      pem = await certificate(controller.signal);
      if (!pem?.startsWith('-----BEGIN CERTIFICATE-----')) throw Error('Missing public certificate');
    } catch (cause) {
      controller.signal.throwIfAborted();
      throw new Error('QZ_CERT_ERROR', { cause });
    }
    controller.signal.throwIfAborted();
    bridge.security.setCertificatePromise(resolve => resolve(pem), { rejectOnFailure: true });
    return bridge.websocket.connect(host ? { host, usingSecure: true } : { usingSecure: true });
  };
  const connection = Promise.race([Promise.resolve().then(open), cancelled]).finally(() => {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
    if (connecting.get(bridge) === connection) connecting.delete(bridge);
  });
  connecting.set(bridge, connection);
  signal?.addEventListener('abort', onAbort, { once: true });
  return connection;
}
