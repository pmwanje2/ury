// Proxy/gateway answers that mean the ERP link is down. 4xx and 500 are
// the application's own answer and are never in this set.
const GATEWAY_UNREACHABLE = new Set([502, 503, 504, 520, 521, 522, 523, 524]);

export function isGatewayUnreachableStatus(status: unknown): boolean {
  return typeof status === 'number' && Number.isInteger(status) && GATEWAY_UNREACHABLE.has(status);
}

// frappe-js-sdk copies the HTTP status onto httpStatus. Axios keeps it on
// response.status. A bare status field can be a Frappe body value, so it is ignored.
export function gatewayStatusOf(error: unknown): number | null {
  if (!error || typeof error !== 'object') return null;
  const record = error as { httpStatus?: unknown; response?: { status?: unknown } };
  for (const value of [record.httpStatus, record.response?.status]) {
    if (isGatewayUnreachableStatus(value)) return value as number;
  }
  return null;
}
