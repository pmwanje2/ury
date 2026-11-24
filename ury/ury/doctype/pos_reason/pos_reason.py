import re

import frappe
from frappe import _
from frappe.model.document import Document


def parse_applies_to(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[\n,]+", str(raw)) if part.strip()]


class POSReason(Document):
    def validate(self):
        from ury.ury_pos.override import PRIVILEGES

        self.code = (self.code or "").strip()
        self.description = (self.description or "").strip()
        if not self.code or not self.description:
            frappe.throw(_("Code and Description are required."))

        codes = parse_applies_to(self.applies_to)
        unknown = [c for c in codes if c not in PRIVILEGES]
        if unknown:
            frappe.throw(_("Unknown privilege code(s) in Applies To: {0}.").format(", ".join(unknown)))
        self.applies_to = "\n".join(dict.fromkeys(codes))
