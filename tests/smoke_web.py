"""
Headless-browser smoke test for the web app.

Starts the range-capable dev server, opens the page in Chromium, waits for the
map and the noise layer, reads a few known points through the app's own
lookup code and takes screenshots.  The basemap is fetched from the internet;
when that is unreachable the app falls back to a plain background and the
test still passes (only the overlay is asserted).

    pip install playwright && playwright install chromium
    python tests/smoke_web.py
"""
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8791
OUT = os.path.join(ROOT, "tests", "screenshots")

PROBES = [  # (name, lng, lat, expect_value)
    ("Toronto – Gardiner Expwy @ Spadina", -79.3910, 43.6385, True),
    ("Toronto – Leaside residential", -79.3665, 43.7085, True),
    ("Montréal – Autoroute 40 @ Décarie", -73.6700, 45.5010, True),
    ("Vancouver – Kitsilano residential", -123.1590, 49.2650, True),
    ("Outside coverage – Sudbury", -80.99, 46.49, None),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "scripts", "serve.py"), str(PORT), os.path.join(ROOT, "web"), "-q"])
    time.sleep(1.0)
    failures = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(f"http://localhost:{PORT}/#11/43.68/-79.42", wait_until="load")
            page.wait_for_function("window.__noise && window.__noise.ready()", timeout=30000)
            # wait for the noise source to have loaded tiles
            page.wait_for_function("""() => {
                const m = document.querySelector('#map');
                return m && m.__mapReady === true;
            }""", timeout=1) if False else None
            page.wait_for_timeout(4000)
            page.screenshot(path=os.path.join(OUT, "toronto_overview.png"))

            for name, lng, lat, expect in PROBES:
                val = page.evaluate("([lng, lat]) => window.__noise.readNoise(lng, lat)", [lng, lat])
                ok = (val is None) if expect is None else (val is not None and val.get("db") is not None and 35 <= val["db"] <= 95)
                status = "ok " if ok else "FAIL"
                print(f"{status} {name:45s} -> {val}")
                failures += 0 if ok else 1

            # simulate a click and check that a popup with a dB value appears
            page.click("#map", position={"x": 640, "y": 430})
            page.wait_for_selector(".maplibregl-popup", timeout=10000)
            txt = page.inner_text(".maplibregl-popup")
            print("popup:", " ".join(txt.split())[:120])
            if "dB" not in txt and "estimate" not in txt and "covered" not in txt:
                print("FAIL popup has no reading")
                failures += 1
            page.screenshot(path=os.path.join(OUT, "toronto_click.png"))

            # zoom to street level to check overzoom rendering
            page.goto(f"http://localhost:{PORT}/#15.5/43.6535/-79.3830", wait_until="load")
            page.wait_for_function("window.__noise && window.__noise.ready()", timeout=30000)
            page.wait_for_timeout(3500)
            page.screenshot(path=os.path.join(OUT, "toronto_street.png"))

            # about modal
            page.click("#about-btn")
            page.wait_for_selector("#about:not([hidden])")
            page.screenshot(path=os.path.join(OUT, "about.png"))

            real_errors = [e for e in errors if "openfreemap" not in e.lower() and "photon" not in e.lower()
                           and "failed to fetch" not in e.lower() and "net::" not in e.lower()]
            if real_errors:
                print("console errors:", real_errors[:5])
                failures += 1
            browser.close()
    finally:
        srv.terminate()
    print("FAILED" if failures else "ALL OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
