#!/usr/bin/env python3
"""Build the dashboard's vendored stylesheet.

The dashboard used to pull Tailwind, Pretendard and Phosphor from three
CDNs at page load. That put the ops screen — the thing you open when
something has gone wrong — behind three third-party networks, and the
Tailwind CDN in particular ships a compiler that rebuilds every class on
every load. This script does that work once, offline, and the result is
committed as ``static/dashboard.css``.

Run it after adding, removing or renaming any class in dashboard.html::

    python3 tools/build_dashboard_assets.py

Needs network, ``npx`` and ``fonttools[woff]`` — all build-time only, none
of them a runtime dependency of the bot. ``tests/test_dashboard_assets.py``
fails if the committed CSS has drifted from the markup, so a forgotten
rebuild surfaces as a red test rather than as a silently unstyled page.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "alpha_bot" / "static"
HTML = STATIC / "dashboard.html"
OUT = STATIC / "dashboard.css"

PHOSPHOR_PKG = "@phosphor-icons/web@2"
TAILWIND_PKG = "tailwindcss@3"

# Only the weights the dashboard actually uses. Adding one here is not
# enough — it must also appear in the markup for its glyphs to survive
# subsetting.
WEIGHTS: dict[str, tuple[str, str, str, str]] = {
    # class token: (package folder, woff2 name, css selector, font-family)
    "ph": ("regular", "Phosphor.woff2", ".ph", "Phosphor"),
    "ph-bold": ("bold", "Phosphor-Bold.woff2", ".ph-bold", "Phosphor-Bold"),
    "ph-fill": ("fill", "Phosphor-Fill.woff2", ".ph-fill", "Phosphor-Fill"),
}
WEIGHT_TOKENS = set(WEIGHTS) | {"ph-thin", "ph-light", "ph-duotone"}

# Pretendard first so a locally installed copy still wins, then the
# platform's own Korean UI face. Downloading ~1.5MB of Korean webfont to
# render a localhost page is not a trade worth making, and subsetting is
# not open to us either: the dashboard renders arbitrary Korean text
# straight from the venue.
SANS_FAMILIES = [
    "Pretendard", "-apple-system", "BlinkMacSystemFont", "Apple SD Gothic Neo",
    "Segoe UI", "Malgun Gothic", "Noto Sans KR", "sans-serif",
]

TAILWIND_CONFIG = """
module.exports = {
  content: [%(html)r],
  theme: {
    extend: {
      fontFamily: { sans: %(sans)s },
      colors: {
        bg: '#f4f6f8', card: '#ffffff', text: '#191f28', subtext: '#6b7684',
        mute: '#8b95a1', border: '#e5e8eb', tossblue: '#3182f6',
        blueDark: '#1b64da', blueSoft: '#e8f3ff', rise: '#f04452',
        riseSoft: '#fceced', fall: '#3182f6', fallSoft: '#e8f3ff',
        warn: '#f59e0b', warnSoft: '#fef3c7', good: '#10b981',
        goodSoft: '#d1fae5', dim: '#f2f4f6',
      },
      borderRadius: { 'toss': '20px', 'toss-sm': '12px', 'toss-xs': '8px' },
      boxShadow: {
        'toss': '0 1px 4px rgba(0,0,0,0.04), 0 4px 16px rgba(0,0,0,0.04)',
        'tossLg': '0 8px 32px rgba(0,0,0,0.08)',
      },
    },
  },
};
"""


def icon_usage(html: str) -> dict[str, set[str]]:
    """Map each weight to the icons rendered in it.

    Scanning ``class="..."`` alone is not enough: the safety bar and the
    decision feed keep their icons in JS lookup tables and interpolate
    them later, so a naive attribute scan silently dropped the ok/halted/
    down glyphs — the three that matter most when the bar turns red.
    Quoted string literals are therefore scanned too, and an icon named
    without a weight beside it is emitted in every weight, since which
    one it lands in is decided at runtime.
    """
    groups = re.findall(r'class="([^"]*)"', html)
    groups += re.findall(r"'([^'\n]*)'", html)
    groups += re.findall(r'"([^"\n]*)"', html)

    # Match names, not whitespace-delimited tokens: a JS string literal
    # holding markup ("<i class=\'ph ph-x\'></i>") splits into fragments
    # that still start with "ph-" but carry the closing tag with them.
    name_re = re.compile(r"(?<![\w-])(ph-[a-z0-9]+(?:-[a-z0-9]+)*)(?![\w-])")
    plain_re = re.compile(r"(?<![\w-])ph(?![\w-])")

    used: dict[str, set[str]] = {w: set() for w in WEIGHTS}
    for group in groups:
        names = set(name_re.findall(group))
        icons = names - WEIGHT_TOKENS
        if not icons:
            continue
        weight = next((w for w in WEIGHTS if w in names), None)
        if weight is None and plain_re.search(group):
            weight = "ph"
        targets = [weight] if weight else list(WEIGHTS)
        for w in targets:
            used[w].update(icons)
    return used


def build_icons(pkg_src: Path, used: dict[str, set[str]]) -> str:
    from fontTools.subset import Options, Subsetter
    from fontTools.ttLib import TTFont

    parts: list[str] = []
    for wclass, (folder, woff, selector, family) in WEIGHTS.items():
        icons = used[wclass]
        if not icons:
            continue
        css = (pkg_src / folder / "style.css").read_text(encoding="utf-8")
        codes: dict[str, int] = {}
        for name in sorted(icons):
            pattern = re.escape(f"{selector}.{name}:before") + r'\s*\{\s*content:\s*"\\([0-9a-f]+)"'
            match = re.search(pattern, css)
            if not match:
                sys.exit(f"no codepoint for {wclass} {name} — renamed upstream?")
            codes[name] = int(match.group(1), 16)

        font = TTFont(pkg_src / folder / woff)
        options = Options()
        options.layout_features = ["*"]
        options.desubroutinize = True
        subsetter = Subsetter(options=options)
        subsetter.populate(unicodes=list(codes.values()))
        subsetter.subset(font)

        buf = io.BytesIO()
        font.flavor = "woff2"
        font.save(buf)
        payload = base64.b64encode(buf.getvalue()).decode()
        print(f"  {wclass:8} {len(icons):2} icons · {len(buf.getvalue()) / 1024:.1f}KB")

        parts.append(
            f'@font-face{{font-family:"{family}";'
            f'src:url(data:font/woff2;base64,{payload}) format("woff2");'
            f"font-weight:normal;font-style:normal;font-display:block}}"
        )
        parts.append(
            f'{selector}{{font-family:"{family}"!important;font-style:normal;'
            f"font-weight:normal;font-variant:normal;text-transform:none;line-height:1;"
            f"letter-spacing:0;-webkit-font-smoothing:antialiased;"
            f"-moz-osx-font-smoothing:grayscale}}"
        )
        parts.extend(f'{selector}.{n}:before{{content:"\\{cp:x}"}}' for n, cp in codes.items())
    return "\n".join(parts)


def main() -> None:
    if not shutil.which("npx"):
        sys.exit("npx not found — install Node to rebuild dashboard.css")
    html = HTML.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        print("tailwind…")
        (work / "tailwind.config.js").write_text(
            TAILWIND_CONFIG % {"html": str(HTML), "sans": json.dumps(SANS_FAMILIES)}, encoding="utf-8"
        )
        (work / "in.css").write_text("@tailwind base;@tailwind components;@tailwind utilities;\n")
        subprocess.run(
            ["npx", "-y", TAILWIND_PKG, "-c", "tailwind.config.js",
             "-i", "in.css", "-o", "tw.css", "--minify"],
            cwd=work, check=True, capture_output=True,
        )
        tailwind = (work / "tw.css").read_text(encoding="utf-8")
        print(f"  {len(tailwind) / 1024:.1f}KB")

        print("phosphor…")
        subprocess.run(["npm", "pack", PHOSPHOR_PKG], cwd=work, check=True, capture_output=True)
        tgz = next(work.glob("phosphor-icons-web-*.tgz"))
        subprocess.run(["tar", "xzf", tgz.name], cwd=work, check=True)
        icons = build_icons(work / "package" / "src", icon_usage(html))

    OUT.write_text(
        "/* Generated by tools/build_dashboard_assets.py — do not edit by hand. */\n"
        f"{tailwind}\n{icons}\n",
        encoding="utf-8",
    )
    print(f"\n{OUT.relative_to(ROOT)} · {OUT.stat().st_size / 1024:.1f}KB")


if __name__ == "__main__":
    main()
