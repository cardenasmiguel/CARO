#!/usr/bin/env python3
"""Convierte las capturas SVG de Rich/Textual a PNG nítidos (2x) con Chrome/Chromium headless. Uso:

    python scripts/svg2png.py docs/*.svg            # escribe docs/<nombre>.png junto a cada SVG

Necesita chromium, chromium-browser o google-chrome en el PATH (o CHROME=/ruta). Escala con PNG_SCALE (por defecto 2).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def chrome() -> str:
    for c in (os.environ.get("CHROME"), "chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if c and shutil.which(c):
            return shutil.which(c)
    sys.exit("No encuentro Chrome/Chromium (define CHROME=/ruta)")


def convert(svg: Path, scale: float) -> Path:
    txt = svg.read_text(encoding="utf-8")
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', txt)
    w, h = (float(m.group(1)), float(m.group(2))) if m else (1458.0, 928.0)
    html = ("<!doctype html><html><head><style>html,body{margin:0;padding:0;background:transparent}"
            f"img{{display:block;width:{w}px;height:{h}px}}</style></head><body><img src='{svg.resolve().as_uri()}'></body></html>")
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "page.html"
        page.write_text(html, encoding="utf-8")
        out = svg.with_suffix(".png")
        subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--allow-file-access-from-files",
                        f"--force-device-scale-factor={scale}", "--default-background-color=00000000",
                        f"--window-size={int(round(w))},{int(round(h))}", f"--screenshot={out}", page.as_uri()],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    return out


if __name__ == "__main__":
    scale = float(os.environ.get("PNG_SCALE", "2"))
    for a in sys.argv[1:]:
        print("escrito", convert(Path(a), scale))
