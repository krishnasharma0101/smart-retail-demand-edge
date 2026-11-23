"""Edge inference service: HTTP REST + WebSocket + MQTT in one process.

    uvicorn retail_edge.service:app --host 0.0.0.0 --port 8000

Data path:  POS / shelf sensors --MQTT--> [queue] --> state --> RandomForest
            --> replenishment policy --> MQTT (forecast, alert, purchase order)
                                     --> WebSocket (dashboard)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import platform
import resource
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import topics as T
from .config import settings
from .features import HORIZON, features_from_history, synth_history
from .inventory import recommend
from .model import DemandModel
from .schemas import BatchRequest, PredictRequest, ReorderRequest, SettingsRequest
from .state import StoreState

log = logging.getLogger("edge")
SIM_START = os.getenv("SIM_START", "2015-11-22")


class Hub:
    """WebSocket fan-out with per-client bounded queues (slow clients drop, never block)."""

    def __init__(self):
        self.clients: set[asyncio.Queue] = set()

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.clients.add(q)
        return q

    def unregister(self, q):
        self.clients.discard(q)

    def send(self, msg: dict):
        data = json.dumps(msg, separators=(",", ":"), default=str)
        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)


class Edge:
    def __init__(self):
        self.model = DemandModel()
        self.state = StoreState(self.model, SIM_START)
        self.hub = Hub()
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=20_000)
        self.feed: deque = deque(maxlen=250)
        self.feed_pending: list = []
        self.mqtt_connected = False
        self.sim_status: dict | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"edge-{settings.store_id}")
        self.client.will_set(T.STATUS, json.dumps({"state": "offline"}), qos=1, retain=True)
        self.client.reconnect_delay_set(1, 10)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---------------- MQTT (paho thread) -> asyncio queue ----------------
    def _on_connect(self, c, userdata, flags, rc, props=None):
        self.mqtt_connected = not rc.is_failure if hasattr(rc, "is_failure") else rc == 0
        for t in T.SUBSCRIPTIONS:
            c.subscribe(t, qos=1)
        c.publish(T.STATUS, json.dumps({"state": "online", "model": self.model.version,
                                        "ts": time.time()}), qos=1, retain=True)
        log.info("MQTT connected to %s:%s", settings.mqtt_host, settings.mqtt_port)

    def _on_disconnect(self, c, userdata, flags, rc, props=None):
        self.mqtt_connected = False
        log.warning("MQTT disconnected rc=%s (auto-reconnect)", rc)

    def _on_message(self, c, userdata, msg):
        if self.loop is None:
            return
        item = (msg.topic, bytes(msg.payload), time.time())
        try:
            self.loop.call_soon_threadsafe(self.inbox.put_nowait, item)
        except (asyncio.QueueFull, RuntimeError):
            self.state.counters["dropped_inbox"] += 1

    def publish(self, topic: str, payload: dict, qos: int = 0, retain: bool = False):
        body = json.dumps(payload, separators=(",", ":"), default=str)
        self.client.publish(topic, body, qos=qos, retain=retain)
        self.state.counters["mqtt_out"] += 1
        self._feed("out", topic, payload)

    def _feed(self, direction: str, topic: str, payload: Any):
        e = {"t": round(time.time(), 3), "dir": direction, "topic": topic, "payload": payload}
        self.feed.append(e)
        self.feed_pending.append(e)

    # ---------------- consumer task: the only writer of state ----------------
    async def consume(self):
        st = self.state
        while True:
            topic, raw, ts = await self.inbox.get()
            st.counters["mqtt_in"] += 1
            st.msg_times.append(ts)
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("payload must be a JSON object")
                kind = topic.split("/")[2]
                self._feed("in", topic, body)
                if kind == "pos":
                    qty = int(body["qty"])
                    if not 0 <= qty <= 1_000_000:
                        raise ValueError("qty out of range")
                    st.on_pos(body["sku"], qty, body.get("price"))
                elif kind == "shelf":
                    st.on_shelf(body["sku"], max(0.0, float(body["on_hand"])))
                elif kind == "delivery":
                    st.on_delivery(body["sku"], int(body["qty"]), body.get("po", ""))
                elif kind == "sim":
                    self.sim_status = body
                elif kind == "clock" and body.get("event") == "day_close":
                    dt.date.fromisoformat(body["sim_date"])
                    await self.on_day_close(body["sim_date"])
                elif kind == "clock":
                    self.hub.send({"type": "clock", "data": body})
            except Exception as e:  # malformed input never kills the edge loop
                st.counters["rejected"] += 1
                log.warning("rejected message on %s: %s", topic, e)

    async def on_day_close(self, sim_date: str):
        st = self.state
        st.close_day(sim_date)
        new_alerts, ms = st.run_forecasts(sim_date, publish=self.publish)
        for p in st.products.values():
            self.client.publish(T.FORECAST.format(sku=p.sku), json.dumps(p.forecast), qos=0, retain=True)
            if p.rec:
                self.client.publish(T.ALERT.format(sku=p.sku), json.dumps({"sku": p.sku, **p.rec}), qos=0, retain=True)
        st.counters["mqtt_out"] += 2 * len(st.products)
        self._feed("out", T.FORECAST.format(sku="*"), {"batch": len(st.products), "latency_ms": round(ms, 2),
                                                        "origin": sim_date})
        for a in new_alerts:
            self._feed("out", T.ALERT.format(sku=a["sku"]), a)
            if st.auto_reorder:
                p = st.products[a["sku"]]
                if not p.open_pos and a["order_qty"] > 0:
                    self.place_order(a["sku"], a["order_qty"], auto=True)
        self.hub.send({"type": "tick", "kpis": st.kpis(), "rows": st.rows(), "alerts": new_alerts,
                       "latency_ms": round(ms, 2)})

    def place_order(self, sku: str, qty: int, auto: bool = False) -> dict:
        po = self.state.make_po(sku, qty)
        po["auto"] = auto
        self.publish(T.REORDER.format(sku=sku), po, qos=1)
        return po

    async def flush_feed(self):
        while True:
            await asyncio.sleep(0.4)
            if self.feed_pending:
                batch, self.feed_pending = self.feed_pending[-60:], []
                self.hub.send({"type": "feed", "items": batch, "rate": self.state.kpis()["msg_rate"]})

    def start_mqtt(self):
        try:
            self.client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=20)
            self.client.loop_start()
        except Exception as e:
            log.error("MQTT start failed (%s); HTTP API keeps working", e)

    def stop(self):
        try:
            self.client.publish(T.STATUS, json.dumps({"state": "offline"}), qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


edge: Edge | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global edge
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    edge = Edge()
    edge.loop = asyncio.get_running_loop()
    edge.start_mqtt()
    tasks = [asyncio.create_task(edge.consume()), asyncio.create_task(edge.flush_feed())]
    yield
    for t in tasks:
        t.cancel()
    edge.stop()


app = FastAPI(title="Smart Retail Edge — Demand Prediction", version=__version__, lifespan=lifespan,
              description="Edge inference service: Random Forest demand forecasts, replenishment alerts, MQTT bridge.")


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errs = [{"field": ".".join(str(x) for x in e.get("loc", [])[1:]), "msg": e.get("msg")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"error": "validation_error", "details": errs})


@app.exception_handler(ValueError)
async def value_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"error": "invalid_input", "details": str(exc)})


def E() -> Edge:
    if edge is None:
        raise HTTPException(503, "service starting")
    return edge


# ------------------------------------------------------------------ REST
@app.get("/health")
def health():
    e = E()
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return {"status": "ok", "model_version": e.model.version, "mqtt_connected": e.mqtt_connected,
            "broker": f"{settings.mqtt_host}:{settings.mqtt_port}", "uptime_s": round(time.time() - e.state.started),
            "sim_date": e.state.sim_date, "rss_mb": round(ru.ru_maxrss / 1024, 1),
            "platform": f"{platform.system()} {platform.machine()} · Python {platform.python_version()}"}


@app.get("/data-info")
def data_info():
    from .data import CLEANING_REPORT
    return json.loads(CLEANING_REPORT.read_text()) if CLEANING_REPORT.exists() else {}


@app.get("/model-info")
def model_info(sample: bool = False):
    return E().model.info(include_sample=sample)


def _predict_one(req: PredictRequest) -> dict:
    e = E()
    st = e.state
    lead = req.lead_time_days if req.lead_time_days is not None else st.lead_time
    if req.sku:
        p = st.products.get(req.sku)
        if p is None:
            raise HTTPException(404, f"unknown sku {req.sku}")
        hist = req.history or [u for _, u in p.history]
        price = req.unit_price or p.unit_price
        origin = req.date or dt.date.fromisoformat(st.sim_date)
        row = features_from_history(hist, origin, price, p.category, p.department,
                                    ref_price=req.ref_price or (sum(p.prices) / len(p.prices) if p.prices else price))
        stock = req.current_stock if req.current_stock is not None else p.on_hand
        on_order = req.on_order or p.on_order
        sigma = float(e.model.sigma.get(p.sku, 0))
        source = "live-edge-history"
    else:
        hist = req.history or synth_history(req.last_day, req.last_7_days, req.last_28_days)
        origin = req.date or dt.date.fromisoformat(st.sim_date)
        dept = req.department or f"{req.category}_1"
        row = features_from_history(hist, origin, req.unit_price, req.category, dept, ref_price=req.ref_price)
        stock, on_order, sigma = req.current_stock, req.on_order, 0.0
        source = "manual-history" if req.history else "manual-summary"
    (pred,), ms = e.model.predict_rows([row])
    if sigma == 0.0:
        # unknown product: use forest spread as the error proxy (p90-p10 ~ 2.56 sigma for a normal)
        sigma = max((pred["p90"] - pred["p10"]) / 2.56 / (HORIZON ** 0.5), 0.0)
    out = {"sku": req.sku, "origin": origin.isoformat(), "horizon_days": HORIZON, **pred,
           "daily_rate": round(pred["pred_7d"] / HORIZON, 2), "latency_ms": round(ms, 3),
           "model_version": e.model.version, "source": source,
           "features": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()}}
    if stock is not None:
        out["recommendation"] = recommend(pred["pred_7d"], stock, on_order, sigma, lead, st.z,
                                          settings.case_pack).dict()
    return out


@app.post("/predict")
def predict(req: PredictRequest):
    out = _predict_one(req)
    E().hub.send({"type": "manual_prediction", "data": {k: v for k, v in out.items() if k != "features"}})
    return out


@app.post("/batch")
def batch(req: BatchRequest):
    t0 = time.perf_counter()
    results = []
    for i, item in enumerate(req.items):
        try:
            results.append({"ok": True, **_predict_one(item)})
        except HTTPException as ex:
            results.append({"ok": False, "index": i, "error": ex.detail})
        except ValueError as ex:
            results.append({"ok": False, "index": i, "error": str(ex)})
    return {"count": len(results), "total_ms": round((time.perf_counter() - t0) * 1e3, 2), "results": results}


@app.get("/stock")
def stock(severity: str | None = None):
    rows = E().state.rows()
    if severity:
        want = {s.strip().upper() for s in severity.split(",")}
        rows = [r for r in rows if (r["rec"] or {}).get("severity") in want]
    return {"sim_date": E().state.sim_date, "count": len(rows), "items": rows}


@app.get("/stock/{sku}")
def stock_one(sku: str):
    p = E().state.products.get(sku)
    if p is None:
        raise HTTPException(404, f"unknown sku {sku}")
    row = E().state.product_row(p)
    row["history"] = list(p.history)
    row["trail"] = list(p.trail)
    row["open_pos"] = p.open_pos
    return row


@app.post("/stock/{sku}/reorder")
def reorder(sku: str, req: ReorderRequest | None = None):
    e = E()
    p = e.state.products.get(sku)
    if p is None:
        raise HTTPException(404, f"unknown sku {sku}")
    qty = (req.qty if req and req.qty else None) or (p.rec or {}).get("order_qty") or settings.case_pack
    if not e.mqtt_connected:
        raise HTTPException(503, "MQTT broker unreachable; order not sent")
    return e.place_order(sku, int(qty))


@app.get("/alerts")
def alerts(limit: int = 50):
    return list(E().state.alerts)[: max(1, min(limit, 300))]


@app.get("/metrics/edge")
def edge_metrics():
    e = E()
    return {"latency": e.model.latency.summary(), "kpis": e.state.kpis(), "sim": e.sim_status,
            "mqtt_connected": e.mqtt_connected}


@app.post("/settings")
def update_settings(req: SettingsRequest):
    st = E().state
    if req.auto_reorder is not None:
        st.auto_reorder = req.auto_reorder
    if req.lead_time_days is not None:
        st.lead_time = req.lead_time_days
    if req.service_level_z is not None:
        st.z = req.service_level_z
    return {"auto_reorder": st.auto_reorder, "lead_time_days": st.lead_time, "service_level_z": st.z}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    e = E()
    q = e.hub.register()
    try:
        await sock.send_text(json.dumps({"type": "snapshot", "kpis": e.state.kpis(), "rows": e.state.rows(),
                                         "alerts": list(e.state.alerts)[:50], "feed": list(e.feed)[-80:],
                                         "health": health(), "sim": e.sim_status}, default=str))
        while True:
            await sock.send_text(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        e.hub.unregister(q)


app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(settings.web_dir / "index.html")


def main():
    import uvicorn
    uvicorn.run("retail_edge.service:app", host=settings.http_host, port=settings.http_port, log_level="info")


if __name__ == "__main__":
    main()
