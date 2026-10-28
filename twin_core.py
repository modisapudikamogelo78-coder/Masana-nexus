"""Masana Nexus digital twin core.

The twin ingests live telemetry from a site, keeps its OWN state in sync
(state estimation + online calibration), forecasts ahead, and runs the Nexus
policy against that live state. It never assumes the telemetry source is a
simulator: anything that POSTs the telemetry schema can drive it.
"""
import math, time, statistics
from collections import deque

P = dict(batt_max_kw=6.0, eff=0.95, floor=0.05, min_reserve=0.20, gen_kw=5.0,
         pv_peak=6.0, job_kw=1.5, tz=2, temp_limit=40.0, fuel_l_kwh=0.30)
STEP_H = 5 / 60


def hod(ts):                      # hour of day (SAST) from epoch seconds
    return ((ts / 3600.0) + P["tz"]) % 24


def clear_sky(h):
    return max(0.0, math.sin(math.pi * (h - 6) / 12)) * P["pv_peak"] if 6 <= h <= 18 else 0.0


def ttr_minutes(soc, cap, ratio, h, fixed_kw, data_kw, jobs_kw):
    """Minutes until usable reserve (above min_reserve) is exhausted, 120 = safe horizon."""
    e = max(0.0, soc - P["min_reserve"]) * cap
    for k in range(24):
        net = fixed_kw + data_kw + jobs_kw - clear_sky((h + k * STEP_H) % 24) * ratio
        e -= net * STEP_H
        if e <= 0:
            return k * 5
    return 120


# ------------------------------------------------------------------ policies
def legacy_decide(s):
    """What the site does today: static thresholds, FIFO jobs, no forecasting."""
    gen = s["gen_on"]
    if not s["grid"]:
        if s["soc"] < 0.30: gen = True
        if s["soc"] >= 0.80: gen = False
    else:
        gen = False
    if s.get("fuel", 99.0) <= 0: gen = False
    run = [j["id"] for j in s["jobs"][:1]]
    return dict(gen=gen, data_frac=1.0, run=run, ttr=-1,
                receipt=dict(action="Legacy: static thresholds, FIFO jobs", reason="No forecast", confidence=1.0,
                             expected="-", rollback="-"))


def nexus_decide(s):
    """Hard tiers: MoMo/USSD + voice are never shed. Data is shed in stages; AI jobs defer.
    Critical-only mode (no fuel + low reserve) sheds ALL data to keep priority tiers alive."""
    fixed = s["fixed_kw"]; fuel = s.get("fuel", 99.0); suspect = s.get("suspect", False)
    soc = s["soc"] - (0.05 if suspect else 0.0)          # extra safety margin while SoC is dead-reckoned
    tag = " [SoC sensor frozen: dead-reckoning]" if suspect else ""
    if s["stale"]:
        gen = (soc < 0.35 or (s["gen_on"] and soc < 0.50)) and fuel > 0
        return dict(gen=gen, data_frac=0.6, run=[], ttr=-1, crit=False, receipt=dict(
            action="FALLBACK: telemetry stale, conservative mode",
            reason="Forecast inputs unreliable: assume no solar, shed data to 60%, pause AI jobs",
            confidence=0.3, expected="Protect priority tiers, hold reserve",
            rollback="Resume optimisation after 2 fresh samples"))
    jobs_now = 1 if s["jobs"] else 0
    if s["grid"]:
        run = [] if s["temp"] > P["temp_limit"] else [j["id"] for j in s["jobs"][:1]]
        return dict(gen=False, data_frac=1.0, run=run, ttr=240, crit=False, receipt=dict(
            action="Grid OK: run flexible jobs, charge battery", reason="Grid present", confidence=0.95,
            expected="No intervention", rollback="n/a"))
    ttr = ttr_minutes(soc, s["cap"], s["ratio"], s["h"], fixed, s["data_kw"], jobs_now * P["job_kw"])
    level = 0 if ttr >= 120 else (1 if ttr >= 75 else 2)
    data_frac = [1.0, 0.7, 0.4][level]
    run = []
    hot = s["temp"] > P["temp_limit"]
    for j in sorted(s["jobs"], key=lambda j: j["dl_in_h"])[:1]:       # earliest deadline first
        slack = j["dl_in_h"] - j["left_h"]
        pv_now = clear_sky(s["h"]) * s["ratio"]
        surplus = pv_now - fixed - s["data_kw"] * data_frac
        if surplus >= P["job_kw"] or (level == 0 and soc > 0.70) or (slack < 1.0 and soc > P["min_reserve"] + 0.10):
            run = [j["id"]]
    thermal = hot and bool(run)
    if hot: run = []
    gen = s["gen_on"]
    if fuel > 0 and (soc < P["min_reserve"] + 0.05 or (ttr < 30 and soc < 0.40)): gen = True
    if gen and soc >= 0.50: gen = False
    if fuel <= 0: gen = False
    crit = s.get("crit_on", False)
    if not crit and not gen and fuel <= 0.01 and soc <= 0.27: crit = True
    if crit and soc >= 0.35: crit = False
    action = ["Normal ops, defer jobs unless solar surplus",
              "Shed 30% data, defer AI jobs", "Shed 60% data, protect MoMo/USSD + voice"][level]
    reason = f"Reserve forecast {ttr:.0f} min" + ("" if level == 0 else f" < {120 if level == 1 else 75}")
    if gen: action += " + generator to 50% SoC"
    if fuel <= 0 and level > 0: action += " (no fuel)"
    if thermal: action += " + thermal pause (>40C)"; reason += "; battery temp above limit"
    if crit:
        action = "CRITICAL-ONLY: MoMo, USSD, voice only"; data_frac = 0.0; run = []
        reason = f"No fuel and SoC {soc*100:.0f}%: all data shed to keep priority tiers alive"
    return dict(gen=gen, data_frac=data_frac, run=run, ttr=ttr, crit=crit, receipt=dict(
        action=action + tag, reason=reason, confidence=round(0.85 - 0.1 * level - (0.2 if suspect else 0), 2),
        expected=f"Time-to-threshold {ttr:.0f} min at current policy",
        rollback="Release critical-only at 35% SoC or grid return; restore load when forecast reserve >120 min" if crit
                 else "Restore load when forecast reserve >120 min or grid returns"))


# --------------------------------------------------------------- projection
def project(policy, st, horizon_h=4.0):
    """Roll the calibrated twin forward under a policy. This is the 'what-if' engine."""
    decide = nexus_decide if policy == "nexus" else legacy_decide
    soc, gen_on = st["soc"], st["gen_on"]
    fuel, crit_on = st.get("fuel", 99.0), False
    jobs = [dict(j) for j in st["jobs"]]
    series, gen_h, blackout, data_sum, n = [], 0.0, 0, 0.0, int(horizon_h / STEP_H)
    min_soc = soc; done = 0
    for i in range(n):
        h = (st["h"] + i * STEP_H) % 24
        s = dict(soc=soc, cap=st["cap"], ratio=st["ratio"], h=h, grid=st["grid"], gen_on=gen_on, stale=False,
                 temp=st["temp"], fixed_kw=st["fixed_kw"], data_kw=st["data_kw"], jobs=jobs,
                 fuel=fuel, crit_on=crit_on, suspect=False)
        for j in jobs: j["dl_in_h"] = j["deadline_h"] - i * STEP_H
        a = decide(s)
        crit_on = a.get("crit", False)
        gen_on = a["gen"] and fuel > 0
        runj = [j for j in jobs if j["id"] in a["run"]]
        load = st["fixed_kw"] + st["data_kw"] * a["data_frac"] + P["job_kw"] * len(runj)
        pv = clear_sky(h) * st["ratio"]
        gen = P["gen_kw"] if gen_on else 0.0
        if gen_on: fuel = max(0.0, fuel - gen * STEP_H * P["fuel_l_kwh"])
        sup = pv + gen + (99 if st["grid"] else 0)
        cap = st["cap"]
        if sup >= load:
            sur = max(0.0, pv + gen - load) if st["grid"] else sup - load
            ch = min(sur, P["batt_max_kw"], (1 - soc) * cap / (STEP_H * P["eff"]))
            soc += ch * STEP_H * P["eff"] / cap
        else:
            need = load - sup
            avail = max(0.0, (soc - P["floor"]) * cap * P["eff"] / STEP_H)
            dis = min(need, P["batt_max_kw"], avail)
            soc -= dis * STEP_H / P["eff"] / cap
            if dis + 1e-9 < need:
                blackout += 5; runj = []
        soc = max(P["floor"], min(1.0, soc)); min_soc = min(min_soc, soc)
        if gen_on: gen_h += STEP_H
        data_sum += a["data_frac"] if blackout == 0 or True else 0
        for j in runj:
            j["left_h"] -= STEP_H
        for j in [j for j in jobs if j["left_h"] <= 1e-9]:
            jobs.remove(j); done += 1
        series.append(round(soc * 100, 1))
    missed = sum(1 for j in jobs if j["deadline_h"] < horizon_h)
    return dict(series=series, gen_hours=round(gen_h, 2), min_soc=round(min_soc * 100, 1), blackout_min=blackout,
                data_pct=round(100 * data_sum / n, 1), jobs_done=done, jobs_open=len(jobs), jobs_at_risk=missed)


# --------------------------------------------------------------------- twin
class Twin:
    def __init__(self, site_id="NODE-104", cap_belief=30.0):
        self.site_id = site_id
        self.cap = cap_belief                  # belief, calibrated online
        self.cap0 = cap_belief
        self.ratio = 1.0                       # solar actual / clear-sky (cloud + derate), tracked online
        self.soc = None                        # fused SoC estimate (0-1)
        self.last = None
        self.last_rx = None
        self.rx_gaps = deque(maxlen=30)
        self.resid = deque(maxlen=60)
        self.cal = deque(maxlen=40)            # (battery energy kWh, dSoC) pairs for capacity regression
        self.hist = deque(maxlen=400)
        self.counts = dict(accepted=0, rejected=0)
        self.rejects = deque(maxlen=5)
        self.receipts = deque(maxlen=40)
        self.last_action = None
        self.rec = None
        self.proj = None
        self.state = None
        self.events = deque(maxlen=30)
        self.flat = 0; self.suspect = False; self.crit_on = False

    @staticmethod
    def _batt_energy(batt_kw, dt):
        return batt_kw * dt / P["eff"] if batt_kw > 0 else batt_kw * dt * P["eff"]

    # ---- validation
    def _validate(self, t):
        errs = []
        for k in ("ts", "soc_pct", "pv_kw", "load_kw", "batt_kw", "grid", "temp_c"):
            if k not in t: errs.append(f"missing {k}")
        if errs: return errs
        if not -3 <= t["soc_pct"] <= 103: errs.append("soc out of range")   # tolerate sensor noise at 0/100%
        else: t["soc_pct"] = min(100.0, max(0.0, t["soc_pct"]))
        if not -0.1 <= t["pv_kw"] <= 25: errs.append("pv out of range")
        if not -20 <= t["temp_c"] <= 90: errs.append("temp out of range")
        if self.last and t["ts"] <= self.last["ts"]: errs.append("out-of-order timestamp")
        return errs

    # ---- ingest = sync
    def ingest(self, t, now=None):
        now = now or time.time()
        if self.last and "ts" in t and self.last["ts"] - t["ts"] > 3600:     # device clock/restart: re-sync from scratch
            self.events.appendleft("re-sync: telemetry clock jumped back"); self.last = None; self.soc = None
            self.cal.clear(); self.resid.clear(); self.last_action = None; self.flat = 0; self.suspect = False
        errs = self._validate(t)
        if errs:
            self.counts["rejected"] += 1; self.rejects.append(errs[0])
            return dict(ok=False, errors=errs)
        self.counts["accepted"] += 1
        if self.last_rx: self.rx_gaps.append(now - self.last_rx)
        self.last_rx = now
        rep = t["soc_pct"] / 100
        h = hod(t["ts"])
        if self.soc is None:
            self.soc = rep
        else:
            dt = (t["ts"] - self.last["ts"]) / 3600
            if 0 < dt <= 1.0:
                # stuck-sensor detection: exact repeats of the SoC reading while the battery is clearly working
                if abs(self.last["batt_kw"]) > 0.3 and t["soc_pct"] == self.last["soc_pct"]: self.flat += 1
                else: self.flat = 0
                was, self.suspect = self.suspect, self.flat >= 3
                if self.suspect and not was: self.events.appendleft("SoC sensor looks frozen: switching to dead-reckoning")
                if was and not self.suspect: self.events.appendleft("SoC sensor live again: re-synced")
                e_batt = self._batt_energy(self.last["batt_kw"], dt)            # kWh drawn from / stored in cells
                pred = self.soc - e_batt / self.cap                              # physics prediction
                r = rep - pred
                if self.suspect:
                    self.soc = pred                                              # trust physics, not the frozen sensor
                elif was:
                    self.soc = rep                                               # sensor recovered: hard re-sync
                else:
                    self.resid.append(abs(r) * 100)
                    self.soc = pred + 0.5 * r                                   # fuse model + sensor
                dS = rep - self.last["soc_pct"] / 100                           # capacity calibration:
                if not self.suspect and not was: self.cal.append((e_batt, dS))   # cap = -sum(E)/sum(dSoC) over a window
                if len(self.cal) >= 15 and not self.suspect:
                    num = -sum(e for e, _ in self.cal); den = sum(d for _, d in self.cal)
                    if abs(den) > 0.05 and 5 < num / den < 100:
                        self.cap += 0.1 * (num / den - self.cap)
            else:
                self.soc = rep
        cs = clear_sky(h)
        if cs > 1.0: self.ratio += 0.3 * (t["pv_kw"] / cs - self.ratio)
        self.last = t
        self.hist.append(dict(ts=t["ts"], rep=round(t["soc_pct"], 1), est=round(self.soc * 100, 1),
                              pv=round(t["pv_kw"], 2), load=round(t["load_kw"], 2), grid=int(t["grid"])))
        self._think(h)
        return dict(ok=True)

    def _jobs(self, t):
        out = []
        for j in t.get("jobs", []):
            out.append(dict(id=j["id"], left_h=j["left_h"], deadline_h=(j["deadline_ts"] - t["ts"]) / 3600,
                            dl_in_h=(j["deadline_ts"] - t["ts"]) / 3600, running=j.get("running", False)))
        return out

    def _decision_input(self, stale):
        t = self.last
        return dict(soc=self.soc, cap=self.cap, ratio=self.ratio, h=hod(t["ts"]), grid=bool(t["grid"]),
                    gen_on=bool(t.get("gen_on")), stale=stale, temp=t["temp_c"],
                    fixed_kw=t.get("load_priority_kw", 0.8) + t.get("load_voice_kw", 1.0),
                    data_kw=t.get("load_data_kw_full", 1.4), jobs=self._jobs(t),
                    fuel=t.get("fuel_l", 99.0), suspect=self.suspect, crit_on=self.crit_on)

    def _think(self, h):
        s = self._decision_input(False)
        self.state = s
        self.rec = nexus_decide(s)
        self.crit_on = self.rec.get("crit", False)
        self.proj = dict(legacy=project("legacy", s), nexus=project("nexus", s))
        act = self.rec["receipt"]["action"]
        if act != self.last_action:
            self.last_action = act
            self.receipts.appendleft(dict(ts=self.last["ts"], clock=round(h, 2), mode=self.last.get("mode", "legacy"),
                                          **self.rec["receipt"]))

    # ---- staleness & snapshot
    def stale(self, now=None):
        if self.last_rx is None: return True
        now = now or time.time()
        gap = statistics.median(self.rx_gaps) if self.rx_gaps else 2.0
        return now - self.last_rx > max(2.0, 4 * gap)

    def command(self, now=None):
        """Bounded command for the site agent. Empty (site reverts to legacy) if twin is stale/unsynced."""
        if self.last is None or self.stale(now): return {}
        r = self.rec
        return dict(gen=r["gen"], data_frac=r["data_frac"], run=r["run"], issued_for_ts=self.last["ts"])

    def snapshot(self, now=None):
        now = now or time.time()
        stale = self.stale(now)
        if self.last is None:
            return dict(site_id=self.site_id, synced=False)
        rec = self.rec
        if stale:
            s = self._decision_input(True); rec = nexus_decide(s)
            if rec["receipt"]["action"] != self.last_action:
                self.last_action = rec["receipt"]["action"]
                self.receipts.appendleft(dict(ts=self.last["ts"], clock=round(hod(self.last["ts"]), 2),
                                              mode="fallback", **rec["receipt"]))
        t = self.last
        mae = round(statistics.mean(self.resid), 2) if self.resid else None
        return dict(
            site_id=self.site_id, synced=True, stale=stale, age_s=round(now - self.last_rx, 1),
            clock=round(hod(t["ts"]), 2), telemetry=t,
            fuel_l=t.get("fuel_l"), suspect=self.suspect, events=list(self.events)[:5],
            twin=dict(soc_est=round(self.soc * 100, 1), cap_belief=round(self.cap, 1), cap_initial=self.cap0,
                      solar_ratio=round(self.ratio, 2), fidelity_mae_pct=mae),
            rec=dict(gen=rec["gen"], data_frac=rec["data_frac"], run=rec["run"], ttr=rec["ttr"], **{"receipt": rec["receipt"]}),
            proj=self.proj, receipts=list(self.receipts), hist=list(self.hist)[-200:], quality=dict(self.counts, last_reject=list(self.rejects)),
        )
