import { t } from '../i18n';

export type FloorPinSurface = 'pair_tablet' | 'staff_pins';

// Stable manager-side refusal ids (ury/ury_pos/floor_pin.py) -> localized copy.
const managerRefusalCopy = (surface: FloorPinSurface): Array<[string, string]> => [
  ['URY_FLOOR_PIN_NOT_PERMITTED', 'errors.floor_pin_not_permitted'],
  ['URY_FLOOR_PIN_LOGIN_DISABLED', `${surface}.login_disabled`],
  ['URY_FLOOR_PIN_TARGET_INELIGIBLE', 'staff_pins.target_ineligible'],
  ['URY_FLOOR_TABLET_LABEL_INVALID', 'pair_tablet.label_invalid'],
  ['URY_FLOOR_PROFILE_NOT_PAIRABLE', 'pair_tablet.profile_not_pairable'],
  ['URY_FLOOR_PIN_POLICY_CONFLICT', 'staff_pins.policy_conflict'],
  ['URY_FLOOR_PIN_ISSUE_EXHAUSTED', 'staff_pins.issue_failed'],
];

export function floorPinError(message: string, surface: FloorPinSurface): string {
  const match = managerRefusalCopy(surface).find(([code]) => message.includes(code));
  return match ? t(match[1]) : message;
}
