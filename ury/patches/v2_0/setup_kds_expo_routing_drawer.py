"""Install custom fields for KDS Expo, Rush/VIP, configurable recall window, routing, and drawer kick."""

from ury.ury_pos.kds_device_fields import ensure_kds_device_custom_fields


def execute():
    ensure_kds_device_custom_fields()
