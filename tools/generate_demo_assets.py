"""Render static, synthetic Job Harness previews for the public README."""

from __future__ import annotations

import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SOURCE = (ASSETS / "demo.html").as_uri()


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 935}, device_scale_factor=1)
        for frame, name in (("", "tui-vacancies.png"), ("frame-2", "demo-02.png"), ("frame-3", "demo-03.png")):
            page.goto(f"{SOURCE}?frame={frame}")
            page.evaluate("frame => document.body.className = frame", frame)
            page.screenshot(path=str(ASSETS / name))
        browser.close()
    (ASSETS / "demo-frames.txt").write_text("file 'tui-vacancies.png'\nduration 1.5\nfile 'demo-02.png'\nduration 1.5\nfile 'demo-03.png'\nduration 2\nfile 'demo-03.png'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "demo-frames.txt", "-vf", "fps=10,scale=1200:-1:flags=lanczos,palettegen", "palette.png"],
        cwd=ASSETS,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "demo-frames.txt", "-i", "palette.png", "-lavfi", "fps=10,scale=1200:-1:flags=lanczos[x];[x][1:v]paletteuse", "demo.gif"],
        cwd=ASSETS,
        check=True,
        capture_output=True,
    )
    (ASSETS / "demo-frames.txt").unlink()
    (ASSETS / "palette.png").unlink()
    (ASSETS / "demo-02.png").unlink()
    (ASSETS / "demo-03.png").unlink()


if __name__ == "__main__":
    main()
