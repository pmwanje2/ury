"""Site-less RED->GREEN regression tests for Neon R3 live bench findings on PR #157.

Covers:
- [R3 Live] `getModeOfPayment()` must return `[]` (never HTTP 500 `KeyError: 'pos_profile'`)
  when `getPosProfile()` returns `{"requires_outlet_selection": True, "available_outlets": [...]}`
  during parallel `initializeApp()` multi-outlet cold-boot before an outlet is selected.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]


class Row(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


def _stub(name, **attrs):
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _load_api_module(fake_frappe):
    fake_utils = _stub(
        "frappe.utils",
        cint=lambda v: int(v or 0),
        flt=lambda v, p=None: float(v or 0),
        get_datetime=lambda v=None: "2026-11-25 08:00:00",
        getdate=lambda v=None: "2026-11-25",
        now_datetime=lambda: "2026-11-25 08:00:00",
        nowdate=lambda: "2026-11-25",
        nestedset=MagicMock(),
        validate_phone_number=MagicMock(),
    )
    fake_frappe.utils = fake_utils
    fake_frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
    fake_frappe._ = lambda m, *a: m

    offline_stub = _stub(
        "ury.ury_pos.offline_tenders",
        profile_offline_payment_modes=lambda profile, room_modes=None, _frappe=None: [
            {
                "mode_of_payment": row.mode_of_payment,
                "allow_offline": row.mode_of_payment == "Cash",
                "requires_reference": False,
            }
            for row in (getattr(profile, "payments", None) or [])
        ],
    )

    stubs = {
        "frappe": fake_frappe,
        "frappe.utils": fake_utils,
        "frappe.utils.nestedset": MagicMock(),
        "frappe.utils.data": MagicMock(),
        "frappe.utils.password": MagicMock(),
        "frappe.permissions": MagicMock(),
        "frappe.model": MagicMock(),
        "frappe.model.document": MagicMock(),
        "erpnext": MagicMock(),
        "erpnext.accounts": MagicMock(),
        "erpnext.accounts.doctype": MagicMock(),
        "erpnext.accounts.doctype.pos_opening_entry": MagicMock(),
        "erpnext.accounts.doctype.pos_opening_entry.pos_opening_entry": MagicMock(),
        "ury.ury_pos.offline_tenders": offline_stub,
    }
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("_test_r3_api_mod", ROOT / "ury" / "ury_pos" / "api.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class TestR3LiveGetModeOfPaymentColdBoot(unittest.TestCase):
    def test_get_mode_of_payment_returns_empty_list_when_outlet_selection_required(self):
        """Parallel cold-boot `initializeApp()` calls `getModeOfPayment()` before an outlet is chosen;
        it must return `[]` cleanly instead of raising `KeyError: 'pos_profile'` (HTTP 500)."""
        fake_frappe = MagicMock()
        api = _load_api_module(fake_frappe)

        with patch.object(
            api,
            "getPosProfile",
            return_value={
                "requires_outlet_selection": True,
                "available_outlets": [
                    {"pos_profile": "Main Grill POS", "branch": "Main Grill"},
                    {"pos_profile": "Pool Bar POS", "branch": "Pool Bar"},
                ],
            },
        ):
            result = api.getModeOfPayment()

        self.assertEqual(result, [])
        fake_frappe.get_doc.assert_not_called()

    def test_get_mode_of_payment_returns_modes_when_profile_resolved(self):
        """Once an outlet is selected, `getModeOfPayment()` returns its payment modes with offline metadata."""
        fake_frappe = MagicMock()
        fake_frappe.get_doc.return_value = SimpleNamespace(
            payments=[
                Row(mode_of_payment="Cash"),
                Row(mode_of_payment="Card"),
            ]
        )
        api = _load_api_module(fake_frappe)

        with patch.dict(
            sys.modules,
            {
                "ury.ury_pos.offline_tenders": _stub(
                    "ury.ury_pos.offline_tenders",
                    profile_offline_payment_modes=lambda profile, room_modes=None, _frappe=None: [
                        {
                            "mode_of_payment": row.mode_of_payment,
                            "allow_offline": row.mode_of_payment == "Cash",
                            "requires_reference": False,
                        }
                        for row in profile.payments
                    ],
                )
            },
        ):
            with patch.object(
                api,
                "getPosProfile",
                return_value={"pos_profile": "Main Grill POS", "room_charge_modes": []},
            ):
                result = api.getModeOfPayment()

        self.assertEqual(
            result,
            [
                {"mode_of_payment": "Cash", "opening_amount": 0.0, "allow_offline": True, "requires_reference": False},
                {"mode_of_payment": "Card", "opening_amount": 0.0, "allow_offline": False, "requires_reference": False},
            ],
        )


if __name__ == "__main__":
    unittest.main()
