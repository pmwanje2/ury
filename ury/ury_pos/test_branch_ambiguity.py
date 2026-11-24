"""Branch ambiguity is refused with a neutral code; no Frappe site or database is required.

``ury/tests/test_branch_resolution.py`` covers the same refusal on a site.
"""

import unittest
from unittest.mock import patch

import frappe

from ury.ury_pos import api


class TestBranchAmbiguity(unittest.TestCase):
    def test_two_branches_without_a_default_refuse_with_a_neutral_code(self):
        with patch.object(api, "frappe") as f, patch.object(api, "_", side_effect=lambda message: message), patch(
            "frappe.core.doctype.user_permission.user_permission.get_user_permissions", return_value={}
        ):
            f.session.user = "cashier"
            f.db.sql.return_value = [frappe._dict(branch="A", room="Room A"), frappe._dict(branch="B", room="Room B")]
            f.throw.side_effect = lambda message, *args, **kwargs: (_ for _ in ()).throw(frappe.ValidationError(message))
            with self.assertRaisesRegex(
                frappe.ValidationError,
                r"^\[POS-BRANCH-AMBIGUOUS\] Set a default Branch User Permission for an assigned branch\.$",
            ):
                api.getBranch()


if __name__ == "__main__":
    unittest.main()
