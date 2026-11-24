# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from ury.brand import coded_message
from ury.ury.api.ury_kot_routing import RoutingError, validate_failover_chain


class URYProductionUnit(Document):
    def validate(self):
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
