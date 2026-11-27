# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class URYSelfOrderingProfile(Document):
    def validate(self):
        if self.enable_guest_tips:
            from ury.ury.api.guest_tip import tip_presets

            from ury.ury.hooks.ury_pos_tender import profile_accepts_guest_tips

            if not profile_accepts_guest_tips(self.pos_profile):
                frappe.throw(frappe._("Configure an enabled Tips Payable liability account for this POS Profile before enabling guest tips."))
            try:
                tip_presets(self.guest_tip_presets)
            except ValueError:
                frappe.throw(frappe._("Enter up to eight whole tip percentages between 1 and 100."))

    def before_insert(self):
        # Per-profile secret used to sign stateless QR table/pickup tokens
        # (see ury/ury/api/self_ordering.py). Never exposed via the API.
        if not self.qr_signing_secret:
            self.qr_signing_secret = frappe.generate_hash(length=48)
