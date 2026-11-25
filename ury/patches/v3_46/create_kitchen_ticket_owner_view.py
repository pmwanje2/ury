"""Expose the same round-ledger receipt used by owned_ticket_filters to SQL reports."""
import frappe


def execute():
    # Only the current receiving check has a JSON boolean true for this ticket.
    # Earlier owners carry forwarding strings. Settlement does not change the receipt.
    frappe.db.sql_ddl("""
        CREATE OR REPLACE SQL SECURITY INVOKER VIEW ury_kitchen_ticket_owner AS
        SELECT k.name AS ticket, COALESCE(receipt.invoice, k.invoice) AS invoice,
               EXISTS (SELECT 1 FROM `tabURY KOT` c
                        WHERE c.docstatus = 1 AND c.type = 'Cancelled'
                          AND FIND_IN_SET(k.name, REPLACE(c.original_kot, ', ', ','))) AS wholly_cancelled
          FROM `tabURY KOT` k
          LEFT JOIN (
              SELECT i.name AS invoice, j.ticket
                FROM `tabPOS Invoice` i
                CROSS JOIN JSON_TABLE(
                    JSON_KEYS(IF(JSON_VALID(i.ury_applied_rounds), i.ury_applied_rounds, '{}'),
                              '$.ticket_ownership'),
                    '$[*]' COLUMNS(ticket VARCHAR(140) PATH '$')
                ) j
               WHERE JSON_TYPE(JSON_EXTRACT(IF(JSON_VALID(i.ury_applied_rounds), i.ury_applied_rounds, '{}'),
                                            '$.applied_rounds')) = 'OBJECT'
                 AND JSON_EXTRACT(IF(JSON_VALID(i.ury_applied_rounds), i.ury_applied_rounds, '{}'),
                                  CONCAT('$.ticket_ownership.', JSON_QUOTE(j.ticket))) = 'true'
          ) receipt ON receipt.ticket = k.name
    """)
