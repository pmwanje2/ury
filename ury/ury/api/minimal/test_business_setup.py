import unittest
from unittest.mock import patch, MagicMock

import frappe

from ury.ury.api.minimal.business_setup import submit_configure_data, _prepare_and_insert_item


class TestSubmitConfigureDataGuard(unittest.TestCase):
    """Guard added to submit_configure_data(): reject Guest callers, and
    reject any call once URY setup (Step 1 and Step 2) is already complete.

    System Settings.setup_complete alone is not the guard, because Step 1
    sets it before Step 2 (this endpoint) runs.

    These are mock-based unit tests -- consistent with the existing
    convention in ury/ury_pos/test_api.py (see TestMergeBillsSEC07) -- so
    they don't require a live Frappe site/bench to reason about, though
    they run under `bench run-tests` like the rest of this codebase's
    test suite.
    """

    @patch("ury.ury.api.minimal.business_setup.is_ury_setup_complete")
    def test_guest_user_is_rejected(self, mock_setup_complete):
        # Guest guard must fire before the setup-complete check even runs.
        # Maximus fork (T0): a Guest is an authorization denial (PermissionError), not a
        # validation error.
        with patch("ury.ury.api.minimal.business_setup.frappe.session", frappe._dict({"user": "Guest"})):
            with self.assertRaises(frappe.PermissionError) as ctx:
                submit_configure_data(data="{}")

        self.assertIn("Not permitted", str(ctx.exception))
        mock_setup_complete.assert_not_called()

    @patch("ury.ury.api.minimal.business_setup.is_ury_setup_complete")
    def test_setup_already_completed_is_rejected(self, mock_setup_complete):
        mock_setup_complete.return_value = True

        with patch("ury.ury.api.minimal.business_setup.frappe.session", frappe._dict({"user": "test@example.com"})):
            with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
                submit_configure_data(data="{}")

        self.assertIn("Setup already completed", str(ctx.exception))
        mock_setup_complete.assert_called_once_with()

    @patch("ury.ury.api.minimal.business_setup.frappe.db.get_single_value")
    @patch("ury.ury.api.minimal.business_setup.is_ury_setup_complete")
    def test_step1_setup_complete_flag_does_not_block_step2(
        self, mock_setup_complete, mock_get_single_value
    ):
        # Regression: after Step 1, System Settings.setup_complete is 1 but
        # no Branch exists yet. Step 2 must still be allowed to run.
        mock_get_single_value.return_value = 1
        mock_setup_complete.return_value = False

        with patch("ury.ury.api.minimal.business_setup.frappe.session", frappe._dict({"user": "test@example.com"})):
            with patch(
                "ury.ury.api.minimal.business_setup._run_configure_data"
            ) as mock_run:
                mock_run.return_value = {"status": "success", "results": {}}
                result = submit_configure_data(data="{}")

        mock_run.assert_called_once()
        self.assertEqual(result, {"status": "success", "results": {}})

    @patch("ury.ury.api.minimal.business_setup.is_ury_setup_complete")
    def test_setup_not_completed_and_authenticated_user_passes_guard(
        self, mock_setup_complete
    ):
        # Setup not complete and a real user must clear the guard and reach
        # _run_configure_data (mocked here so this stays a guard-only test).
        mock_setup_complete.return_value = False

        with patch("ury.ury.api.minimal.business_setup.frappe.session", frappe._dict({"user": "test@example.com"})):
            with patch(
                "ury.ury.api.minimal.business_setup._run_configure_data"
            ) as mock_run:
                mock_run.return_value = {"status": "success", "results": {}}
                result = submit_configure_data(data="{}")

        mock_run.assert_called_once()
        self.assertEqual(result, {"status": "success", "results": {}})


class TestSubmitConfigureDataRollback(unittest.TestCase):
    """Transaction safety: submit_configure_data() wraps the whole configure
    flow (via the private _run_configure_data helper) in try/except and
    calls frappe.db.rollback() on any failure, matching the convention in
    ury/ury_pos/api.py (see TestMergeBillsSEC07.test_merge_bills_different_branches
    in ury/ury_pos/test_api.py).

    We assert on frappe.db.rollback() being invoked rather than checking
    real DocType rows for Branch/Room/Table, because this worktree has no
    runnable Frappe site/bench available (no `bench start`/site config in
    this checkout) to actually insert and roll back real documents against
    a live database. The mock-based assertion still exercises the exact
    guard added in business_setup.py: the except block around
    _run_configure_data() unconditionally calls frappe.db.rollback() before
    re-raising, for both frappe.PermissionError and any other Exception.
    """

    @patch("ury.ury.api.minimal.business_setup.is_ury_setup_complete", return_value=False)
    @patch("ury.ury.api.minimal.business_setup.frappe.db.rollback")
    @patch("ury.ury.api.minimal.business_setup._run_configure_data")
    def test_generic_failure_partway_through_triggers_rollback(
        self, mock_run, mock_rollback, _mock_setup_complete
    ):
        mock_run.side_effect = Exception("boom while creating URY Table")

        with patch("ury.ury.api.minimal.business_setup.frappe.session", frappe._dict({"user": "test@example.com"})):
            with patch(
                "ury.ury.api.minimal.business_setup.frappe.log_error"
            ) as mock_log_error:
                with self.assertRaises(Exception) as ctx:
                    submit_configure_data(data="{}")

        self.assertIn("boom while creating URY Table", str(ctx.exception))
        mock_rollback.assert_called_once()
        mock_log_error.assert_called_once()

    @patch("ury.ury.api.minimal.business_setup.is_ury_setup_complete", return_value=False)
    @patch("ury.ury.api.minimal.business_setup.frappe.db.rollback")
    @patch("ury.ury.api.minimal.business_setup._run_configure_data")
    def test_permission_error_partway_through_triggers_rollback_and_reraises(
        self, mock_run, mock_rollback, _mock_setup_complete
    ):
        mock_run.side_effect = frappe.PermissionError("no permission for Branch")

        with patch("ury.ury.api.minimal.business_setup.frappe.session", frappe._dict({"user": "test@example.com"})):
            with self.assertRaises(frappe.PermissionError):
                submit_configure_data(data="{}")

        mock_rollback.assert_called_once()

    @patch("ury.ury.api.minimal.business_setup.is_ury_setup_complete", return_value=False)
    @patch("ury.ury.api.minimal.business_setup.frappe.db.rollback")
    @patch("ury.ury.api.minimal.business_setup._run_configure_data")
    def test_success_path_never_rolls_back(
        self, mock_run, mock_rollback, _mock_setup_complete
    ):
        mock_run.return_value = {"status": "success", "results": {"branch": "Main"}}

        with patch("ury.ury.api.minimal.business_setup.frappe.session", frappe._dict({"user": "test@example.com"})):
            result = submit_configure_data(data="{}")

        self.assertEqual(result, {"status": "success", "results": {"branch": "Main"}})
        mock_rollback.assert_not_called()


class TestPrepareAndInsertItem(unittest.TestCase):
    @patch("ury.ury.api.minimal.business_setup.frappe.get_doc")
    @patch("ury.ury.api.minimal.business_setup.frappe.db.has_column", return_value=True)
    @patch("ury.ury.api.minimal.business_setup.frappe.get_meta")
    def test_hsn_code_is_set_when_field_exists(self, mock_get_meta, mock_has_column, mock_get_doc):
        mock_meta = MagicMock()
        mock_meta.has_field.return_value = True
        mock_get_meta.return_value = mock_meta

        mock_doc = MagicMock()
        mock_get_doc.return_value = mock_doc

        item_data = {"doctype": "Item", "item_code": "Dish 1"}
        _prepare_and_insert_item(item_data)

        self.assertEqual(item_data.get("gst_hsn_code"), "999512")
        # Maximus fork (T0, #75): menu Items are created with native permission checks.
        mock_doc.insert.assert_called_once_with()


class TestDefaultKotNamingSeries(unittest.TestCase):
    """Minimal business setup assigns a neutral KOT naming series on both create and update."""

    def _run_for_pos_profile(self, *, profile_exists: bool):
        from ury.ury.api.minimal.business_setup import _run_configure_data

        captured = {}
        existing_profile = frappe._dict(name="Main", save=MagicMock())

        def fake_exists(doctype, name=None, *args, **kwargs):
            if doctype == "POS Profile":
                return profile_exists
            if doctype in ("Company", "Branch", "URY Room", "URY Table", "URY Restaurant", "Warehouse",
                           "URY Production Unit", "URY Menu", "Price List", "Item Group"):
                return True
            return False

        def fake_get_doc(arg, name=None, *args, **kwargs):
            if isinstance(arg, dict):
                if arg.get("doctype") == "POS Profile":
                    captured["created"] = dict(arg)
                doc = MagicMock()
                doc.name = arg.get("name") or arg.get("pos_profile_name") or "Created"
                return doc
            if arg == "POS Profile":
                return existing_profile
            if arg == "Company":
                return frappe._dict(default_currency="UGX", default_income_account="Sales - M",
                                    default_expense_account="COGS - M", cost_center="Main - M",
                                    write_off_account="Write Off - M")
            doc = MagicMock()
            doc.name = name or arg
            return doc

        meta = MagicMock()
        meta.get_table_fields.return_value = []
        meta.has_field.return_value = True

        fake_db = MagicMock()
        fake_db.exists.side_effect = fake_exists
        fake_db.get_value.return_value = "M"

        with (
            patch("ury.ury.api.minimal.business_setup._get_company_name", return_value="Main Co"),
            patch("ury.ury.api.minimal.business_setup.frappe.publish_realtime"),
            patch("ury.ury.api.minimal.business_setup.frappe.db", fake_db),
            patch("ury.ury.api.minimal.business_setup.frappe.get_all", return_value=[]),
            patch("ury.ury.api.minimal.business_setup.frappe.get_meta", return_value=meta),
            patch("ury.ury.api.minimal.business_setup.frappe.get_doc", side_effect=fake_get_doc),
            patch("ury.ury.api.minimal.business_setup.frappe.session", frappe._dict(user="admin@example.com")),
        ):
            _run_configure_data({"branch": {"branchName": "Main"}}, {}, "admin@example.com")
        return captured.get("created"), existing_profile

    def test_new_pos_profile_gets_neutral_kot_series(self):
        created, _ = self._run_for_pos_profile(profile_exists=False)
        self.assertIsNotNone(created)
        self.assertEqual(created["custom_kot_naming_series"], "KOT-")
        self.assertEqual(created["kot_naming_series"], "KOT-")

    def test_existing_pos_profile_update_gets_neutral_kot_series(self):
        _, updated = self._run_for_pos_profile(profile_exists=True)
        self.assertEqual(updated.custom_kot_naming_series, "KOT-")
        self.assertEqual(updated.kot_naming_series, "KOT-")


if __name__ == "__main__":
    unittest.main()
