"""Stand-in for the PHYSICAL site. Runs hidden 'true' physics that the twin does not know
(battery is smaller than the twin believes, panels are derated, sensors are noisy), posts
telemetry over HTTP exactly as real hardware would, and applies the twin's bounded command
if one is fresh. Replace this file with a gateway reading real meters to go live.

python site_emulator.py --speed 5 --step 0.5
"""
import argparse, json, math, random, time, urllib.request
from twin_core import hod, clear_sky, P

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://127.0.0.1:8000")
p.add_argument("--speed", type=float, default=5.0, help="simulated minutes per step")
p.add_argument("--step", type=float, default=0.5, help="wall seconds per step")
a = p.parse_args()


def call(path, body=None):
    req = urllib.request.Request(a.url + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r: return json.loads(r.read())
    except Exception as e:
        return None


class Site:
    CAP_TRUE, DERATE = 26.0, 0.88          # hidden truth (twin believes 30 kWh and 1.0)
    SCRIPT = [14, 15, 16, 17, 18, 19]      # AI job arrival hours

    def __init__(s):
        now = time.time()
        s.ts = (now // 86400) * 86400 + 4 * 3600      # 06:00 SAST today
        s.soc, s.grid, s.gen = 0.85, True, False
        s.cloud_left = s.heat_left = s.drop_left = 0
        s.jobs, s.jid = [], 0
        s.fuel, s.frz_left, s.frozen, s.last_meas, s.black_total = 60.0, 0, 0.85, 0.85, 0
        s.data_frac, s.mode, s.cmd_age = 1.0, "legacy", 99
        s.rng = random.Random(7)

    def inject(s, ev):
        if ev == "grid_off": s.grid = False
        elif ev == "grid_on": s.grid = True
        elif ev == "cloud": s.cloud_left = 48
        elif ev == "heat": s.heat_left = 48
        elif ev == "dropout": s.drop_left = 24
        elif ev == "jobs": [s.add_job() for _ in range(3)]
        elif ev == "low_fuel":                                                   # scenario B: remote site, 8 L left, evening
            s.set_clock(16.0); s.fuel = 8.0; s.soc = 0.55; s.grid = False; s.gen = False; s.jobs = []; s.black_total = 0
        elif ev == "scenario_c":                                                 # scenario C: outage + SoC sensor sticks 4 sim-hours
            s.set_clock(16.0); s.fuel = 60.0; s.soc = 0.50; s.grid = False; s.gen = False; s.jobs = []; s.black_total = 0
            s.frozen = 0.50; s.frz_left = 48
        elif ev == "freeze": s.frozen = s.last_meas; s.frz_left = 48
        elif ev == "reset": s.soc = 0.85; s.grid = True; s.gen = False; s.jobs = []; s.fuel = 60.0; s.frz_left = 0; s.black_total = 0; s.set_clock(6.0)

    def set_clock(s, hour):
        s.ts = (s.ts // 86400) * 86400 + ((hour - 2) % 24) * 3600

    def add_job(s):
        s.jid += 1
        s.jobs.append(dict(id=f"J{s.jid}", left_h=1.0, deadline_ts=s.ts + (8 + s.rng.random() * 3) * 3600, running=False))

    def step(s, cmd):
        dt = a.speed / 60
        h0 = hod(s.ts); s.ts += a.speed * 60; h = hod(s.ts)
        for hr in s.SCRIPT:
            if h0 < hr <= h: s.add_job()
        cloud = 0.35 if s.cloud_left > 0 else 1.0
        pv = clear_sky(h) * s.DERATE * cloud * (1 + s.rng.gauss(0, 0.02))
        # --- control: twin command if fresh, else legacy local controller (which trusts its SoC reading, frozen or not)
        meas = s.frozen if s.frz_left > 0 else s.soc + s.rng.gauss(0, 0.003)
        if cmd:
            s.mode, s.gen, s.data_frac = "nexus", cmd["gen"], cmd["data_frac"]
            run_ids = set(cmd["run"])
        else:
            s.mode, s.data_frac = "legacy", 1.0
            if not s.grid:
                if meas < 0.30: s.gen = True
                if meas >= 0.80: s.gen = False
            else: s.gen = False
            run_ids = {j["id"] for j in s.jobs[:1]}
        if not s.grid and meas < 0.12: s.gen = True          # local safety override, independent of twin (uses the same SoC reading)
        genon = s.gen and s.fuel > 0
        ambient = 22 + 10 * max(0.0, math.sin(math.pi * (h - 9) / 12)) + (9 if s.heat_left > 0 else 0)
        temp = ambient + 4 * len(run_ids & {j["id"] for j in s.jobs})
        if temp > P["temp_limit"] + 3: run_ids = set()        # site hardware thermal cutoff
        for j in s.jobs: j["running"] = j["id"] in run_ids
        n = lambda v: v * (1 + s.rng.gauss(0, 0.02))
        pr, vo, da = n(0.8), n(1.0), n(1.4) * s.data_frac
        load = pr + vo + da + P["job_kw"] * sum(j["running"] for j in s.jobs)
        gen = P["gen_kw"] if genon else 0.0
        if genon: s.fuel = max(0.0, s.fuel - gen * dt * P["fuel_l_kwh"])
        sup = pv + gen + (99 if s.grid else 0)
        batt = 0.0; black = False
        if sup >= load:
            sur = max(0.0, pv + gen - load) if s.grid else sup - load
            ch = min(sur, P["batt_max_kw"], (1 - s.soc) * s.CAP_TRUE / (dt * P["eff"]))
            s.soc += ch * dt * P["eff"] / s.CAP_TRUE; batt = -ch
        else:
            need = load - sup
            avail = max(0.0, (s.soc - P["floor"]) * s.CAP_TRUE * P["eff"] / dt)
            dis = min(need, P["batt_max_kw"], avail)
            s.soc -= dis * dt / P["eff"] / s.CAP_TRUE; batt = dis
            black = dis + 1e-9 < need
        s.soc = max(P["floor"], min(1.0, s.soc))
        if black: s.black_total += a.speed
        s.frz_left = max(0, s.frz_left - 1); s.last_meas = meas
        if not black:
            for j in s.jobs:
                if j["running"]: j["left_h"] -= dt
            s.jobs = [j for j in s.jobs if j["left_h"] > 1e-9]
        for k in ("cloud_left", "heat_left"): setattr(s, k, max(0, getattr(s, k) - 1))
        return dict(site_id="NODE-104", ts=s.ts, soc_pct=round(100 * meas, 2), batt_kw=round(batt, 3),
                    pv_kw=round(max(0, pv), 3), load_kw=round(load, 3), grid=s.grid, gen_on=genon, fuel_l=round(s.fuel, 1), temp_c=round(temp + s.rng.gauss(0, 0.3), 1),
                    load_priority_kw=round(pr, 3), load_voice_kw=round(vo, 3), load_data_kw=round(da, 3), load_data_kw_full=1.4,
                    data_frac=s.data_frac, blackout=black, mode=s.mode,
                    jobs=[dict(id=j["id"], left_h=round(j["left_h"], 3), deadline_ts=j["deadline_ts"], running=j["running"]) for j in s.jobs],
                    _truth=dict(cap_kwh=s.CAP_TRUE, derate=s.DERATE, soc_true=round(100 * s.soc, 2), blackout_min_total=s.black_total))


site = Site()
print(f"site emulator -> {a.url}  ({a.speed} sim-min per {a.step}s)")
while True:
    poll = call("/api/site_poll") or {}
    for ev in poll.get("injections", []): site.inject(ev); print("event:", ev)
    t = site.step(poll.get("command") or {})
    if site.drop_left > 0: site.drop_left -= 1       # telemetry link down: step happens, nothing is sent
    else: call("/api/telemetry", t)
    time.sleep(a.step)
