"""The committed dashboard.css must still match the markup it was built from.

Dropping the CDNs traded a runtime compiler for a build artifact, and a
build artifact can go stale: add a class to dashboard.html, forget to run
tools/build_dashboard_assets.py, and the page loads with that element
unstyled. Nothing else would notice. These tests do.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "alpha_bot" / "static"
HTML = STATIC / "dashboard.html"
CSS = STATIC / "dashboard.css"

WEIGHTS = {"ph-bold", "ph-fill", "ph-thin", "ph-light", "ph-duotone"}
ICON = re.compile(r"(?<![\w-])(ph-[a-z0-9]+(?:-[a-z0-9]+)*)(?![\w-])")

# Tailwind writes selectors with CSS-escaped punctuation: w-1/2 becomes
# .w-1\/2, sm:flex becomes .sm\:flex.
# Classes that exist only so JS can find the element (querySelectorAll
# ('.oFilter')). They are camelCase here by convention; Tailwind
# utilities never are, so the shape alone separates them.
HOOK = re.compile(r"[a-z]+[A-Z]\w*")

ESCAPE = re.compile(r"([:/.\[\]()%!#'\",<>=+*&$])")


class DashboardAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML.read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")

    def test_no_external_resources(self) -> None:
        """The ops screen must render with the network down.

        It is the screen you open when something has gone wrong, which is
        exactly when a third-party CDN is the worst thing to depend on.

        Scoped to what the page *fetches* to render itself. An <a href> is
        a place the operator may choose to go, not a dependency: it costs
        nothing when the network is gone, so linking out to a doc or a
        token issuer stays allowed.
        """
        external = [
            *re.findall(
                r'<(?:script|img|iframe|source|video|audio|embed)\b[^>]*\bsrc="(https?://[^"]+)"',
                self.html,
            ),
            *re.findall(r'<link\b[^>]*\bhref="(https?://[^"]+)"', self.html),
            *re.findall(r"url\(\s*['\"]?(https?://[^)'\"]+)", self.html),
        ]
        self.assertEqual(external, [], f"dashboard.html fetches from outside: {external}")

    def test_every_icon_has_a_glyph(self) -> None:
        """Subsetting keeps only the glyphs in use — so 'in use' must be right.

        Icons named in JS lookup tables rather than class attributes are the
        ones that get missed, and those happen to be the safety bar's own
        ok/halted/down states.
        """
        icons = set(ICON.findall(self.html)) - WEIGHTS
        missing = sorted(i for i in icons if f".{i}:before" not in self.css)
        self.assertEqual(missing, [], f"no glyph subset for {missing} — rebuild assets")

    def test_every_utility_class_is_compiled(self) -> None:
        """Classes the page uses but the stylesheet never defines."""
        local = set(re.findall(r"\.([a-zA-Z][\w-]*)\s*[,{:]", self._inline_style()))
        candidates: set[str] = set()
        for attr in re.findall(r'class="([^"]*)"', self.html):
            if "${" in attr:  # interpolated at runtime; covered by its own literals
                continue
            candidates.update(attr.split())

        missing = sorted(
            c for c in candidates
            if not c.startswith("ph")
            and not HOOK.fullmatch(c)
            and c not in local
            and f".{ESCAPE.sub(r'\\\1', c)}" not in self.css
        )
        self.assertEqual(missing, [], f"{missing} not in dashboard.css — rebuild assets")

    def _inline_style(self) -> str:
        return "\n".join(re.findall(r"<style>(.*?)</style>", self.html, re.S))


if __name__ == "__main__":
    unittest.main()
