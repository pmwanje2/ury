"""Install G19 POS Service Charge Rule table & Sales Taxes and Charges charge-kind metadata."""

from __future__ import annotations

from ury.ury_pos.privileged_fields import ensure_privileged_action_custom_fields


def execute() -> None:
    ensure_privileged_action_custom_fields()
