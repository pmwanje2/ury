# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from ury.brand import coded_message
from .station_scope import station_company, prune_station_shares
from ury.ury.api.ury_kot_routing import RoutingError, validate_failover_chain


_DEVICE_SETTINGS_SAVE = object()


class URYProductionUnit(Document):
    def has_permission(self, permtype="read", *, debug=False, user=None):
        return bool(station_company(self, user)) and super().has_permission(permtype, debug=debug, user=user)

    def validate_share(self, share):
        # Group-wide shares cannot be limited to the station's company.
        current = frappe.db.get_value(self.doctype, self.name, ["company", "branch"], as_dict=True, for_update=True)
        if share.everyone or not share.user or not current or not station_company(current, share.user, for_update=True):
            frappe.throw(_("Station sharing requires a user in the owning branch and company."), frappe.PermissionError)

    def on_update(self):
        if self.has_value_changed("branch") or self.has_value_changed("company"):
            prune_station_shares([self.name])

    def save_device_settings(self):
        # Only a server-loaded, existing station can take this narrow exception.
        self.flags.device_settings_save = _DEVICE_SETTINGS_SAVE
        try:
            self.save()
        finally:
            self.flags.pop("device_settings_save", None)

    def _get_missing_mandatory_fields(self):
        missing = super()._get_missing_mandatory_fields()
        if self.flags.device_settings_save is _DEVICE_SETTINGS_SAVE and not self.department:
            return [item for item in missing if item[0] != "department"]
        return missing

    def validate(self):
        if not self.branch:
            frappe.throw(_("Select an owning branch for the production station."))
        original = self.get_doc_before_save()
        if original and (self.has_value_changed("branch") or self.has_value_changed("company")):
            if frappe.session.user != "Administrator" and not station_company(original):
                frappe.throw(_("You cannot move a station outside your ownership scope."), frappe.PermissionError)
        if not station_company(self):
            frappe.throw(_("Station ownership must match an authorized branch and company."), frappe.PermissionError)
        failover_target = self.get("custom_failover_production_unit")
        if failover_target:
            try:
                validate_failover_chain(
                    self.name or self.get("production"),
                    failover_target,
                    branch=self.get("branch"),
                )
            except RoutingError as exc:
                frappe.throw(
                    coded_message(exc.reason_code.replace("_", "-"), str(exc)),
                    frappe.ValidationError,
                )

        timeout = self.get("custom_heartbeat_timeout_seconds")
        if timeout is not None and timeout != "":
            try:
                timeout_val = int(timeout)
            except (TypeError, ValueError):
                frappe.throw(
                    coded_message(
                        "ROUTING-INVALID-HEARTBEAT-TIMEOUT",
                        _("Heartbeat timeout must be a whole number of seconds."),
                    ),
                    frappe.ValidationError,
                )
            if timeout_val < 0:
                frappe.throw(
                    coded_message(
                        "ROUTING-INVALID-HEARTBEAT-TIMEOUT",
                        _("Heartbeat timeout cannot be negative."),
                    ),
                    frappe.ValidationError,
                )
