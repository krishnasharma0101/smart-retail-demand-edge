"""Record a narrated walkthrough of the live dashboard (Playwright video -> H.264 mp4).

    python scripts/record_demo.py [--url http://localhost:8000] [--out media/demo.mp4]
"""
import argparse
import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

CAPTION_JS = """
(text) => {
  let c = document.getElementById('__cap');
  if (!c) { c = document.createElement('div'); c.id='__cap';
    c.style.cssText='position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#1d2322ee;color:#f2f5f4;'+
      'font:500 15px -apple-system,Segoe UI,system-ui,sans-serif;padding:9px 16px;border-radius:6px;z-index:99;max-width:980px;text-align:center;'+
      'box-shadow:0 4px 18px #0003;transition:opacity .25s';
    document.body.appendChild(c); }
  c.textContent = text; c.style.opacity = text ? 1 : 0;
}
"""


async def main(url: str, out: Path):
    tmp = Path(tempfile.mkdtemp())
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1440, "height": 900},
                                  record_video_dir=str(tmp), record_video_size={"width": 1440, "height": 900})
        pg = await ctx.new_page()
        cap = lambda t: pg.evaluate(CAPTION_JS, t)
        w = pg.wait_for_timeout

        await pg.goto(url + "/#overview")
        await pg.wait_for_selector("#miniTable tbody tr")
        await cap("Smart Retail Edge: a Random Forest demand model running on the store's own edge node, no cloud")
        await w(5000)
        await cap("The store simulator replays real Walmart M5 sales the model never saw, streaming POS and shelf-sensor events over MQTT")
        await w(6000)
        await cap("At every day close the edge forecasts 7-day demand for all 49 products in one batch (about 20 ms) and publishes the results")
        await w(6000)
        await pg.hover("#kWape")
        await cap("Live accuracy: each forecast is scored 7 days later against what actually sold, next to a naive last-week baseline")
        await w(5500)
        await cap("Chart: units sold (bars), the forecast daily rate with its 10–90% tree band, and the next 7 days")
        first = pg.locator("#miniTable tbody tr").nth(1)
        await first.click()
        await w(5000)
        btn = pg.locator("#queue [data-order]").first
        if await btn.count():
            await cap("Replenishment queue: click Order and the edge sends a purchase order to the store over MQTT (QoS 1)")
            await w(1500)
            await btn.click()
            await w(4000)

        await pg.click("nav a[data-view=inventory]")
        await cap("Inventory: on-hand vs forecast, days of cover against the 3-day lead time, reorder point and suggested order")
        await w(5000)
        await pg.click("#sevChips button[data-sev=HIGH]")
        await cap("Filter by severity. CRITICAL means the shelf runs out before a new delivery could arrive")
        await w(3500)
        await pg.click("#sevChips button[data-sev=MEDIUM]")
        await w(2500)
        await pg.click("#sevChips button[data-sev=ALL]")
        await pg.click("#invTable th[data-k=cover]")
        await w(2500)

        await pg.click("nav a[data-view=predict]")
        await cap("Manual prediction over HTTP: pick an existing product and run a what-if with your own stock level")
        await pg.select_option("#pSku", "FOODS_3_090")
        await w(1200)
        await pg.fill("input[name=current_stock]", "140")
        await w(800)
        await pg.click("#predBtn")
        await w(5000)
        await cap("Or describe a new product: category, price and recent sales. You get a forecast, an interval and an order recommendation")
        await pg.click("#modeSeg button[data-mode=manual]")
        await pg.select_option("#pCat", "HOUSEHOLD")
        for n, v in (("last_day", "9"), ("last_7_days", "52"), ("last_28_days", "190"), ("unit_price", "4.97"), ("current_stock", "30")):
            await pg.fill(f"input[name={n}]", "")
            await pg.type(f"input[name={n}]", v, delay=60)
        await pg.click("#predBtn")
        await w(5000)
        await cap("Bad input is rejected with a clear reason, both in the browser and by the API's own validation")
        await pg.fill("input[name=last_day]", "90")
        await pg.click("#predBtn")
        await w(3500)

        await pg.click("nav a[data-view=model]")
        await cap("Hold-out test (last 6 months): Random Forest R² 0.85, 20% lower RMSE than a 28-day moving average")
        await w(6000)
        await cap("Feature importance, predicted vs actual, error by department, model card and the data-cleaning audit")
        await pg.mouse.wheel(0, 500)
        await w(4500)
        await pg.mouse.wheel(0, -500)

        await pg.click("nav a[data-view=edge]")
        await cap("Edge node: inference latency, MQTT throughput, memory footprint and the full topic map")
        await w(6000)
        await cap("Every message in and out of the broker, live: POS, shelf readings, forecasts, alerts and purchase orders")
        await w(5500)

        await pg.click("nav a[data-view=overview]")
        await cap("Stack: FastAPI, paho-MQTT, a Mosquitto or embedded broker, scikit-learn. Runs on a laptop or a Raspberry Pi with run.sh or Docker")
        await w(6500)
        await cap("")
        await w(800)
        await ctx.close()
        await b.close()
    webm = next(tmp.glob("*.webm"))
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-c:v", "libx264", "-preset", "slow", "-crf", "24",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-r", "25", str(out)], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print("wrote", out, out.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "media" / "demo.mp4"))
    a = ap.parse_args()
    asyncio.run(main(a.url, Path(a.out)))
