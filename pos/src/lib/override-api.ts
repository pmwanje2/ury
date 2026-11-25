import { call, parseFrappeError } from '@ury/core';
import { t } from '../i18n';

export type AuthRefusalCode =
  | 'POS-AUTH-REQUIRED'
  | 'POS-AUTH-SELF'
  | 'POS-AUTH-NOT-ELIGIBLE'
  | 'POS-AUTH-PIN'
  | 'POS-AUTH-PASSWORD'
  | 'POS-AUTH-LOCKED'
  | 'POS-AUTH-NO-PIN'
  | 'POS-AUTH-REASON'
  | 'POS-AUTH-EXPIRED'
  | 'POS-AUTH-CHANGED'
  | 'POS-AUTH-UNAVAILABLE';

export interface OverrideApprover {
  user: string;
  full_name: string;
}

export interface OverrideReason {
  code: string;
  description: string;
  requires_note: number;
  applies_to: string[];
}

export interface PrivilegePolicy {
  privilege: string;
  label: string;
  may_perform: boolean;
  requires_reason: boolean;
}

export interface AuthoriseResponse {
  token: string;
  approver: string;
  approver_name: string;
  expires_in: number;
}

interface PinStatus {
  has_pin: boolean;
  must_reset?: boolean;
  locked?: boolean;
}

export interface MyPinStatus extends PinStatus {
  external_verifier: boolean;
  can_manage_here?: boolean;
}

export interface PinUserStatus extends PinStatus {
  can_clear?: boolean;
  user: string;
  full_name: string;
}

interface ClearPinResponse {
  ok: boolean;
  has_pin: boolean;
  user?: string;
  must_reset?: boolean;
}

const REFUSAL_CODES: readonly AuthRefusalCode[] = [
  'POS-AUTH-REQUIRED',
  'POS-AUTH-SELF',
  'POS-AUTH-NOT-ELIGIBLE',
  'POS-AUTH-PIN',
  'POS-AUTH-PASSWORD',
  'POS-AUTH-LOCKED',
  'POS-AUTH-NO-PIN',
  'POS-AUTH-REASON',
  'POS-AUTH-EXPIRED',
  'POS-AUTH-CHANGED',
  'POS-AUTH-UNAVAILABLE',
];

export function extractAuthErrorCode(errorOrMessage: unknown): AuthRefusalCode | null {
  const raw =
    typeof errorOrMessage === 'string'
      ? errorOrMessage
      : parseFrappeError(errorOrMessage, '') || (errorOrMessage instanceof Error ? errorOrMessage.message : String(errorOrMessage ?? ''));
  if (!raw) return null;
  for (const code of REFUSAL_CODES) {
    if (raw.includes(code)) return code;
  }
  return null;
}

export function isAuthRequiredError(errorOrMessage: unknown): boolean {
  return extractAuthErrorCode(errorOrMessage) === 'POS-AUTH-REQUIRED';
}

export function overrideErrorMessage(error: unknown, fallbackKey = 'manager_approval.error_generic'): string {
  const code = extractAuthErrorCode(error);
  switch (code) {
    case 'POS-AUTH-PIN':
      return t('manager_approval.error_wrong_pin');
    case 'POS-AUTH-PASSWORD':
      return t('approval_pin.error_wrong_password');
    case 'POS-AUTH-LOCKED':
      return t('manager_approval.error_locked');
    case 'POS-AUTH-NO-PIN':
      return t('manager_approval.error_no_pin');
    case 'POS-AUTH-REASON':
      return t('manager_approval.error_reason');
    case 'POS-AUTH-SELF':
      return t('manager_approval.error_self_approval');
    case 'POS-AUTH-NOT-ELIGIBLE':
      return t('manager_approval.error_not_eligible');
    case 'POS-AUTH-EXPIRED':
      return t('manager_approval.error_expired');
    case 'POS-AUTH-CHANGED':
      return t('manager_approval.error_changed');
    case 'POS-AUTH-REQUIRED':
      return t('manager_approval.error_required');
    case 'POS-AUTH-UNAVAILABLE':
      return t('manager_approval.error_unavailable');
    default:
      return t(fallbackKey);
  }
}

// Business actions preserve their server validation messages after handling approval refusals.
export function overrideActionErrorMessage(error: unknown, fallbackKey = 'manager_approval.error_generic'): string {
  if (extractAuthErrorCode(error)) return overrideErrorMessage(error, fallbackKey);
  return parseFrappeError(error, '') || t(fallbackKey);
}

export async function listApprovers(params: {
  posProfile: string;
  privilege: string;
  referenceName?: string;
  percent?: number;
  amount?: number;
  privilegeGroup?: number;
}): Promise<OverrideApprover[]> {
  const query: Record<string, unknown> = {
    pos_profile: params.posProfile,
    privilege: params.privilege,
  };
  if (params.referenceName) query.reference_name = params.referenceName;
  if (params.percent !== undefined) query.percent = params.percent;
  if (params.amount !== undefined) query.amount = params.amount;
  if (params.privilegeGroup !== undefined) query.privilege_group = params.privilegeGroup;
  const res = await call.get<{ message?: OverrideApprover[] }>('ury.ury_pos.override.list_approvers', query);
  return Array.isArray(res?.message) ? res.message : [];
}

export async function getPrivilegePolicy(params: {
  posProfile: string;
  privilege: string;
  percent?: number;
  amount?: number;
  privilegeGroup?: number;
}): Promise<PrivilegePolicy | null> {
  const query: Record<string, unknown> = {
    pos_profile: params.posProfile,
    privilege: params.privilege,
  };
  if (params.percent !== undefined) query.percent = params.percent;
  if (params.amount !== undefined) query.amount = params.amount;
  if (params.privilegeGroup !== undefined) query.privilege_group = params.privilegeGroup;
  const res = await call.get<{ message?: PrivilegePolicy }>('ury.ury_pos.override.privilege_policy', query);
  return res?.message ?? null;
}

export async function listReasons(privilege: string): Promise<OverrideReason[]> {
  const res = await call.get<{ message?: OverrideReason[] }>('ury.ury_pos.override.list_reasons', {
    privilege,
  });
  return Array.isArray(res?.message) ? res.message : [];
}

export async function authoriseOverride(params: {
  posProfile: string;
  privilege: string;
  approver: string;
  pin: string;
  referenceDoctype: string;
  referenceName: string;
  claim: Record<string, unknown>;
}): Promise<AuthoriseResponse> {
  const res = await call.post<{ message: AuthoriseResponse }>('ury.ury_pos.override.authorise', {
    pos_profile: params.posProfile,
    privilege: params.privilege,
    approver: params.approver,
    pin_secret: params.pin,
    reference_doctype: params.referenceDoctype,
    reference_name: params.referenceName,
    claim: params.claim,
  });
  return res.message;
}

export async function getMyPinStatus(): Promise<MyPinStatus> {
  const res = await call.get<{ message?: MyPinStatus }>('ury.ury_pos.override.my_pin_status');
  return res?.message ?? { has_pin: false, external_verifier: false };
}

export async function setMyPin(password: string, pin: string): Promise<{ ok: boolean; has_pin: boolean }> {
  const res = await call.post<{ message: { ok: boolean; has_pin: boolean } }>('ury.ury_pos.override.set_my_pin', {
    password,
    pin_secret: pin,
  });
  return res.message;
}

export async function clearUserPin(user: string, posProfile?: string): Promise<ClearPinResponse> {
  const payload: Record<string, unknown> = { user };
  if (posProfile) payload.pos_profile = posProfile;
  const res = await call.post<{ message: ClearPinResponse }>(
    'ury.ury_pos.override.clear_user_pin',
    payload,
  );
  return res.message;
}

export async function listPinUsers(posProfile: string): Promise<PinUserStatus[]> {
  const res = await call.get<{ message?: PinUserStatus[] }>('ury.ury_pos.override.list_pin_users', {
    pos_profile: posProfile,
  });
  return Array.isArray(res?.message) ? res.message : [];
}
