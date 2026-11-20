import { t } from '../i18n';

// Stable manager-side refusal ids (ury/ury_pos/floor_pin.py) -> localized copy.
const MANAGER_REFUSAL_COPY: Array<[string, string]> = [
  ['URY_FLOOR_PIN_NOT_PERMITTED', 'errors.floor_pin_not_permitted'],
  ['URY_FLOOR_TABLET_LABEL_INVALID', 'pair_tablet.label_invalid'],
  ['URY_FLOOR_PROFILE_NOT_PAIRABLE', 'pair_tablet.profile_not_pairable'],
  ['URY_FLOOR_PIN_ISSUE_EXHAUSTED', 'staff_pins.issue_failed'],
];

export function floorPinError(message: string): string {
  const match = MANAGER_REFUSAL_COPY.find(([code]) => message.includes(code));
  return match ? t(match[1]) : message;
}
