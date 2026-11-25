from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_custom_fields({
        "POS Invoice Item": [
            dict(fieldname="custom_seat_no", label="Seat Number", fieldtype="Int",
                 insert_after="custom_course", default="0", non_negative=1),
            dict(fieldname="custom_origin_invoice_item", label="Original Invoice Item", fieldtype="Data",
                 insert_after="custom_seat_no", read_only=1, no_copy=1),
            dict(fieldname="custom_kitchen_anchor", label="Kitchen Check", fieldtype="Data",
                 insert_after="custom_origin_invoice_item", read_only=1, no_copy=1),
        ],
        "Item": [
            dict(fieldname="custom_is_shareable", label="Allow Fractional Check Split", fieldtype="Check",
                 insert_after="stock_uom", default="0"),
        ],
        "Branch": [
            dict(fieldname="custom_table_release_trigger", label="Table Release Trigger", fieldtype="Select",
                 options="On Bill Print (Legacy)\nOn Settlement (Hospitality Default)",
                 default="On Bill Print (Legacy)", insert_after="custom_grace_period"),
            dict(fieldname="custom_enable_bussing_state", label="Enable Bussing State", fieldtype="Check",
                 default="0", insert_after="custom_table_release_trigger"),
            dict(fieldname="custom_block_dirty_table_seating", label="Block Seating at Dirty Tables",
                 fieldtype="Check", default="0", insert_after="custom_enable_bussing_state"),
        ],
    }, update=True)
