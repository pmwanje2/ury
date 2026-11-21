# URY site and outlet branding

## After release: chat/Desk steps

**Outlet name:** In URY, open **Setup → Self Ordering** (`/ury/self-ordering-profile`). Create or edit the outlet's profile, enter **Guest Display Name**, and choose **Save Profile**. The staff profile name stays unchanged when you edit the guest name. Clearing the guest name restores the site name on this profile's guest links.

**Outlet logo:** Open **Setup → Branches** (`/ury/branch`), choose **Edit Branch** for the branch linked to the profile's restaurant, enter the public image path (for example `/files/outlet.png`) in **Guest Logo**, and save. Use an image already available to guests. Clearing this field restores the site logo. The image belongs to the restaurant and is shared by its profiles.

**Verify:** Open the outlet's existing signed pickup or table link (`/order?t=…`) in a signed-out browser. Check the name and logo. The plain `/order` entry keeps the site identity.

**Site name, logo and favicon:** On a tenant with the governed Website Settings chat operations enabled, sign in to Eve as a person with the tenant's branding permissions. For example: “Show the current site name, logo and favicon. Change the site name to …, use `/files/site-logo.png` for the logo and `/files/site-icon.ico` for the favicon.” Review the old and new values, confirm, then ask Eve to read them back. The native target is `Website Settings` (document name `Website Settings`): `get_document`, then `update_draft` with the current `modified` and only `app_name`, `app_logo`, `favicon`. Use existing public `/files/` images; on a tenant admitting supervised image attachments, an uploaded site logo can also be attached to `app_logo` with `attach_file`. Refresh `/ury` and the unsigned `/order` entry to verify. If Eve refuses the operation, ask the operator to check tenant admission and native field permissions. No ERPNext Desk steps are required.
