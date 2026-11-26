import type { QzBridge } from '../../../packages/core/src/print/qz-runtime';

type PostSignature = (method: string, args: { toSign: string }) => Promise<{ message?: unknown }>;

export class QzSigningRefusedError extends Error {
  constructor();
}

export function configureQzSignature(
  bridge: QzBridge,
  post: PostSignature,
  onRefusal?: (error: QzSigningRefusedError) => void,
): void;

export function runSignedQz<T>(
  bridge: QzBridge,
  post: PostSignature,
  operation: () => Promise<T>,
  signal?: AbortSignal,
): Promise<T>;

export function withQzDeadline<T>(operation: (signal: AbortSignal, startPrinting: () => void) => Promise<T>): Promise<T>;

export function connectQz(bridge: QzBridge, certificate: () => Promise<string>, host?: string): Promise<void>;
