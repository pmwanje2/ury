"""Install custom fields for Scope e+f privileged POS actions."""

from ury.ury_pos.privileged_fields import ensure_privileged_action_custom_fields


def execute():
    ensure_privileged_action_custom_fields()
