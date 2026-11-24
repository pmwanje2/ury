import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class POSPrivilegeRule(Document):
    def validate(self):
        from ury.ury_pos.override import PRIVILEGES

        if self.privilege not in PRIVILEGES:
            frappe.throw(_("Unknown POS privilege: {0}.").format(self.privilege))

        if self.pos_profile:
            profile_company = frappe.db.get_value("POS Profile", self.pos_profile, "company")
            if profile_company and profile_company != self.company:
                frappe.throw(
                    _("POS Profile {0} belongs to company {1}, not {2}.").format(
                        self.pos_profile, profile_company, self.company
                    )
                )

        if flt(self.max_percent) < 0 or flt(self.max_percent) > 100:
            frappe.throw(_("Max Percent must be between 0 and 100."))
        if flt(self.max_amount) < 0:
            frappe.throw(_("Max Amount cannot be negative."))

        pg = cint(self.privilege_group)
        if pg < 0 or pg > 3:
            frappe.throw(_("Privilege Group must be between 0 and 3."))

        filters = {
            "company": self.company,
            "privilege": self.privilege,
            "role": self.role,
            "name": ["!=", self.name or ""],
        }
        if self.pos_profile:
            filters["pos_profile"] = self.pos_profile
        else:
            filters["pos_profile"] = ["in", ["", None]]

        if frappe.db.exists("POS Privilege Rule", filters):
            scope = self.pos_profile or self.company
            frappe.throw(
                _("A privilege rule for {0} / {1} already exists for {2}.").format(
                    self.privilege, self.role, scope
                )
            )
