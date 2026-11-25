"""Native recall and logical backup regressions. The backup fixture needs mariadb-dump,
mariadb and a provisioning account (FRAPPE_DB_ROOT_PASSWORD or site db_root_password).
Only random disposable schemas are provisioned. Restore always runs as the target tenant.
"""
import json
import os
import secrets
import subprocess
import unittest
from datetime import timedelta
from tempfile import NamedTemporaryFile
from unittest.mock import patch

import frappe
import pymysql
from frappe.database import get_db
from frappe.utils import now_datetime

from ury.ury.api import ury_kot_display as display
from ury.ury.api.ury_profitability_posted_cost import _read_postings
from ury.ury.doctype.ury_order import test_pos_pr9_r21 as r21, ury_order


class TestRecall(unittest.TestCase):
    def setUp(self):
        self.fx = r21.TestReceivingCheck()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_receiving_check_recalls_only_uncompensated_tickets(self):
        for currency, places in self.fx.kitchen.currencies:
            with self.subTest(currency=currency):
                _, check, ticket = self.fx.owners(currency, places)
                self.assertEqual(display.expo_bump_order(check.name, tickets=[ticket.name])["served_kots"], [ticket.name])
                def candidates():
                    return {r.name: r.invoice for r in display.recent_served_kots(branch=check.branch)["kots"]}
                self.assertEqual(candidates()[ticket.name], check.name)
                ury_order.cancel_kot(check.name, reason="Guest")
                self.assertNotIn(ticket.name, candidates())

    def test_compensated_page_does_not_hide_older_recallable_tickets(self):
        _, owner, first = self.fx.owners("USD", 2)
        start = now_datetime() - timedelta(minutes=2)
        tickets = []
        for index in range(101):
            doc = frappe.copy_doc(first)
            doc.name = "r23-served-" + str(index)
            doc.update(dict(creation=start, modified=start + timedelta(seconds=index),
                            validation_dedup_key=None, docstatus=1, production_time=0, order_status="Served"))
            doc.db_insert()
            tickets.append(doc.name)
        # A single submitted compensation may cover several original releases.
        cancel = frappe.copy_doc(first)
        cancel.name = "r23-cancel-page"
        cancel.update(dict(validation_dedup_key=None, type="Cancelled", original_kot=",".join(tickets[1:]), docstatus=1))
        cancel.db_insert()
        self.assertEqual([r.name for r in display.recent_served_kots(branch=owner.branch)["kots"]], tickets[:1])


class TestTenantBackup(unittest.TestCase):
    def setUp(self):
        self.host = frappe.conf.db_host or "localhost"
        self.port = int(frappe.conf.db_port or 3306)
        admin = pymysql.connect(host=self.host, port=self.port, user=os.getenv("FRAPPE_DB_ROOT_USER", "root"),
                                password=os.getenv("FRAPPE_DB_ROOT_PASSWORD", frappe.conf.db_root_password or ""),
                                autocommit=True)
        self.addCleanup(admin.close)
        self.admin = admin.cursor()
        self.source = self.tenant()
        self.target = self.tenant()
        self.fixture_db = frappe.db.sql("SELECT DATABASE()")[0][0]
        with self.connection(self.source) as conn, conn.cursor() as cur:
            for table in ("tabPOS Invoice", "tabURY KOT", "tabURY Fulfilment Record", "tabStock Entry"):
                self.admin.execute(f"SHOW CREATE TABLE `{self.fixture_db}`.`{table}`")
                cur.execute(self.admin.fetchone()[1])
            # Mirror the installed app's projection, if present, with the source
            # tenant as its definer. The dump and restore below are unmodified.
            self.admin.execute("SELECT VIEW_DEFINITION FROM information_schema.VIEWS "
                               "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='ury_kitchen_ticket_owner'", self.fixture_db)
            for (definition,) in self.admin.fetchall():
                local = definition.replace(f"`{self.fixture_db}`.", "")
                cur.execute("CREATE SQL SECURITY INVOKER VIEW ury_kitchen_ticket_owner AS " + local)
            for name, ledger, status in (
                ("issuer", {"applied_rounds": {}, "ticket_ownership": {"served": "paid"}}, 0),
                ("paid", {"applied_rounds": {}, "ticket_ownership": {"served": True}}, 1),
                ("empty", "", 0), ("malformed", '{"ticket_ownership":broken}', 0),
            ):
                cur.execute("INSERT INTO `tabPOS Invoice` (name,company,branch,ury_applied_rounds,docstatus,"
                            "status,custom_split_from) VALUES (%s,'Company','Branch',%s,%s,%s,%s)",
                            (name, json.dumps(ledger) if isinstance(ledger, dict) else ledger, status,
                             "Paid" if status else "Draft", "issuer" if status else None))
            cur.execute("INSERT INTO `tabURY KOT` (name,invoice,branch,docstatus) VALUES ('served','issuer','Branch',1)")
            cur.execute("INSERT INTO `tabStock Entry` (name,purpose,company,total_outgoing_value,docstatus)"
                        " VALUES ('issue','Material Issue','Company',7,1)")
            cur.execute("INSERT INTO `tabURY Fulfilment Record` (name,company,branch,kot,item_code,qty,"
                        "posted_to_erpnext,posting_reference) VALUES ('posted','Company','Branch','served','Soup',1,1,'issue')")

    def tenant(self):
        name = "r23" + secrets.token_hex(7)
        password = secrets.token_urlsafe(24)
        self.admin.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        self.addCleanup(self.admin.execute, f"DROP DATABASE `{name}`")
        self.admin.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (name, password))
        self.addCleanup(self.admin.execute, "DROP USER %s@'%%'", (name,))
        self.admin.execute(f"GRANT ALL ON `{name}`.* TO %s@'%%'", (name,))
        return name, password

    def connection(self, tenant):
        name, password = tenant
        return pymysql.connect(host=self.host, port=self.port, user=name, password=password,
                               database=name, autocommit=True, collation="utf8mb4_unicode_ci")

    def cli(self, binary, tenant, *options, data=None):
        name, password = tenant
        with NamedTemporaryFile(mode="w") as credentials:
            credentials.write(f"[client]\npassword={password}\n")
            credentials.flush()
            return subprocess.run([binary, f"--defaults-file={credentials.name}", "--protocol=tcp",
                                   "--host", self.host, "--port", str(self.port), "--user", name, *options, name],
                                  input=data, capture_output=True)

    def assert_paid_owner(self, tenant):
        name, password = tenant
        db = get_db(host=self.host, port=self.port, user=name, password=password, cur_db_name=name)
        try:
            with patch.object(frappe.local, "db", db):
                rows = _read_postings("Company", "Branch", ["issuer", "paid"])
                self.assertEqual(db.sql("SHOW WARNINGS"), ())
                self.assertEqual([(row.invoice, row.posted_cost) for row in rows], [("paid", 7)])
        finally:
            db.close()

    def test_malformed_ledgers_are_quiet(self):
        self.assert_paid_owner(self.source)

    def test_restricted_dump_restore_keeps_paid_ticket_owner(self):
        backup = self.cli("mariadb-dump", self.source, "--single-transaction", "--skip-lock-tables", "--skip-add-drop-table")
        self.assertEqual(backup.returncode, 0, backup.stderr.decode())
        restored = self.cli("mariadb", self.target, "--show-warnings", data=backup.stdout)
        self.assertEqual(restored.returncode, 0, restored.stderr.decode())
        self.assertEqual(restored.stdout, b"")  # --show-warnings must also be quiet.
        with self.connection(self.target) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA=DATABASE()")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT docstatus,status,custom_split_from FROM `tabPOS Invoice` WHERE name='paid'")
            self.assertEqual(cur.fetchone(), (1, "Paid", "issuer"))
            with self.assertRaises(pymysql.MySQLError) as refusal:
                cur.execute(f"SELECT name FROM `{self.source[0]}`.`tabPOS Invoice`")
            self.assertIn(refusal.exception.args[0], (1044, 1142))
        self.assert_paid_owner(self.target)

