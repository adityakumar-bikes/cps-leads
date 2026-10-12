#!/usr/bin/env python3
"""Prints a JSON fixture for test_fact_engine.js: random leads run through the pipeline's own FactTable encoder
(so the JS decoder/engine is tested against what the pipeline really writes), plus the raw rows to count against."""
import json, os, random, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_data as R
from datetime import datetime, timedelta

random.seed(11)
states = [f"State{i}" for i in range(14)]
cities = [f"City{i}" for i in range(90)]
city_state = {c: states[(i * 7) % len(states)] for i, c in enumerate(cities)}
dealers = []
for i in range(700):
    c = random.choice(cities)
    dealers.append((f"{2000 + i} · {c}, {city_state[c]}", c, city_state[c]))
dealers.append(("Unknown", None, None))
brands = {"Alpha": ["MC", "UB"], "Beta": ["Beta"], "Gamma": ["Gamma"], "Delta": ["PB", "TRM"]}
prods = [(b, bus[j % len(bus)], f"{b} Model{j}") for b, bus in brands.items() for j in range(9)]
months = ["Aug'2026", "Sep'2026", "Oct'2026", "Jul'2026"]
meds = ["Organic", "Google", "Facebook", "Non-MS", "Whatsapp"]
lts = ["1105", "70", "103", "69", "80", "75", "1004"]
rows, F = [], R.FactTable()
for _ in range(60000):
    d, c, s = random.choice(dealers)
    if d == "Unknown":
        c = random.choice(cities)
        s = city_state[c]
        if random.random() < 0.1:
            s = random.choice(states)          # state/city mismatch, like the real data
    p = random.choice(prods)
    m = random.choice(months)
    dt = None if random.random() < 0.03 else datetime(2026, 7, 1) + timedelta(days=random.randint(0, 100))
    row = (s, c, d, p, m, random.choice(meds), random.choice(lts), dt)
    F.add(*row)
    rows.append([s, c, d, list(p), m, row[5], row[6], dt.strftime("%Y-%m-%d") if dt else ""])
ims = {"Alpha": {s: ("Y" if i % 2 == 0 else "N") for i, s in enumerate(states)},
       "Gamma": {s: ("Y" if i % 3 == 0 else "N") for i, s in enumerate(states)}}
oem = {"Alpha": {"MC": {}, "UB": {}}, "Delta": {"PB": {}}}
for b, bus in oem.items():
    for bu, m in bus.items():
        for i, c in enumerate(random.sample(cities, 35)):
            m[c.lower() if i % 3 == 0 else c] = {"p": f"{bu} Top {25 if i < 15 else 100}", "s": 0.01}   # some keys differ in case
fact = F.encode()
fact["dealer_names"] = [f"Dealer {i}" for i in range(len(fact["dealers"]))]
fact["dealer_codes"] = [f"code{i}" for i in range(len(fact["dealers"]))]
json.dump({"fact": fact, "rows": rows, "ims": ims, "oem": oem}, sys.stdout)
