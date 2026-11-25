"""Share server-computed approval details with the POS manager prompt."""

from typing import Any


def attach_auth_challenge(
    *,
    privilege: str,
    pos_profile: str | None,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any],
    amount: float | None = None,
    percent: float | None = None,
    framework,
) -> None:
    """Attach the exact server-computed claim to frappe.local.response for ManagerApproval parity."""
    local = getattr(framework, "local", None)
    resp = getattr(local, "response", None) if local is not None else None
    if isinstance(resp, dict):
        resp["pos_auth_challenge"] = {
            "privilege": privilege,
            "pos_profile": pos_profile or "",
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "claim": claim,
            "amount": amount,
            "percent": percent,
        }
