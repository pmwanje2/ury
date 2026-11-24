# Copyright (c) 2026, Maximus contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from ury.brand import coded_message


class URYRoutingOverride(Document):
    def validate(self):
        if not any(
            self.get(field)
            for field in ("item", "item_group", "room", "order_type", "pos_profile")
        ):
            frappe.throw(
                coded_message(
                    "ROUTING-OVERRIDE-EMPTY-SCOPE",
                    _(
                        "Specify at least one routing scope "
                        "(Item, Item Group, Room, Order Type, or POS Profile)."
                    ),
                ),
                frappe.ValidationError,
            )

        if self.target_production_unit and self.branch:
            target_branch = frappe.db.get_value(
                "URY Production Unit", self.target_production_unit, "branch"
            )
            if target_branch and target_branch != self.branch:
                frappe.throw(
                    coded_message(
                        "ROUTING-OVERRIDE-BRANCH-MISMATCH",
                        _(
                            "Target station {0} belongs to branch {1}, not {2}."
                        ).format(self.target_production_unit, target_branch, self.branch),
                    ),
                    frappe.ValidationError,
                )

        if self.room and self.branch:
            room_branch = frappe.db.get_value("URY Room", self.room, "branch")
            if room_branch and room_branch != self.branch:
                frappe.throw(
                    coded_message(
                        "ROUTING-OVERRIDE-ROOM-BRANCH-MISMATCH",
                        _("Room {0} belongs to branch {1}, not {2}.").format(
                            self.room, room_branch, self.branch
                        ),
                    ),
                    frappe.ValidationError,
                )

        if self.pos_profile and self.branch:
            profile_branch = frappe.db.get_value(
                "POS Profile", self.pos_profile, "branch"
            )
            if profile_branch and profile_branch != self.branch:
                frappe.throw(
                    coded_message(
                        "ROUTING-OVERRIDE-PROFILE-BRANCH-MISMATCH",
                        _("POS Profile {0} belongs to branch {1}, not {2}.").format(
                            self.pos_profile, profile_branch, self.branch
                        ),
                    ),
                    frappe.ValidationError,
                )
