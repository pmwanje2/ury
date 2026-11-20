"""Maximus fork: kept so the Patch Log line from the version-2 line stays valid.

On the version-2 line this patch granted the URY Admin role permissions. Live sites already
ran it. Fresh installs get the same rows from ury.role_permissions via
ury.install.ensure_role_permissions, so the patch body is a no-op here.
"""


def execute():
	return None
