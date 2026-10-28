# Masana Nexus Digital Twin

A digital twin of one tower site: it ingests live telemetry, keeps its own state in sync,
calibrates itself, forecasts, and runs the Nexus policy against the live state.

## Run (3 terminals, Python 3.9+)
    pip install fastapi uvicorn
    uvicorn server:app --port 8000          # the twin
    python site_emulator.py                 # stand-in for the physical site (replace with real hardware)
    open http://localhost:8000              # live twin dashboard
    open http://localhost:8000/replay       # pitch replay: scenarios A/B/C, baseline vs Nexus side by side

## What makes it a twin (not a simulation)
- Telemetry in over HTTP: POST /api/telemetry (any device can send it)
- State estimation: physics prediction fused with the SoC sensor every sample
- Online calibration: learns real battery capacity and solar derate (emulator hides the truth: 26 kWh, 0.88)
- Data quality: validation, rejected-sample counter, staleness detection, clock re-sync
- What-if engine: rolls the calibrated twin 4 h ahead under legacy vs Nexus policy
- Shadow mode (recommend only) or human-approved control (site applies a bounded command)
- Dead-man's switch: no fresh telemetry => no command => site reverts to its legacy controller

## Connect real hardware
POST JSON every few seconds to /api/telemetry:
    {"site_id":"NODE-104","ts":<epoch seconds>,"soc_pct":71.2,"batt_kw":1.4,   # +discharge / -charge
     "pv_kw":2.1,"load_kw":3.2,"grid":false,"gen_on":false,"temp_c":31.0,
     "load_priority_kw":0.8,"load_voice_kw":1.0,"load_data_kw":1.4,"load_data_kw_full":1.4,
     "jobs":[{"id":"J1","left_h":1.0,"deadline_ts":<epoch>,"running":false}]}
Then poll GET /api/site_poll for {"command":{gen,data_frac,run}} and apply it only if present.
Fields to map: ESP32/Pi + current/voltage sensors, inverter/BMS Modbus, or a Jetson reading a power meter.

## Honest limits
Emulator physics are assumptions. Model is a single site, simple state fusion (not a full EKF),
fuel/battery numbers are placeholders. Savings shown are twin projections, not field results.

## Hard scenarios in the live twin (emulator buttons)
- B (remote, 8 L fuel, evening): generator limited by fuel; Nexus switches to CRITICAL-ONLY (all data shed) when fuel is gone and SoC <= 27%.
- C (outage + frozen SoC sensor, 4 h): the twin spots exact-repeat readings while the battery is working,
  switches to dead-reckoning with an extra safety margin, and re-syncs when the sensor recovers.
  Measured in the emulator over the same 10 h: legacy site 50 min blackout (battery hit 5%); Nexus 0 min blackout, lowest SoC ~28%.
- B in the live twin is closer: no blackout for either controller in 10 h, but Nexus keeps a higher floor (about 29% vs 19% SoC) and 1 L of diesel left vs 0.
  (The /replay page uses a different, harsher parameter set, so its B numbers differ. Do not mix the two when quoting.)
Legacy site in the emulator trusts its own SoC reading, frozen or not. State that assumption openly when presenting scenario C.
