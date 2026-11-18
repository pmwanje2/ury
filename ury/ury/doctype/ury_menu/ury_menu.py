# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from ury.ury.api.item_price import item_price_release_states, price_valid_on


class URYMenu(Document):
    def validate(self):
        for d in self.items:
            if not d.rate:
                d.rate = frappe.db.get_value("Item", d.item, "standard_rate")

    def on_update(self):
        """Sync Price List"""
        self.make_price_list()

    def on_trash(self):
        """clear prices"""
        self.clear_item_price()

    def clear_item_price(self, price_list=None):
        """clear all item prices for this menu"""
        if not price_list:
            price_list = self.get_price_list().name
        frappe.db.sql("delete from `tabItem Price` where price_list = %s", price_list)

    def make_price_list(self):
        """Keep the menu's Item Prices in step with the menu rates.

        A condition record is never overwritten or deleted: a changed rate is a
        new Item Price valid from today, and unchanged lines write nothing.
        With an Item Price release workflow the new row is a proposal that a
        second person releases; billing keeps the released price until then.
        """
        price_list = self.get_price_list()
        self.db_set("price_list", price_list.name)

        today = frappe.utils.getdate()
        release_field, released = item_price_release_states()

        for d in self.items:
            rate = flt(d.rate)
            current = price_valid_on(d.item, price_list.name, today, release_field, released)
            if current and flt(current.price_list_rate) == rate:
                continue
            if release_field and self.has_pending_proposal(d.item, price_list.name, rate, today, release_field, released):
                continue
            self.set_todays_item_price(d.item, price_list.name, rate, today, release_field, released)

    def has_pending_proposal(self, item_code, price_list, rate, today, release_field, released):
        """An unreleased Item Price at this rate, valid by today, already waits for release."""
        rows = frappe.db.get_list(
            "Item Price",
            filters={"item_code": item_code, "price_list": price_list},
            fields=["price_list_rate", "valid_from", "valid_upto", release_field],
        )
        getdate = frappe.utils.getdate
        return any(
            flt(row.price_list_rate) == rate
            and row.get(release_field) not in released
            and (not row.valid_from or getdate(row.valid_from) <= today)
            and (not row.valid_upto or getdate(row.valid_upto) >= today)
            for row in rows
        )

    def set_todays_item_price(self, item_code, price_list, rate, today, release_field, released):
        fields = ["name", "price_list_rate"] + ([release_field] if release_field else [])
        todays = frappe.db.get_list(
            "Item Price",
            filters={"item_code": item_code, "price_list": price_list, "valid_from": today},
            fields=fields,
        )
        if not todays:
            frappe.get_doc(
                dict(
                    doctype="Item Price",
                    price_list=price_list,
                    item_code=item_code,
                    price_list_rate=rate,
                    valid_from=today,
                )
            ).insert()
            return

        row = todays[0]
        if flt(row.price_list_rate) == rate:
            return
        if release_field and row.get(release_field) in released:
            frappe.throw(
                _("{0} already has a price released from {1}. Propose the change as a new Item Price with a later valid from date.").format(item_code, today)
            )
        item_price = frappe.get_doc("Item Price", row.name)
        item_price.price_list_rate = rate
        item_price.save()

    def get_price_list(self):
        """Create price list for menu if missing"""
        price_list_name = frappe.db.get_value(
            "Price List", dict(restaurant_menu=self.name)
        )
        if price_list_name:
            price_list = frappe.get_doc("Price List", price_list_name)
        else:
            price_list = frappe.new_doc("Price List")
            price_list.restaurant_menu = self.name
            price_list.price_list_name = self.name

        price_list.enabled = 1
        price_list.selling = 1
        price_list.save()

        return price_list
