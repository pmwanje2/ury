import frappe

from ury.ury.print_format.defaults import configure_pos_invoice_print_formats


def execute():
	configure_pos_invoice_print_formats()
	frappe.db.commit()
