import os
import sys
import unittest

import frappe
from frappe.utils import now_datetime

os.chdir("/home/frappe/frappe-bench/sites")
frappe.init(site="p2.localhost")
frappe.connect()
frappe.set_user("Administrator")
now_datetime()
try:
    suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:] or ["ury.tests.test_guest_phone_payment"])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
finally:
    frappe.db.rollback()
    frappe.destroy()
sys.exit(not result.wasSuccessful())
