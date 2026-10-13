"""Public site identity for URY pages; deliberately not an RPC endpoint."""

import colorsys
import re
from urllib.parse import quote_plus

import frappe

DEFAULT_SOURCE_URL = "https://github.com/ury-erp/ury"


def get_theme():
    """Read native Website Theme data only; never expose its executable fields."""
    from frappe.website.doctype.website_theme.website_theme import get_active_theme

    theme = get_active_theme()
    if not theme or theme.name == "Standard":
        return None
    tokens = {}
    for key in ("primary", "text", "background", "dark"):
        value = getattr(theme, f"{key}_color", None)
        if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            tokens[key] = value
    for key, field, pattern in (
        ("font", "google_font", r"[A-Za-z][A-Za-z0-9 ]{0,39}"),
        ("font_size", "font_size", r"(1[2-9]|2[0-4])px"),
    ):
        value = getattr(theme, field, None)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            tokens[key] = value
    rounded = getattr(theme, "button_rounded_corners", None)
    if isinstance(rounded, (bool, int)) and rounded in (0, 1):
        tokens["rounded"] = bool(rounded)
    return tokens


def theme_style(theme):
    """Map validated theme data to the shared light-mode HSL token vocabulary."""
    if not theme:
        return ""

    def hsl(color):
        rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        h, l, s = colorsys.rgb_to_hls(*rgb)
        return h * 360, s * 100, l * 100

    def token(h, s, l):
        parts = [f"{value:.4f}".rstrip("0").rstrip(".") for value in (h, s, l)]
        return f"{parts[0]} {parts[1]}% {parts[2]}%"

    def luminance(color):
        h, s, l = color
        rgb = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    def contrast(a, b):
        lo, hi = sorted((luminance(a), luminance(b)))
        return (hi + 0.05) / (lo + 0.05)

    values = {}
    gray950 = (240, 1, 5)
    if "background" in theme:
        h, s, l = hsl(theme["background"])
        values["--background"] = values["--gray-50"] = token(h, s, l)
        for step, sat, light in ((100, 20, 97), (200, 15, 92), (300, 10, 85), (400, 8, 70),
                                 (500, 6, 50), (600, 5, 40), (700, 4, 30), (800, 3, 20), (900, 2, 10), (950, 1, 5)):
            values[f"--gray-{step}"] = token(h, min(s, sat, 25), light)
        gray950 = (h, min(s, 1), 5)
    if "text" in theme:
        values["--foreground"] = values["--gray-900"] = token(*hsl(theme["text"]))

    # For the small mid-luminance gap where neither white nor the default
    # gray-950 passes AA, deepen that same gray token to black.
    colors = [hsl(theme[key]) for key in ("primary", "dark") if key in theme]
    if any(contrast(color, (0, 0, 100)) < 4.5 and contrast(color, gray950) < 4.5 for color in colors):
        gray950 = (gray950[0], gray950[1], 0)
        values["--gray-950"] = token(*gray950)
    for key, target in (("primary", "primary"), ("dark", "header")):
        if key not in theme:
            continue
        color = hsl(theme[key])
        values[f"--{target}"] = token(*color)
        foreground = (0, 0, 100) if contrast(color, (0, 0, 100)) >= 4.5 else gray950
        values[f"--{target}-foreground"] = token(*foreground)
        if target == "header":
            for part in ("user", "title", "muted"):
                values[f"--header-{part}"] = token(*foreground)
    if "primary" in theme:
        h, s, _ = hsl(theme["primary"])
        values["--ring"] = values["--primary"]
        values["--button-foreground"] = "var(--primary-foreground)"
        for step, light in ((50, 97), (100, 94), (200, 88), (300, 80), (400, 70), (500, 61.7647),
                            (600, 55), (700, 46), (800, 37), (900, 28), (950, 18)):
            values[f"--blue-{step}"] = values[f"--primary-{step}"] = token(h, s, light)
        values["--header-border"] = "var(--primary)"
    if theme.get("rounded") is False:
        values["--radius"] = "0"
    if "font" in theme:
        # Escape a leading digit in each CSS identifier without introducing
        # quotes that HTML escaping would turn into raw-text entities.
        font = re.sub(r"(?<!\S)([0-9])", lambda match: f"\\{ord(match[1]):x} ", theme["font"])
        values["--font-sans"] = f"{font}, sans-serif"
        values["--font-inter"] = "var(--font-sans)"
    if "font_size" in theme:
        values["font-size"] = theme["font_size"]
    return "html:root:not(.dark){" + "".join(f"{key}:{value};" for key, value in values.items()) + "}"


def get_theme_context():
    theme = get_theme()
    return {
        "theme_style": theme_style(theme),
        "theme_font_url": (
            "https://fonts.googleapis.com/css2?family=" + quote_plus(theme["font"])
            + ":wght@400;500;600;700&display=swap"
            if theme and "font" in theme else None
        ),
    }


def get_brand(surface="site"):
    settings = frappe.get_cached_doc("Website Settings")
    brand = {
        "name": settings.app_name or None,
        "logo": settings.app_logo or None,
        "favicon": settings.favicon or None,
    }
    if surface == "guest":
        token = getattr(frappe, "form_dict", {}).get("t")
        if token:
            # Never accept an unsigned restaurant/profile name from the URL.
            from ury.ury.api.self_ordering import _verify_qr_token

            try:
                profile, _, _ = _verify_qr_token(token)
            except (frappe.PermissionError, frappe.ValidationError, frappe.DoesNotExistError, TypeError):
                pass  # Invalid links keep site identity; the API reports the refusal.
            else:
                restaurant = frappe.get_cached_doc("URY Restaurant", profile.restaurant)
                brand["name"] = getattr(profile, "guest_display_name", None) or brand["name"]
                brand["logo"] = restaurant.image or brand["logo"]
    if surface == "staff":
        # A dict hook arrives as {key: [values...]}; any other shape is ignored.
        hooks = frappe.get_hooks("ury_brand")
        if not isinstance(hooks, dict):
            hooks = {}
        for key in brand:
            values = hooks.get(key)
            if isinstance(values, list) and values and values[-1]:
                brand[key] = values[-1]
    return brand if any(brand.values()) else None


def json_for_script(value):
    """Preserve JSON values without allowing an HTML script tag to close."""
    return (
        frappe.as_json(value, indent=None, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def get_brand_context(surface="site"):
    brand = get_brand(surface=surface)
    return {
        "brand": brand,
        "brand_json": json_for_script(brand),
        "brand_source_url": frappe.conf.get("ury_source_url") or DEFAULT_SOURCE_URL,
    }
