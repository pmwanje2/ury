"""Site branding contracts, runnable without a Frappe bench."""

import colorsys
import importlib
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
PAGES = {"pos": "pos", "urypos": "urypos", "mosaic": "mosaic", "order": "self-order"}
DEFAULT_TITLES = {"pos": "URY POS", "urypos": "URY POS", "mosaic": "Mosaic", "order": "Order"}
SITE_BRAND = {"name": 'MAX\\MUS "</script><script>evil()</script>', "logo": '/files/logo"<&.png', "favicon": '/files/favicon"<&.ico'}
STAFF_BRAND = {"name": 'Counter "</script><script>evil()</script>', "logo": '/files/counter"<&.png', "favicon": '/files/counter"<&.ico'}


class PageParser(HTMLParser):

    def __init__(self, html):
        super().__init__()
        self.title = ""
        self.favicon = None
        self.source = None
        self.footer = ""
        self.in_title = False
        self.in_footer = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "footer":
            self.in_footer = True
        if tag == "link" and attrs.get("rel") == "icon":
            self.favicon = attrs.get("href")
        if tag == "a" and self.in_footer:
            self.source = attrs.get("href")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "footer":
            self.in_footer = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.in_footer:
            self.footer += data


class TestSiteBrand(unittest.TestCase):

    def setUp(self):
        self.settings = SimpleNamespace(app_name=None, app_logo=None, favicon=None)
        self.hooks = {}
        framework = ModuleType("frappe")
        framework.__path__ = []
        framework.get_cached_doc = lambda doctype: self.settings if doctype == "Website Settings" else self.fail(doctype)
        framework.get_hooks = lambda hook: self.hooks if hook == "ury_brand" else self.fail(hook)
        framework.as_json = json.dumps
        framework.session = SimpleNamespace(user="Guest")
        framework.local = SimpleNamespace(dev_server=0)
        framework.conf = {}
        framework.form_dict = {}
        framework.db = SimpleNamespace(commit=lambda: None)
        framework._ = lambda value: value

        def whitelist(**kwargs):
            def decorate(function):
                function.is_whitelisted = True
                return function
            return decorate

        framework.whitelist = whitelist
        sessions = ModuleType("frappe.sessions")
        sessions.get_csrf_token = lambda: "test-csrf"
        sessions.get = lambda: {"app_logo_url": "/assets/frappe/ignored.svg"}
        website = ModuleType("frappe.website")
        website.__path__ = []
        website_utils = ModuleType("frappe.website.utils")
        website_utils.get_boot_data = lambda: {"lang": "en"}
        self.active_theme = None
        theme_module = ModuleType("frappe.website.doctype.website_theme.website_theme")
        theme_module.get_active_theme = lambda: self.active_theme
        utils = ModuleType("frappe.utils")
        utils.__path__ = []
        utils.get_build_version = lambda: "test-version"
        utils.cint = lambda value: int(value or 0)
        telemetry = ModuleType("frappe.utils.telemetry")
        telemetry.capture = lambda *args, **kwargs: None
        framework.sessions = sessions
        framework.website = website
        framework.utils = utils
        website.utils = website_utils
        self.framework = framework
        self.enterContext(patch.dict(sys.modules, {
            "frappe": framework, "frappe.sessions": sessions, "frappe.website": website,
            "frappe.website.utils": website_utils, "frappe.utils": utils,
            "frappe.utils.telemetry": telemetry,
            "frappe.website.doctype.website_theme.website_theme": theme_module,
        }))
        for name in ["ury.brand", *(f"ury.www.{page}" for page in PAGES)]:
            sys.modules.pop(name, None)

    def brand_module(self):
        self.assertIsNotNone(importlib.util.find_spec("ury.brand"), "Website Settings branding helper is missing")
        return importlib.import_module("ury.brand")

    def set_brand(self):
        self.settings.app_name = SITE_BRAND["name"]
        self.settings.app_logo = SITE_BRAND["logo"]
        self.settings.favicon = SITE_BRAND["favicon"]

    def set_staff_brand(self):
        self.hooks = {key: ["earlier-value", value] for key, value in STAFF_BRAND.items()}

    def context(self, page):
        return importlib.import_module(f"ury.www.{page}").get_context({})

    def render(self, page, context):
        template = Environment(autoescape=False, loader=FileSystemLoader(ROOT / "ury")).from_string((ROOT / PAGES[page] / "index.html").read_text())
        return template.render(**context, app_name=self.settings.app_name or "", frappe=SimpleNamespace(lang="en", session=self.framework.session))

    def test_empty_settings_return_none_not_frappe_defaults(self):
        self.assertIsNone(self.brand_module().get_brand())

    def test_set_settings_return_only_the_three_public_brand_fields(self):
        self.set_brand()
        brand = self.brand_module()
        self.assertEqual(brand.get_brand(), SITE_BRAND)
        self.assertFalse(getattr(brand.get_brand, "is_whitelisted", False))

    def test_partial_settings_leave_other_fields_unset(self):
        self.settings.app_logo = "/files/tenant.png"
        self.assertEqual(self.brand_module().get_brand(), {"name": None, "logo": "/files/tenant.png", "favicon": None})

    def test_guest_page_uses_verified_profile_public_brand_only(self):
        self.set_brand()
        profile = SimpleNamespace(guest_display_name='Restaurant <img onerror="evil()">', restaurant="Restaurant A")
        restaurant = SimpleNamespace(image="/files/restaurant.png")
        self.framework.get_cached_doc = lambda doctype, name=None: self.settings if doctype == "Website Settings" else restaurant
        self.framework.form_dict = {"t": "signed-token"}
        api = ModuleType("ury.ury.api.self_ordering")
        from unittest.mock import Mock
        verify = Mock(return_value=(profile, "Table 7", "QR Table"))
        api._verify_qr_token = verify
        with patch.dict(sys.modules, {"ury.ury.api.self_ordering": api}):
            context = self.context("order")
        expected = {"name": profile.guest_display_name, "logo": restaurant.image, "favicon": SITE_BRAND["favicon"]}
        self.assertEqual(context["brand"], expected)
        verify.assert_called_once_with("signed-token")
        self.assertEqual(json.loads(json.loads(context["boot"]))["ury_brand"], expected)
        parsed = PageParser(self.render("order", context))
        self.assertEqual(parsed.title, profile.guest_display_name)
        self.assertEqual(self.brand_module().get_brand(), SITE_BRAND)

    def test_guest_profile_fields_are_optional_manager_editable_configuration(self):
        schema = json.loads((ROOT / "ury/ury/doctype/ury_self_ordering_profile/ury_self_ordering_profile.json").read_text())
        fields = {field["fieldname"]: field for field in schema["fields"]}
        for name, fieldtype in (("guest_display_name", "Data"),):
            with self.subTest(field=name):
                self.assertIn(name, fields, "Optional guest identity cannot be edited through a native profile")
                self.assertEqual(fields[name]["fieldtype"], fieldtype)
                self.assertFalse(fields[name].get("reqd") or fields[name].get("read_only"))
                self.assertIn(name, schema["field_order"])

    def test_unavailable_qr_profiles_keep_the_guest_page_renderable(self):
        self.set_brand()
        self.framework.PermissionError = type("PermissionError", (Exception,), {})
        self.framework.ValidationError = type("ValidationError", (Exception,), {})
        self.framework.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
        self.framework.form_dict = {"t": "disabled-or-invalid-token"}
        api = ModuleType("ury.ury.api.self_ordering")
        for error in (self.framework.PermissionError, self.framework.ValidationError, self.framework.DoesNotExistError):
            with self.subTest(error=error.__name__):
                def refuse(token):
                    raise error("Ordering unavailable")
                api._verify_qr_token = refuse
                with patch.dict(sys.modules, {"ury.ury.api.self_ordering": api}):
                    try:
                        context = self.context("order")
                    except Exception as failure:
                        self.fail(f"QR refusal must not break the guest HTML: {failure}")
                self.assertEqual(context["brand"], SITE_BRAND)

    def test_guest_brand_falls_back_per_field_and_ignores_unsigned_profile_names(self):
        self.set_brand()
        profile = SimpleNamespace(guest_display_name="", restaurant="Restaurant A")
        self.framework.get_cached_doc = lambda doctype, name=None: self.settings if doctype == "Website Settings" else SimpleNamespace(image=None)
        api = ModuleType("ury.ury.api.self_ordering")
        api._verify_qr_token = lambda token: (profile, "Table 7", "QR Table")
        with patch.dict(sys.modules, {"ury.ury.api.self_ordering": api}):
            self.framework.form_dict = {"t": "signed-token"}
            self.assertEqual(self.context("order")["brand"], SITE_BRAND)
            self.framework.form_dict = {"profile": "Restaurant B"}
            self.assertEqual(self.context("order")["brand"], SITE_BRAND)

    def test_staff_pages_use_last_hook_values(self):
        self.set_brand()
        self.set_staff_brand()
        for page in ("pos", "urypos", "mosaic"):
            with self.subTest(page=page):
                context = self.context(page)
                self.assertEqual(context["brand"], STAFF_BRAND)
                self.assertEqual(json.loads(context["brand_json"]), STAFF_BRAND)
                html = self.render(page, context)
                parser = ScriptParser(html)
                self.assertEqual(len(parser.inline), 1)
                result = subprocess.run(["node", "-e", "global.window=global;" + parser.inline[0] + ";process.stdout.write(JSON.stringify(window.frappe.boot.ury_brand));"], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), STAFF_BRAND)
                parsed = PageParser(html)
                self.assertEqual(parsed.title, STAFF_BRAND["name"])
                self.assertEqual(parsed.favicon, STAFF_BRAND["favicon"])

    def test_staff_hook_works_without_website_brand(self):
        self.set_staff_brand()
        for page in ("pos", "urypos", "mosaic"):
            with self.subTest(page=page):
                self.assertEqual(self.context(page)["brand"], STAFF_BRAND)

    def test_partial_staff_hook_falls_back_per_key(self):
        self.set_brand()
        for key, value in STAFF_BRAND.items():
            self.hooks = {key: ["earlier-value", value]}
            expected = {**SITE_BRAND, key: value}
            for page in ("pos", "urypos", "mosaic"):
                with self.subTest(key=key, page=page):
                    self.assertEqual(self.context(page)["brand"], expected)

    def test_staff_pages_without_hook_use_website_settings(self):
        self.set_brand()
        for page in ("pos", "urypos", "mosaic"):
            with self.subTest(page=page):
                self.assertEqual(self.context(page)["brand"], SITE_BRAND)

    def test_malformed_staff_hook_is_ignored_not_fatal(self):
        # Frappe returns a list for a hook declared as a plain string, and a
        # dict of lists for a dict hook. Only the dict shape is a brand.
        self.set_brand()
        for hooks in (["Counter"], [], "Counter", {"name": "Counter"}, {"name": []}):
            self.hooks = hooks
            for page in ("pos", "urypos", "mosaic"):
                with self.subTest(hooks=hooks, page=page):
                    self.assertEqual(self.context(page)["brand"], SITE_BRAND)

    def test_order_and_default_brand_ignore_staff_hook(self):
        self.set_brand()
        self.set_staff_brand()
        self.assertEqual(self.brand_module().get_brand(), SITE_BRAND)
        self.assertEqual(self.brand_module().get_brand_context()["brand"], SITE_BRAND)
        context = self.context("order")
        self.assertEqual(context["brand"], SITE_BRAND)
        self.assertEqual(json.loads(json.loads(context["boot"]))["ury_brand"], SITE_BRAND)
        parsed = PageParser(self.render("order", context))
        self.assertEqual(parsed.title, SITE_BRAND["name"])
        self.assertEqual(parsed.favicon, SITE_BRAND["favicon"])

    def test_authenticated_pos_and_dev_boot_use_staff_hook(self):
        self.set_brand()
        self.set_staff_brand()
        self.framework.session.user = "cashier@example.com"
        controller = importlib.import_module("ury.www.pos")
        for boot in (self.context("pos")["boot"], controller.get_boot()):
            with self.subTest(boot=boot):
                self.assertEqual(json.loads(json.loads(boot))["ury_brand"], STAFF_BRAND)

    def test_ury_dashboard_uses_staff_brand_in_context_boot_and_html(self):
        self.set_brand()
        self.set_staff_brand()
        self.framework.session.user = "manager@example.com"
        controller = importlib.import_module("ury.www.ury")

        context = controller.get_context({})
        self.assertEqual(context["brand"], STAFF_BRAND)
        self.assertEqual(json.loads(json.loads(context["boot"]))["ury_brand"], STAFF_BRAND)
        self.assertEqual(json.loads(json.loads(controller.get_boot()))["ury_brand"], STAFF_BRAND)

        template = Environment(autoescape=False, loader=FileSystemLoader(ROOT / "frontend")).from_string(
            (ROOT / "frontend/index.html").read_text()
        )
        html = template.render(
            **context,
            app_name=self.settings.app_name or "",
            frappe=SimpleNamespace(lang="en", session=self.framework.session),
        )
        parsed = PageParser(html)
        self.assertEqual(parsed.title, STAFF_BRAND["name"])
        self.assertEqual(parsed.favicon, STAFF_BRAND["favicon"])

    def test_all_controllers_preserve_unset_brand(self):
        for page in PAGES:
            with self.subTest(page=page):
                context = self.context(page)
                self.assertIn("brand", context)
                self.assertIsNone(context["brand"])

    def test_all_pages_roundtrip_brand_without_script_breakout(self):
        self.set_brand()
        for page in PAGES:
            with self.subTest(page=page):
                context = self.context(page)
                self.assertEqual(context.get("brand"), SITE_BRAND)
                html = self.render(page, context)
                # Execute the actual rendered boot script, not a copied serializer.
                parser = ScriptParser(html)
                self.assertEqual(len(parser.inline), 1)
                result = subprocess.run(["node", "-e", "global.window=global;" + parser.inline[0] + ";process.stdout.write(JSON.stringify(window.frappe.boot.ury_brand));"], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), SITE_BRAND)
                parsed = PageParser(html)
                self.assertEqual(parsed.title, SITE_BRAND["name"])
                self.assertEqual(parsed.favicon, SITE_BRAND["favicon"])

    def test_unset_templates_keep_current_titles_and_favicons(self):
        for page in PAGES:
            with self.subTest(page=page):
                parsed = PageParser(self.render(page, self.context(page)))
                self.assertEqual(parsed.title, DEFAULT_TITLES[page])
                self.assertEqual(parsed.favicon, "/ury.ico")

    def test_pos_and_guest_order_offer_public_source_and_escaped_brand(self):
        self.set_brand()
        for page in ("pos", "order"):
            with self.subTest(page=page):
                context = self.context(page)
                parsed = PageParser(self.render(page, context))
                self.assertIn(SITE_BRAND["name"] + " · built on URY (AGPL-3.0) · Source", " ".join(parsed.footer.split()))
                self.assertEqual(parsed.source, "https://github.com/ury-erp/ury")
                self.framework.conf["ury_source_url"] = "https://github.com/pmwanje2/ury/tree/public-release"
                parsed = PageParser(self.render(page, self.context(page)))
                self.assertEqual(parsed.source, "https://github.com/pmwanje2/ury/tree/public-release")
                self.framework.conf.clear()

    def test_authenticated_pos_and_dev_boot_include_site_brand(self):
        self.set_brand()
        self.framework.session.user = "cashier@example.com"
        controller = importlib.import_module("ury.www.pos")
        boot = json.loads(json.loads(self.context("pos")["boot"]))
        self.assertEqual(boot.get("ury_brand"), SITE_BRAND)
        self.assertEqual(json.loads(json.loads(controller.get_boot())).get("ury_brand"), SITE_BRAND)

    def test_boot_keeps_frappe_serialization_for_datetime_values(self):
        self.set_brand()
        self.framework.as_json = lambda value, **kwargs: json.dumps(value, default=lambda item: item.isoformat())
        self.framework.website.utils.get_boot_data = lambda: {"server_time": datetime(2026, 10, 5, 12, 30)}
        for page in ("pos", "order"):
            with self.subTest(page=page):
                try:
                    boot = json.loads(json.loads(self.context(page)["boot"]))
                except TypeError as error:
                    self.fail(f"Page boot must retain Frappe datetime serialization: {error}")
                self.assertEqual(boot["server_time"], "2026-10-05T12:30:00")
                self.assertEqual(boot["ury_brand"], SITE_BRAND)

    def theme_module(self):
        module = self.brand_module()
        self.assertTrue(callable(getattr(module, "get_theme", None)), "Native Website Theme token reader is missing")
        self.assertTrue(callable(getattr(module, "theme_style", None)), "Server-side theme renderer is missing")
        return module

    def set_theme(self, **overrides):
        self.active_theme = SimpleNamespace(**{
            "name": "Salama Hospitality", "primary_color": "#23664a", "text_color": "#1f2a24",
            "background_color": "#f2ede4", "dark_color": "#23664a", "google_font": "Noto Sans",
            "font_size": "14px", "button_rounded_corners": 1, **overrides,
        })

    def theme_tokens(self):
        module = self.theme_module()
        style = module.theme_style(module.get_theme())
        self.assertTrue(style.startswith("html:root:not(.dark){"), style)
        import re
        return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", style))

    def test_no_theme_and_standard_preserve_the_existing_look(self):
        module = self.theme_module()
        for theme in (None, SimpleNamespace(name="Standard", primary_color="#23664a")):
            with self.subTest(theme=theme):
                self.active_theme = theme
                self.assertIsNone(module.get_theme())
                self.assertEqual(module.theme_style(module.get_theme()), "")
                html = self.render("pos", self.context("pos"))
                self.assertNotIn("html:root:not(.dark)", html)

    def test_native_theme_emits_only_validated_public_tokens_never_code(self):
        self.set_theme(js="evil()", custom_scss="body{display:none}", theme_scss="evil", custom_overrides="evil",
                       font_properties="evil", theme_url="https://evil.invalid", custom_is_active=1)
        theme = self.theme_module().get_theme()
        self.assertEqual(theme, {"primary": "#23664a", "text": "#1f2a24", "background": "#f2ede4",
                                 "dark": "#23664a", "font": "Noto Sans", "font_size": "14px", "rounded": True})
        self.assertFalse(getattr(self.theme_module().get_theme, "is_whitelisted", False))

    def test_invalid_hex_values_are_dropped_per_field(self):
        module = self.theme_module()
        for value in ("#fff", "#gggggg", "red", "#123456;evil", "#123456\n", None, 123456):
            with self.subTest(value=value):
                self.set_theme(primary_color=value, text_color=value, background_color=value, dark_color=value)
                self.assertEqual(set(module.get_theme()), {"font", "font_size", "rounded"})
        self.set_theme(primary_color="#ABCDEF")
        self.assertEqual(module.get_theme()["primary"], "#ABCDEF")

    def test_invalid_fonts_sizes_and_check_values_are_dropped(self):
        module = self.theme_module()
        for value in ("1Font", "Noto Sans;evil", '</style><script>evil()</script>', "A" * 41, "Noto\n", None, 14):
            with self.subTest(font=value):
                self.set_theme(google_font=value)
                self.assertNotIn("font", module.get_theme())
        for value in ("11px", "25px", "14", "14rem", "14px;evil", "14px\n", None, 14):
            with self.subTest(size=value):
                self.set_theme(font_size=value)
                self.assertNotIn("font_size", module.get_theme())
        for value in ("12px", "24px"):
            self.set_theme(font_size=value)
            self.assertEqual(module.get_theme()["font_size"], value)
        for value in (None, "evil", 2):
            self.set_theme(button_rounded_corners=value)
            self.assertNotIn("rounded", module.get_theme())

    def test_hospitality_scale_mixes_brand_rgb_toward_white_and_black(self):
        self.set_theme()
        tokens = self.theme_tokens()
        self.assertEqual(tokens["--primary"], "154.9254 48.9051% 26.8627%")
        self.assertEqual(tokens["--ring"], tokens["--primary"])
        self.assertEqual(tokens["--button-foreground"], "var(--primary-foreground)")
        for step, expected_rgb in (
            (50, (244, 247.35, 245.95)), (100, (233, 239.7, 236.9)),
            (200, (200, 216.75, 209.75)), (300, (167, 193.8, 182.6)),
            (400, (123, 163.2, 146.4)), (500, (79, 132.6, 110.2)),
            (600, (35, 102, 74)), (700, (29.75, 86.7, 62.9)),
            (800, (24.5, 71.4, 51.8)), (900, (19.25, 56.1, 40.7)),
            (950, (14, 40.8, 29.6)),
        ):
            with self.subTest(step=step):
                h, s, l = map(float, tokens[f"--primary-{step}"].replace("%", "").split())
                rgb = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
                for actual, expected in zip(rgb, expected_rgb):
                    self.assertAlmostEqual(actual * 255, expected, delta=0.001)

    def test_primary_600_and_blue_600_keep_the_exact_owner_brand(self):
        for color, expected in (("#23664a", "154.9254 48.9051% 26.8627%"),
                                ("#174a8b", "213.6207 71.6049% 31.7647%"),
                                ("#767676", "0 0% 46.2745%")):
            with self.subTest(color=color):
                self.set_theme(primary_color=color)
                tokens = self.theme_tokens()
                for name in ("--primary", "--primary-600", "--blue-600"):
                    self.assertEqual(tokens[name], expected)

    def test_brand_scale_has_near_white_50_and_darker_700(self):
        for color in ("#23664a", "#174a8b", "#767676"):
            with self.subTest(color=color):
                self.set_theme(primary_color=color)
                tokens = self.theme_tokens()
                channels = {}
                for step in (50, 600, 700):
                    h, s, l = map(float, tokens[f"--primary-{step}"].replace("%", "").split())
                    channels[step] = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
                self.assertTrue(all(240 / 255 < channel < 1 for channel in channels[50]))
                self.assertTrue(all(shade < base for shade, base in zip(channels[700], channels[600])))

    def test_white_text_passes_aa_on_600_and_all_brand_shades(self):
        for color in ("#23664a", "#174a8b", "#767676"):
            self.set_theme(primary_color=color)
            tokens = self.theme_tokens()
            for step in (600, 700, 800, 900, 950):
                with self.subTest(color=color, step=step):
                    h, s, l = map(float, tokens[f"--primary-{step}"].replace("%", "").split())
                    rgb = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
                    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
                    luminance = sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
                    self.assertGreaterEqual(1.05 / (luminance + 0.05), 4.5)
                    self.assertEqual(tokens[f"--blue-{step}"], tokens[f"--primary-{step}"])

    def test_hospitality_remaps_blue_utilities_to_the_generated_primary_scale(self):
        self.set_theme(primary_color="#23664a")
        tokens = self.theme_tokens()
        self.assertEqual(tokens.get("--blue-600"), "154.9254 48.9051% 26.8627%")
        for step in (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950):
            with self.subTest(step=step):
                self.assertEqual(tokens.get(f"--blue-{step}"), tokens[f"--primary-{step}"])

    def test_theme_without_a_valid_primary_preserves_stock_blue_utilities(self):
        self.set_theme(primary_color="invalid")
        self.assertFalse(any(key.startswith("--blue-") for key in self.theme_tokens()))

    def test_background_text_and_header_keep_their_own_tokens(self):
        self.set_theme()
        tokens = self.theme_tokens()
        self.assertEqual(tokens["--background"], "38.5714 35% 92.1569%")
        self.assertEqual(tokens["--gray-50"], tokens["--background"])
        self.assertEqual(tokens["--foreground"], "147.2727 15.0685% 14.3137%")
        self.assertEqual(tokens["--gray-900"], tokens["--foreground"])
        self.assertEqual(tokens["--header"], tokens["--primary"])
        for step in (100, 200, 300, 400, 500, 600, 700, 800, 950):
            hue, saturation, _ = tokens[f"--gray-{step}"].split()
            self.assertEqual(hue, "38.5714")
            self.assertLessEqual(float(saturation[:-1]), 25)

    def test_derived_text_grays_pass_aa_on_canvas_and_white_cards(self):
        def luminance(rgb):
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
            return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        for background, text in (("#f2ede4", "#1f2a24"), ("#eef1f5", "#1f2933"),
                                 ("#ffffff", None), ("#767676", None)):
            self.set_theme(background_color=background, text_color=text)
            tokens = self.theme_tokens()
            canvas = luminance([int(background[i:i + 2], 16) / 255 for i in (1, 3, 5)])
            for step in (500, 600, 700, 800, 900, 950):
                h, s, l = map(float, tokens[f"--gray-{step}"].replace("%", "").split())
                rgb = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
                for precision, channels in (("CSS", rgb), ("8-bit", [round(v * 255) / 255 for v in rgb])):
                    gray = luminance(channels)
                    for surface, value in (("canvas", canvas), ("white card", 1)):
                        with self.subTest(background=background, step=step, surface=surface, precision=precision):
                            lo, hi = sorted((gray, value))
                            self.assertGreaterEqual((hi + 0.05) / (lo + 0.05), 4.5)
                if step != 900 or text is None:
                    self.assertLessEqual(s, 25)
                    expected_hue = 38.5714 if background == "#f2ede4" else 214.2857 if background == "#eef1f5" else 0
                    self.assertAlmostEqual(h, expected_hue, places=4)

    def test_font_size_changes_body_text_not_root_rem_sizing(self):
        for size in ("12px", "14px", "24px"):
            with self.subTest(size=size):
                self.set_theme(font_size=size)
                html = self.render("pos", self.context("pos"))
                rules = dict(re.findall(r"([^{}<>]+)\{([^{}]+)\}", html))
                self.assertNotIn("font-size:", rules["html:root:not(.dark)"])
                self.assertEqual(rules.get("html:root:not(.dark) body"), f"font-size:{size};")
        self.set_theme(font_size=None)
        self.assertNotIn("font-size:", self.context("pos")["theme_style"])

    def test_light_header_subtitle_uses_the_derived_muted_gray(self):
        self.set_theme(name="Maximus Essential", primary_color="#174a8b", text_color="#1f2933",
                       background_color="#eef1f5", dark_color="#ffffff")
        tokens = self.theme_tokens()
        self.assertEqual(tokens["--header-muted"], "var(--gray-500)")
        self.assertNotEqual(tokens["--header-muted"], tokens["--header-title"])
        self.assertEqual(tokens["--header-title"], tokens["--header-foreground"])

    def test_dark_header_keeps_white_muted_text_for_contrast(self):
        self.set_theme()
        self.assertEqual(self.theme_tokens()["--header-muted"], "0 0% 100%")

    def test_foregrounds_choose_white_or_dark_by_real_contrast(self):
        for color, foreground in (("#23664a", "0 0% 100%"), ("#174a8b", "0 0% 100%"),
                                  ("#ffffff", "240 1% 5%"), ("#ffff00", "240 1% 5%")):
            with self.subTest(color=color):
                self.set_theme(primary_color=color, dark_color=color, background_color=None)
                tokens = self.theme_tokens()
                self.assertEqual(tokens["--primary-foreground"], foreground)
                self.assertEqual(tokens["--header-foreground"], foreground)

    def test_mid_gray_foregrounds_still_pass_aa_when_default_gray950_does_not(self):
        import colorsys

        def luminance(hsl):
            h, s, l = map(float, hsl.replace("%", "").split())
            rgb = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
            return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        self.set_theme(primary_color="#777777", dark_color="#777777")
        tokens = self.theme_tokens()
        self.assertEqual(tokens["--primary-foreground"], tokens["--gray-950"])
        self.assertEqual(tokens["--header-foreground"], tokens["--gray-950"])
        for prefix in ("primary", "header"):
            pair = sorted([luminance(tokens[f"--{prefix}"]), luminance(tokens[f"--{prefix}-foreground"])])
            self.assertGreaterEqual((pair[1] + 0.05) / (pair[0] + 0.05), 4.5)

    def test_rounded_off_and_valid_font_render_without_extra_scripts(self):
        self.set_theme(button_rounded_corners=0, google_font="Noto Sans 2")
        tokens = self.theme_tokens()
        self.assertEqual(tokens["--radius"], "0")
        self.assertEqual(tokens["--font-sans"], r"Noto Sans \32 , sans-serif")
        self.assertEqual(tokens["--font-inter"], "var(--font-sans)")
        html = self.render("pos", self.context("pos"))
        self.assertIn("font-size:14px;", html)
        self.assertIn("family=Noto+Sans+2:wght@400;500;600;700&amp;display=swap", html)
        self.assertEqual(len(ScriptParser(html).inline), 1)
        self.set_theme(button_rounded_corners=1)
        self.assertNotIn("--radius", self.theme_tokens())

    def test_valid_google_font_numeric_words_are_escaped_as_css_identifiers(self):
        for font, css_family in (("Press Start 2P", r"Press Start \32 P"),
                                 ("M PLUS 1p", r"M PLUS \31 p")):
            with self.subTest(font=font):
                self.set_theme(google_font=font)
                html = self.render("pos", self.context("pos"))
                self.assertIn(f"--font-sans:{css_family}, sans-serif;", html)
                self.assertNotIn("&#", html)

    def test_digit_only_font_words_preserve_the_following_word_boundary(self):
        self.set_theme(google_font="M PLUS 1 Code")
        html = self.render("pos", self.context("pos"))
        # First space terminates the CSS escape; the second separates words.
        self.assertIn(r"--font-sans:M PLUS \31  Code, sans-serif;", html)
        self.assertIn("family=M+PLUS+1+Code:wght", html)

    def test_invalid_code_payload_cannot_break_out_of_pos_style_or_link(self):
        self.set_theme(primary_color="</style><script>evil()</script>", google_font='Noto\" onload=evil()',
                       js="evil()", theme_scss="evil()")
        html = self.render("pos", self.context("pos"))
        self.assertNotIn("evil", html)
        self.assertNotIn("onload", html)
        self.assertEqual(len(ScriptParser(html).inline), 1)

    def test_pos_context_injects_native_theme_before_the_app_not_into_guest_pages(self):
        self.set_theme()
        html = self.render("pos", self.context("pos"))
        self.assertIn("--header:154.9254 48.9051% 26.8627%;", html)
        self.assertLess(html.index("html:root:not(.dark)"), html.index('id="root"'))
        self.assertNotIn("html:root:not(.dark)", self.render("order", self.context("order")))

    def test_vite_build_preserves_the_server_theme_style_for_frappe(self):
        from tempfile import TemporaryDirectory

        self.set_theme()
        with TemporaryDirectory(prefix="ury-x54-build-") as output:
            result = subprocess.run(["node", "node_modules/vite/bin/vite.js", "build", "--outDir", output],
                                    cwd=ROOT / "pos", text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            template = Environment(autoescape=False, loader=FileSystemLoader(ROOT / "ury")).from_string((Path(output) / "index.html").read_text())
            html = template.render(**self.context("pos"), frappe=SimpleNamespace(lang="en"))
            self.assertIn("<style>html:root:not(.dark){", html)
            self.assertIn("--header:154.9254 48.9051% 26.8627%;", html)


class ScriptParser(HTMLParser):

    def __init__(self, html):
        super().__init__()
        self.inline = []
        self.in_inline = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "script" and "src" not in dict(attrs):
            self.inline.append("")
            self.in_inline = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_inline = False

    def handle_data(self, data):
        if self.in_inline:
            self.inline[-1] += data
