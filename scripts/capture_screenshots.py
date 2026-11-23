"""Capture dashboard screenshots with headless Chromium (1440x900).

    python scripts/capture_screenshots.py [--url http://localhost:8000] [--out media/screenshots]
Requires the stack to be running (./run.sh) and `playwright install chromium`.
"""
import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def main(url: str, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        errors = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await pg.goto(url + "/#overview")
        await pg.add_style_tag(content="#toast{display:none!important}")
        await pg.wait_for_selector("#miniTable tbody tr")
        await pg.wait_for_timeout(2500)
        await pg.screenshot(path=out / "01-overview.png")

        await pg.click("nav a[data-view=inventory]")
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=out / "02-inventory.png")
        await pg.click("#sevChips button[data-sev=HIGH]")
        await pg.wait_for_timeout(400)
        await pg.screenshot(path=out / "03-inventory-high.png")
        await pg.click("#sevChips button[data-sev=ALL]")

        await pg.click("nav a[data-view=predict]")
        await pg.select_option("#pSku", "FOODS_3_090")
        await pg.fill("input[name=current_stock]", "140")
        await pg.click("#predBtn")
        await pg.wait_for_selector("#resBody:not(.hidden)")
        await pg.wait_for_timeout(500)
        await pg.screenshot(path=out / "04-predict-sku.png")

        await pg.click("#modeSeg button[data-mode=manual]")
        await pg.select_option("#pCat", "HOUSEHOLD")
        await pg.fill("input[name=last_day]", "9")
        await pg.fill("input[name=last_7_days]", "52")
        await pg.fill("input[name=last_28_days]", "190")
        await pg.fill("input[name=unit_price]", "4.97")
        await pg.fill("input[name=current_stock]", "30")
        await pg.click("#predBtn")
        await pg.wait_for_timeout(900)
        await pg.screenshot(path=out / "05-predict-manual.png")

        await pg.fill("input[name=last_day]", "90")
        await pg.click("#predBtn")
        await pg.wait_for_timeout(600)
        await pg.screenshot(path=out / "06-predict-validation.png")

        await pg.click("nav a[data-view=model]")
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path=out / "07-model.png")

        await pg.click("nav a[data-view=edge]")
        await pg.wait_for_timeout(6000)
        await pg.screenshot(path=out / "08-edge-mqtt.png")

        await pg.click("nav a[data-view=overview]")
        await pg.evaluate("document.querySelector('#toast').style.cssText='display:flex!important'")
        btn = pg.locator("#queue [data-order]").first
        if await btn.count():
            await btn.click()
            await pg.wait_for_timeout(700)
            await pg.screenshot(path=out / "10-purchase-order.png")

        await pg.goto(url + "/docs")
        await pg.wait_for_timeout(2500)
        await pg.screenshot(path=out / "09-api-docs.png")
        await b.close()
        if errors:
            print("console errors:", errors[:10])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "media" / "screenshots"))
    a = ap.parse_args()
    asyncio.run(main(a.url, Path(a.out)))
