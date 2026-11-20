"""Site-less KOT publication and station-signing contracts."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch


class TestURYKOT(TestCase):
    def load_module(self, path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_kot_display_realtime_publishes_after_commit(self):
        framework = MagicMock()
        framework.get_doc.return_value = SimpleNamespace(
            enable_order_type_wise_display_on_mosaic=0,
        )
        framework.as_json.return_value = "{}"
        framework.db.get_value.return_value = "/alert.wav"
        framework.cache.return_value.get_value.return_value = "11:59:00"
        with patch.dict(sys.modules, {
            "frappe": framework,
            "frappe.utils.print_format": SimpleNamespace(print_by_server=MagicMock()),
            "frappe.model.document": SimpleNamespace(Document=object),
        }):
            module = self.load_module(Path(__file__).with_name("ury_kot.py"), "kot_under_test")
            kot = module.URYKOT()
            kot.branch = "Branch A"
            kot.production = "Kitchen"
            kot.invoice = "INV-001"
            kot.pos_profile = "POS Profile"
            kot.time = "12:00:00"
            kot.kotDisplayRealtime()

        framework.publish_realtime.assert_called_once_with(
            "kot_update_Branch A_Kitchen",
            {"kot": {}, "audio_file": "/alert.wav", "last_kot_time": "11:59:00"},
            after_commit=True,
        )

    def test_kitchen_station_role_may_request_qz_signatures(self):
        framework = MagicMock()
        framework.whitelist.side_effect = lambda **kwargs: lambda function: function
        with patch.dict(sys.modules, {
            "frappe": framework,
            "frappe.www.printview": SimpleNamespace(validate_print_permission=MagicMock()),
            "pypdf": SimpleNamespace(PdfWriter=MagicMock()),
            "ury.ury.doctype.ury_order.ury_order": SimpleNamespace(
                release_merge_cluster_tables=MagicMock(),
                _free_tables_if_no_open_invoices=MagicMock(),
            ),
        }):
            path = Path(__file__).parents[2] / "api" / "ury_print.py"
            module = self.load_module(path, "kot_signer_under_test")

        self.assertIn("Restaurant Kitchen", module.QZ_SIGNING_ROLES)
