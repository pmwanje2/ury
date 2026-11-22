"""Verify the installed/offline launcher artwork, including adaptive-icon margins."""
from pathlib import Path
import unittest

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]


class InstalledIcons(unittest.TestCase):
    def test_launcher_images_have_visible_artwork_inside_the_safe_area(self):
        for base in ("serve/public/icons", "ury/public/brief/icons"):
            for icon in (ROOT / base).glob("*.png"):
                with self.subTest(icon=str(icon)):
                    with Image.open(icon) as source:
                        image = source.convert("RGB")
                    background = Image.new("RGB", image.size, image.getpixel((0, 0)))
                    bounds = ImageChops.difference(image, background).getbbox()
                    self.assertIsNotNone(bounds, "launcher contains no visible artwork")
                    margin = image.width // 10
                    self.assertGreaterEqual(min(bounds[:2]), margin)
                    self.assertLessEqual(max(bounds[2:]), image.width - margin)
