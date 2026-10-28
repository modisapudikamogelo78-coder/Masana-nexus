"""Masana digital twin server. Run: uvicorn server:app --port 8000
Any device (ESP32, Raspberry Pi, Jetson, inverter gateway) can POST telemetry to /api/telemetry."""
import threading, time
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from twin_core import Twin

app = FastAPI(title="Masana Nexus Digital Twin")
twin = Twin()
lock = threading.Lock()
injections = deque()
ctl = dict(control=False)          # human-approved control switch (False = shadow mode)


@app.post("/api/telemetry")
async def telemetry(req: Request):
    body = await req.json()
    with lock:
        return twin.ingest(body)


@app.get("/api/state")
def state():
    with lock:
        snap = twin.snapshot()
        snap["control"] = ctl["control"]
        return snap


@app.get("/api/site_poll")           # polled by the site agent: pending events + bounded command
def site_poll():
    with lock:
        ev = []
        while injections: ev.append(injections.popleft())
        cmd = twin.command() if ctl["control"] else {}
        return dict(injections=ev, command=cmd, control=ctl["control"])


@app.post("/api/inject")
async def inject(req: Request):      # demo-only: emulator events (a real site ignores these)
    b = await req.json()
    with lock: injections.append(b["event"])
    return dict(ok=True)


@app.post("/api/control")
async def control(req: Request):
    b = await req.json()
    with lock: ctl["control"] = bool(b["on"])
    return dict(control=ctl["control"])


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/replay")
def replay():
    return FileResponse("static/replay.html")
