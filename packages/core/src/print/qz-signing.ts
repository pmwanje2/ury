import type { QzBridge } from './qz-runtime';

type PostSignature = (method: string, args: { toSign: string }) => Promise<{ message?: unknown }>;

export function configureQzSignature(bridge: QzBridge, post: PostSignature): void {
  bridge.security.setSignatureAlgorithm('SHA512');
  bridge.security.setSignaturePromise((toSign) => (resolve, reject) => {
    post('ury.ury.api.ury_print.signature_promise', { toSign })
      .then(({ message }) => {
        if (typeof message !== 'string' || !message.trim()) {
          throw new Error('QZ signature is unavailable');
        }
        return message;
      })
      .then(resolve, reject);
  });
}
