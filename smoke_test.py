"""Smoke test: exercises the solver + API end to end via TestClient.

The A/B concentrate numbers are constructed so that the exact target mix
for 10 L is A = 1500 mL + B = 125 mL + 8375 mL water (16.25% concentrate):

    N: 1500 mL * 1200 mg/L / 1000 = 1800 mg -> 180 mg/L
    P:  125 mL * 4000 mg/L / 1000 =  500 mg ->  50 mg/L
    K: 1500 * 400/1000 + 125 * 2000/1000
                                 =  850 mg ->  85 mg/L
"""
import json
import os
import tempfile

tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def base_request() -> dict:
    return {
        "target_volume_ml": 10000,
        "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
        "concentrates": [
            {"name": "A液", "available_volume": 2000,
             "concentrations": {"N": 1200, "K": 400}},
            {"name": "B液", "available_volume": 2000,
             "concentrations": {"P": 4000, "K": 2000}},
        ],
        "max_concentrate_fraction": 0.20,
        "min_water_ml": 7000,
        "tolerance": 0.05,
    }


with TestClient(app) as client:
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    # 1) feasible trial --------------------------------------------------- #
    req = base_request()
    r = client.post("/api/solve", json=req)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    assert data["water_volume_ml"] >= 7000
    assert data["concentrate_total_ml"] <= 2000 + 1e-6
    assert abs(
        sum(a["volume_ml"] for a in data["allocations"])
        + data["water_volume_ml"] - 10000
    ) < 1e-4
    for el in data["elements"]:
        assert el["status"] == "ok", el
    alloc = {a["concentrate"]: a["volume_ml"] for a in data["allocations"]}
    assert abs(alloc["A液"] - 1500) < 1e-4, alloc
    assert abs(alloc["B液"] - 125) < 1e-4, alloc
    assert abs(data["water_volume_ml"] - 8375) < 1e-4
    print("1) feasible allocations:", alloc,
          "water:", data["water_volume_ml"])

    # 2) infeasible: A bottle nearly empty -> N shortage ------------------ #
    bad = base_request()
    bad["concentrates"][0]["available_volume"] = 50
    r = client.post("/api/solve", json=bad)
    data = r.json()
    assert data["feasible"] is False, data
    nv = [v for v in data["violations"] if v["element"] == "N"]
    assert nv and nv[0]["code"] == "ELEMENT_SHORTAGE", data["violations"]
    # 50 mL * 1.2 mg/mL = 60 mg max vs target 1800 mg
    assert abs(nv[0]["actual"] - 60.0) < 1e-6, nv
    print("2) shortage:", nv[0]["message"])

    # 3) hard volume conflict: 5% concentrate cap implies at most 9500 mL
    # water, but the user demands >= 9600 mL water ----------------------- #
    bad2 = base_request()
    bad2["min_water_ml"] = 9600
    bad2["max_concentrate_fraction"] = 0.05
    data = client.post("/api/solve", json=bad2).json()
    assert data["feasible"] is False
    assert data["violations"][0]["code"] == "CONCENTRATE_FRACTION_CONFLICT"
    print("3) conflict:", data["violations"][0]["message"])

    # 4) water min exceeds total ------------------------------------------ #
    bad3 = base_request()
    bad3["min_water_ml"] = 12000
    data = client.post("/api/solve", json=bad3).json()
    assert data["violations"][0]["code"] == "WATER_MIN_EXCEEDS_VOLUME"
    print("4) water > total:", data["violations"][0]["message"])

    # 5) per-bottle max_volume binds -> N shortage ------------------------ #
    bad4 = base_request()
    bad4["concentrates"][0]["max_volume"] = 5
    data = client.post("/api/solve", json=bad4).json()
    assert data["feasible"] is False
    assert any(v["code"] == "ELEMENT_SHORTAGE" and v["element"] == "N"
               for v in data["violations"]), data["violations"]
    print("5) max_volume cap binds")

    # 6) misspelled inner key is rejected, not silently ignored ----------- #
    bad5 = base_request()
    bad5["concentrates"][1]["concentrates"] = bad5["concentrates"][1].pop(
        "concentrations")
    r = client.post("/api/solve", json=bad5)
    assert r.status_code == 422, r.text
    print("6) typo rejected with 422")

    # 7) unknown element rejected ----------------------------------------- #
    bad6 = base_request()
    bad6["targets"]["concentrations"]["Xx"] = 1
    assert client.post("/api/solve", json=bad6).status_code == 422

    # 7b) element keys are case-insensitive (ca/CA/fe all accepted) ------- #
    ci = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"ca": 50, "FE": 2.5}},
        "concentrates": [
            {"name": "C", "available_volume": 100,
             "concentrations": {"CA": 25000}},
            {"name": "E", "available_volume": 100,
             "concentrations": {"fe": 1250}},
        ],
        "max_concentrate_fraction": 0.5, "min_water_ml": 0, "tolerance": 0.05}
    r = client.post("/api/solve", json=ci)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    got = {e["element"]: e["status"] for e in data["elements"]}
    assert got == {"Ca": "ok", "Fe": "ok"}, got

    # 8) duplicate concentrate names rejected ----------------------------- #
    bad7 = base_request()
    bad7["concentrates"][1]["name"] = "A液"
    assert client.post("/api/solve", json=bad7).status_code == 422

    # 9) save feasible recipe --------------------------------------------- #
    r = client.post("/api/recipes", json={
        "name": "叶菜通用配方", "description": "test", "note": "v1",
        "request": base_request()})
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == 1 and v1["result"]["feasible"]

    # 10) second save -> version 2 ---------------------------------------- #
    r = client.post("/api/recipes", json={
        "name": "叶菜通用配方", "note": "v2", "request": base_request()})
    assert r.status_code == 201 and r.json()["version"] == 2

    # 11) infeasible save rejected, then forced --------------------------- #
    r = client.post("/api/recipes", json={
        "name": "bad", "request": bad4})
    assert r.status_code == 422
    r = client.post("/api/recipes?force=true", json={
        "name": "bad", "request": bad4})
    assert r.status_code == 201 and r.json()["result"]["feasible"] is False

    # 12) list / get / versions / export ---------------------------------- #
    names = [x["name"] for x in client.get("/api/recipes").json()]
    assert "叶菜通用配方" in names and len(names) == 2
    rid = v1["recipe_id"]
    versions = client.get(f"/api/recipes/{rid}/versions").json()
    assert versions[0]["version"] == 2
    assert client.get(f"/api/recipes/{rid}/versions/1").json()["version"] == 1
    r = client.get(f"/api/recipes/{rid}/versions/1/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    payload = r.json()
    assert payload["recipe"]["name"] == "叶菜通用配方"
    assert payload["version"]["result"]["feasible"] is True

    assert client.get("/api/recipes/999").status_code == 404
    assert client.get(f"/api/recipes/{rid}/versions/99").status_code == 404

    # 13) element surplus: N and P coupled 1:1, P target far too low ------ #
    surp_req = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 200, "P": 1}},
        "concentrates": [
            {"name": "BNP", "available_volume": 100,
             "concentrations": {"N": 100000, "P": 100000}},
        ],
        "max_concentrate_fraction": 0.5,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    data = client.post("/api/solve", json=surp_req).json()
    assert data["feasible"] is False, data
    assert any(v["code"] == "ELEMENT_SURPLUS" and v["element"] == "P"
               for v in data["violations"]), data["violations"]
    print("13) surplus:",
          [v["message"] for v in data["violations"]
           if v["code"] == "ELEMENT_SURPLUS"][0])

    # 14) micronutrients: coupled Fe/N ratio exactly matches target ------- #
    # 1 L, N=100 mg/L (100 mg), Fe=2 mg/L (2 mg). Use 2 mL of a mother
    # liquor with N=50000 mg/L and Fe=1000 mg/L -> N 100 mg, Fe 2 mg.
    micro = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 100, "Fe": 2}},
        "concentrates": [
            {"name": "M", "available_volume": 100,
             "concentrations": {"N": 50000, "Fe": 1000, "B": 500}},
        ],
        "max_concentrate_fraction": 0.5,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    r = client.post("/api/solve", json=micro)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    fe = [e for e in data["elements"] if e["element"] == "Fe"][0]
    assert fe["status"] == "ok"
    assert abs(data["allocations"][0]["volume_ml"] - 2.0) < 1e-4
    print("14) micro mix ok:", [(e["element"], e["status"])
                                for e in data["elements"]])

    # 15) element present in a concentrate but not targeted is reported as
    #     "unconstrained" (actual concentration still shown) -------------- #
    unc = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 100, "Fe": 2}},
        "concentrates": [
            {"name": "M", "available_volume": 100,
             # B comes along for the ride but has no target
             "concentrations": {"N": 50000, "Fe": 1000, "B": 500}},
        ],
        "max_concentrate_fraction": 0.5, "min_water_ml": 0, "tolerance": 0.05}
    data = client.post("/api/solve", json=unc).json()
    assert data["feasible"] is True, data
    # B is untargeted: no row for it, because targets drive element rows;
    # it only matters if the user lists it. Confirm targeted rows are ok
    # and B does not block feasibility.
    assert {e["element"] for e in data["elements"]} == {"N", "Fe"}
    # zero target element -> unconstrained row
    unc["targets"] = {"concentrations": {"N": 100, "Fe": 2, "B": 0}}
    data = client.post("/api/solve", json=unc).json()
    assert data["feasible"] is True, data
    b_row = [e for e in data["elements"] if e["element"] == "B"][0]
    assert b_row["status"] == "unconstrained"
    assert b_row["relative_deviation"] is None
    print("15) untargeted/zero-target handling ok")

    # =================================================================== #
    # Raw-water mineral baseline (source_water)
    # =================================================================== #
    # 10 L; target Ca=100, Mg=40 mg/L; raw water already carries
    # Ca=40, Mg=30 mg/L; concentrate C has Ca=640, Mg=130 mg/L.
    # Exact solution: C 1000 mL + water 9000 mL:
    #   Ca: 1000*640/1000 + 9000*40/1000 = 640 + 360 = 1000 mg
    #   Mg: 1000*130/1000 + 9000*30/1000 = 130 + 270 =  400 mg
    def raw_request() -> dict:
        return {
            "target_volume_ml": 10000,
            "targets": {"concentrations": {"Ca": 100, "Mg": 40}},
            "concentrates": [
                {"name": "C液", "available_volume": 2000,
                 "concentrations": {"Ca": 640, "Mg": 130}}
            ],
            "source_water": {"concentrations": {"ca": 40, "mg": 30}},
            "max_concentrate_fraction": 0.20,
            "min_water_ml": 0,
            "tolerance": 0.05,
        }

    # 28) raw-water baseline: contributions split per element ------------- #
    r = client.post("/api/solve", json=raw_request())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    alloc = {a["concentrate"]: a["volume_ml"] for a in data["allocations"]}
    assert abs(alloc["C液"] - 1000) < 1e-4, alloc
    assert abs(data["water_volume_ml"] - 9000) < 1e-4
    rows = {e["element"]: e for e in data["elements"]}
    assert abs(rows["Ca"]["concentrate_amount_mg"] - 640) < 1e-3
    assert abs(rows["Ca"]["source_water_amount_mg"] - 360) < 1e-3
    assert abs(rows["Ca"]["actual_amount_mg"] - 1000) < 1e-3
    assert abs(rows["Mg"]["concentrate_amount_mg"] - 130) < 1e-3
    assert abs(rows["Mg"]["source_water_amount_mg"] - 270) < 1e-3
    assert abs(rows["Mg"]["actual_amount_mg"] - 400) < 1e-3
    assert all(e["status"] == "ok" for e in data["elements"])
    print("28) raw-water baseline split:",
          {e: (rows[e]["concentrate_amount_mg"],
               rows[e]["source_water_amount_mg"]) for e in rows})

    # 29) the baseline scales with the SOLVED water volume, not a fixed
    #     s*V pre-deduction: exact case yields 360 mg (40 mg/L * 9000 mL),
    #     not 400 mg (40 mg/L * 10000 mL) ------------------------------- #
    data = client.post("/api/solve", json=raw_request()).json()
    w0 = data["water_volume_ml"]
    ca0 = [e for e in data["elements"] if e["element"] == "Ca"][0]
    assert abs(ca0["source_water_amount_mg"] - 0.04 * w0) < 1e-6
    assert abs(ca0["source_water_amount_mg"] - 360) < 1e-3
    # binding bottle cap forces a different compromise water share; the
    # raw-water contribution must follow that solved water (x=800 -> w=9200)
    capped = raw_request()
    capped["concentrates"][0]["available_volume"] = 800
    data = client.post("/api/solve", json=capped).json()
    assert data["feasible"] is False, data  # Ca shortfall beyond tolerance
    w = data["water_volume_ml"]
    assert abs(data["concentrate_total_ml"] - 800) < 1e-4
    assert abs(w - 9200) < 1e-4, w
    for el in data["elements"]:
        assert abs(
            el["source_water_amount_mg"]
            - {  # source mg = source mg/L * solved water L
                "Ca": 0.04, "Mg": 0.03}[el["element"]] * w
        ) < 1e-6
        assert abs(
            el["actual_amount_mg"]
            - el["concentrate_amount_mg"] - el["source_water_amount_mg"]
        ) < 1e-6
    print("29) baseline tracks solved water (360 mg @9000 mL,",
          "368 mg @9200 mL), never a fixed s*V deduction")

    # 30) raw-water-forced surplus: target tiny but baseline unavoidable --- #
    surplus = raw_request()
    surplus["targets"] = {"concentrations": {"Ca": 10}}
    data = client.post("/api/solve", json=surplus).json()
    assert data["feasible"] is False, data
    v = [x for x in data["violations"]
         if x["code"] == "ELEMENT_SURPLUS" and x["element"] == "Ca"]
    assert v, data["violations"]
    # even with zero concentrate, ~100% water * 40 mg/L = ~400 mg baseline
    assert abs(v[0]["actual"] - 400) < 1.0, v[0]
    print("30) raw-water-forced surplus:", v[0]["message"])

    # 31) shortage bound includes the baseline contribution --------------- #
    short = raw_request()
    short["concentrates"][0]["available_volume"] = 10
    short["concentrates"][0]["concentrations"] = {"Ca": 1200, "Mg": 130}
    data = client.post("/api/solve", json=short).json()
    assert data["feasible"] is False
    v = [x for x in data["violations"]
         if x["code"] == "ELEMENT_SHORTAGE" and x["element"] == "Ca"]
    assert v, data["violations"]
    # max = 10 mL*1.2 mg/mL + 9990 mL water*0.04 mg/mL = 12 + 399.6
    assert abs(v[0]["actual"] - 411.6) < 1e-3, v[0]
    print("31) shortage reachable bound includes baseline:", v[0]["actual"])

    # 32) source_water validation: unknown element / negative / typo -> 422 #
    bad = raw_request()
    bad["source_water"]["concentrations"]["Xx"] = 1
    assert client.post("/api/solve", json=bad).status_code == 422
    bad = raw_request()
    bad["source_water"]["concentrations"]["Ca"] = -1
    assert client.post("/api/solve", json=bad).status_code == 422
    bad = raw_request()
    bad["source_water"] = {"bogus": {"Ca": 1}}
    assert client.post("/api/solve", json=bad).status_code == 422
    print("32) source_water validation 422 ok")

    # 33) saving preserves source_water; export echoes it back ------------- #
    r = client.post("/api/recipes", json={
        "name": "原水基线配方", "note": "v1", "request": raw_request()})
    assert r.status_code == 201, r.text
    sw_rid = r.json()["recipe_id"]
    saved = client.get(
        f"/api/recipes/{sw_rid}/versions/1").json()
    assert saved["request"]["source_water"] == {
        "concentrations": {"Ca": 40.0, "Mg": 30.0}}, saved["request"]
    export = client.get(
        f"/api/recipes/{sw_rid}/versions/1/export").json()
    assert export["version"]["request"]["source_water"]["concentrations"] == {
        "Ca": 40.0, "Mg": 30.0}
    # result rows keep the split too
    erows = {e["element"]: e
             for e in export["version"]["result"]["elements"]}
    assert abs(erows["Ca"]["source_water_amount_mg"] - 360) < 1e-3
    print("33) save + export preserve source_water")

    # 34) old requests without source_water behave exactly as before ------ #
    legacy = client.post("/api/solve", json=base_request()).json()
    assert legacy["feasible"] is True
    assert all(e["source_water_amount_mg"] == 0.0
               and e["concentrate_amount_mg"] == e["actual_amount_mg"]
               for e in legacy["elements"])
    print("34) legacy requests: zero baseline, unchanged results")

    # =================================================================== #
    # Irrigation plan (POST /api/irrigation/plan)
    # =================================================================== #
    def irr_body(**overrides) -> dict:
        body = {
            "timezone": "Asia/Shanghai",
            "start_date": "2026-05-01",
            "end_date": "2026-05-03",
            "sessions": [
                {"start_time": "08:00", "duration_minutes": 10,
                 "water_ml": 500},
                {"start_time": "08:10:30", "duration_minutes": 5,
                 "water_ml": 200},  # gap 30s -> merges with 08:00
                {"start_time": "23:55", "duration_minutes": 10,
                 "water_ml": 300},  # crosses midnight
            ],
            "daily_capacity_ml": 1100,
        }
        body.update(overrides)
        return body

    # 16) basic plan: merge close batches, cross midnight, UTC+local times #
    r = client.post("/api/irrigation/plan", json=irr_body())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["timezone"] == "Asia/Shanghai" and d["days"] == 3
    # 3 sessions * 3 days = 9 raw; 08:00+08:10:30 merge per day -> 6
    assert d["batch_count_before_merge"] == 9
    assert d["batch_count_after_merge"] == 6
    assert d["merge_gap_seconds"] == 60
    assert d["capacity_ok"] and d["planned_water_ml"] == 3000
    day1 = d["day_results"][0]
    assert day1["batch_count_before_merge"] == 3
    assert day1["batch_count_after_merge"] == 2
    assert day1["planned_water_ml"] == 1000 and day1["within_capacity"]
    b0 = d["batches"][0]
    assert b0["merged"] and b0["water_ml"] == 700
    assert b0["batch_count_before_merge"] == 2
    assert b0["duration_seconds"] == 930  # 08:00:00 .. 08:15:30
    assert b0["start_utc"] == "2026-05-01T00:00:00Z"
    assert b0["local_start"] == "2026-05-01T08:00:00+0800"
    # every day's 23:55 session crosses midnight (last one ends on 05-04)
    xmid = [b for b in d["batches"] if b["crosses_midnight"]]
    assert len(xmid) == 3
    assert xmid[0]["local_end"] == "2026-05-02T00:05:00+0800"
    assert xmid[0]["end_utc"] == "2026-05-01T16:05:00Z"
    print("16) irrigation merge + cross-midnight:",
          d["batch_count_before_merge"], "->",
          d["batch_count_after_merge"], "batches")

    # 17) merge boundary: gap <= 60s merges, 61s does not ---------------- #
    near = {
        "timezone": "UTC", "start_date": "2024-01-01", "end_date": "2024-01-01",
        "daily_capacity_ml": 1000,
        "sessions": [
            {"start_time": "08:00", "duration_minutes": 10, "water_ml": 10},
            {"start_time": "08:11", "duration_minutes": 10, "water_ml": 10},
        ],
    }
    d = client.post("/api/irrigation/plan", json=near).json()
    assert d["batch_count_after_merge"] == 1 and d["batches"][0]["water_ml"] == 20
    near["sessions"][1]["start_time"] = "08:11:01"
    d = client.post("/api/irrigation/plan", json=near).json()
    assert d["batch_count_after_merge"] == 2
    print("17) merge boundary 60s/61s ok")

    # 18) merge across midnight between two days -------------------------- #
    cross = {
        "timezone": "UTC", "start_date": "2024-01-01", "end_date": "2024-01-02",
        "daily_capacity_ml": 10000,
        "sessions": [
            {"start_time": "23:59", "duration_minutes": 2, "water_ml": 100},
            {"start_time": "00:01:30", "duration_minutes": 1, "water_ml": 50},
        ],
    }
    d = client.post("/api/irrigation/plan", json=cross).json()
    assert d["batch_count_before_merge"] == 4
    assert d["batch_count_after_merge"] == 3
    merged_xmid = [
        b for b in d["batches"] if b["merged"] and b["crosses_midnight"]
    ]
    assert len(merged_xmid) == 1
    b = merged_xmid[0]
    assert b["start_utc"] == "2024-01-01T23:59:00Z"
    assert b["end_utc"] == "2024-01-02T00:02:30Z"
    assert b["water_ml"] == 150
    assert {s["day"] for s in b["sources"]} == {"2024-01-01", "2024-01-02"}
    days = {x["date"]: x for x in d["day_results"]}
    assert days["2024-01-01"]["batch_count_after_merge"] == 2
    assert days["2024-01-02"]["batch_count_after_merge"] == 2
    print("18) cross-midnight merge ok")

    # 19) DST gap (spring forward) and duplicate (fall back) ------------- #
    spring = {
        "timezone": "America/New_York",
        "start_date": "2024-03-09", "end_date": "2024-03-11",
        "sessions": [{"start_time": "02:30", "duration_minutes": 30,
                      "water_ml": 100}],
        "daily_capacity_ml": 1000,
    }
    d = client.post("/api/irrigation/plan", json=spring).json()
    events = {x["date"]: x["dst_events"] for x in d["day_results"]}
    assert events["2024-03-09"] == [] and events["2024-03-10"] == ["gap"]
    gb = [b for b in d["batches"] if b["dst_event"] == "gap"][0]
    # 02:30 EST does not exist; fold=0 maps to 07:30Z == 03:30 EDT
    assert gb["start_utc"] == "2024-03-10T07:30:00Z"
    assert gb["end_utc"] == "2024-03-10T08:00:00Z"
    assert gb["local_start"] == "2024-03-10T03:30:00-0400"
    assert gb["sources"][0]["requested_time"] == "02:30:00"
    print("19a) DST gap shifted:", gb["dst_note"])

    fall = {
        "timezone": "America/New_York",
        "start_date": "2024-11-03", "end_date": "2024-11-03",
        "sessions": [{"start_time": "01:30", "duration_minutes": 20,
                      "water_ml": 100}],
        "daily_capacity_ml": 1000,
    }
    b = client.post("/api/irrigation/plan", json=fall).json()["batches"][0]
    assert b["dst_event"] == "duplicate"
    assert b["start_utc"] == "2024-11-03T05:30:00Z"  # first occurrence, EDT
    assert b["local_start"] == "2024-11-03T01:30:00-0400"
    print("19b) DST duplicate first occurrence:", b["dst_note"])

    # elapsed duration is physical time even when a session spans a DST jump
    span = dict(spring, start_date="2024-03-10", end_date="2024-03-10")
    span["sessions"] = [{"start_time": "01:30", "duration_minutes": 120,
                         "water_ml": 1}]
    b = client.post("/api/irrigation/plan", json=span).json()["batches"][0]
    assert b["duration_seconds"] == 7200  # 2 real hours ...
    assert b["local_end"] == "2024-03-10T04:30:00-0400"  # ... clock shows 3h
    print("19c) DST-spanning physical duration ok")

    # 20) daily capacity shortfall: explicit gap, never truncated --------- #
    d = client.post(
        "/api/irrigation/plan", json=irr_body(daily_capacity_ml=900)
    ).json()
    assert d["capacity_ok"] is False
    assert len(d["capacity_violations"]) == 3
    v = d["capacity_violations"][0]
    assert v["planned_water_ml"] == 1000
    assert v["daily_capacity_ml"] == 900
    assert v["shortfall_ml"] == 100
    assert "缺口 100" in v["message"] and "未截断" in v["message"]
    assert all(x["within_capacity"] is False for x in d["day_results"])
    # batches still carry the full planned water (3 days x 1000 = 3000)
    assert sum(b["water_ml"] for b in d["batches"]) == 3000
    print("20) capacity shortfall reported without truncation")
    # zero capacity is a legal (if useless) input, not a 422
    r = client.post(
        "/api/irrigation/plan", json=irr_body(daily_capacity_ml=0)
    )
    assert r.status_code == 200 and r.json()["capacity_ok"] is False

    # 21) invalid input -> 422 ------------------------------------------- #
    def irr_422(overrides, label):
        r = client.post("/api/irrigation/plan", json=irr_body(**overrides))
        assert r.status_code == 422, (label, r.status_code, r.text[:300])

    irr_422({"timezone": "Mars/Olympus"}, "unknown timezone")
    irr_422({"timezone": "+08:00"}, "fixed-offset string")
    irr_422({"timezone": "   "}, "blank timezone")
    irr_422({"end_date": "2026-04-30"}, "end before start")
    irr_422({"sessions": [
        {"start_time": "08:00", "duration_minutes": 10, "water_ml": 1},
        {"start_time": "08:00", "duration_minutes": 1, "water_ml": 1}]},
        "duplicate start")
    irr_422({"sessions": []}, "empty sessions")
    irr_422({"sessions": [{"start_time": "25:00", "duration_minutes": 1,
                           "water_ml": 1}]}, "invalid time")
    irr_422({"sessions": [{"start_time": "08:00", "duration_minutes": 0,
                           "water_ml": 1}]}, "zero duration")
    irr_422({"sessions": [{"start_time": "08:00", "duration_minutes": 1,
                           "water_ml": -5}]}, "negative water")
    irr_422({"daily_capacity_ml": -1}, "negative capacity")
    irr_422({"start_date": "nope"}, "bad date")
    irr_422({"start_date": "2024-01-01", "end_date": "2025-01-05"},
            "range over 370 days")
    bad = irr_body()
    bad["bogus"] = 1
    r = client.post("/api/irrigation/plan", json=bad)
    assert r.status_code == 422
    print("21) all invalid irrigation inputs rejected with 422")

    # 21b) local suspended days ------------------------------------------ #
    sd = client.post(
        "/api/irrigation/plan",
        json=irr_body(suspended_dates=["2026-05-02"]),
    ).json()
    # day 2 schedules nothing: 9 - 3 raw, 6 - 2 merged batches, 2000 mL
    assert sd["suspended_dates"] == ["2026-05-02"]
    assert sd["batch_count_before_merge"] == 6
    assert sd["batch_count_after_merge"] == 4
    assert sd["planned_water_ml"] == 2000
    days_sd = {x["date"]: x for x in sd["day_results"]}
    sday = days_sd["2026-05-02"]
    assert sday["suspended"] is True
    assert sday["batch_count_before_merge"] == 0
    assert sday["planned_water_ml"] == 0 and sday["within_capacity"]
    assert days_sd["2026-05-01"]["suspended"] is False
    assert days_sd["2026-05-03"]["suspended"] is False
    # cross-midnight batch started on day 1 (23:55 -> 05-02 00:05) survives
    survive = [b for b in sd["batches"]
               if b["start_utc"] == "2026-05-01T15:55:00Z"]
    assert survive and survive[0]["crosses_midnight"]
    # no batch starts on the suspended day
    assert not any(b["local_start"].startswith("2026-05-02")
                   for b in sd["batches"])
    # unordered input is echoed sorted; capacity-0 never flags suspended day
    sd2 = client.post("/api/irrigation/plan", json=irr_body(
        daily_capacity_ml=0,
        suspended_dates=["2026-05-03", "2026-05-01"])).json()
    assert sd2["suspended_dates"] == ["2026-05-01", "2026-05-03"]
    assert {v["date"] for v in sd2["capacity_violations"]} == {"2026-05-02"}
    # day 2's 23:55 batch crosses INTO suspended 05-03 and is not cancelled
    assert any(b["start_utc"] == "2026-05-02T15:55:00Z" and b["crosses_midnight"]
               for b in sd2["batches"])
    irr_422({"suspended_dates": ["2026-05-02", "2026-05-02"]},
            "duplicate suspended date")
    irr_422({"suspended_dates": ["2026-04-30"]}, "suspended before range")
    irr_422({"suspended_dates": ["2026-05-04"]}, "suspended after range")
    print("21b) suspended local days: no batches, cross-midnight kept ok")

    # =================================================================== #
    # Consumption forecast
    # (POST /api/recipes/{id}/versions/{v}/consumption-forecast)
    # =================================================================== #
    # Saved recipe: 10 L batch uses A 1500 mL + B 125 mL -> 0.15 mL A and
    # 0.0125 mL B per litre of irrigation water.
    r = client.post("/api/recipes", json={
        "name": "预测配方", "note": "v1", "request": base_request()})
    assert r.status_code == 201, r.text
    fc_rid = r.json()["recipe_id"]
    fc_url = f"/api/recipes/{fc_rid}/versions/1/consumption-forecast"

    def fc_body(**overrides) -> dict:
        body = {
            "timezone": "Asia/Shanghai",
            "start_date": "2026-05-01",
            "end_date": "2026-05-03",
            "sessions": [
                {"start_time": "08:00", "duration_minutes": 10,
                 "water_ml": 500},
                {"start_time": "08:10:30", "duration_minutes": 5,
                 "water_ml": 200},  # merges with 08:00 -> 700 mL
                {"start_time": "23:55", "duration_minutes": 10,
                 "water_ml": 300},   # crosses midnight
            ],
            "stock": [
                {"concentrate": "A液", "stock_ml": 100},
                {"concentrate": "B液", "stock_ml": 10},
            ],
        }
        body.update(overrides)
        return body

    # 22) proportional draw-down, totals and shortage -------------------- #
    r = client.post(fc_url, json=fc_body())
    assert r.status_code == 200, r.text
    d = r.json()
    # 3 days x 1000 mL = 3000 mL -> A 450 mL, B 37.5 mL required
    assert d["planned_water_ml"] == 3000
    assert d["batch_count_before_merge"] == 9
    assert d["batch_count_after_merge"] == 6
    assert d["sufficient"] is False
    assert d["first_shortfall_date"] == "2026-05-01"
    liq = {x["concentrate"]: x for x in d["liquids"]}
    assert abs(liq["A液"]["mix_ml_per_l"] - 150.0) < 1e-9
    assert abs(liq["B液"]["mix_ml_per_l"] - 12.5) < 1e-9
    assert abs(liq["A液"]["total_required_ml"] - 450) < 1e-6
    assert abs(liq["B液"]["total_required_ml"] - 37.5) < 1e-6
    # clamped consumption, explicit gap, never negative
    assert liq["A液"]["total_consumed_ml"] == 100
    assert liq["B液"]["total_consumed_ml"] == 10
    assert abs(liq["A液"]["total_shortfall_ml"] - 350) < 1e-6
    assert abs(liq["B液"]["total_shortfall_ml"] - 27.5) < 1e-6
    assert liq["A液"]["remaining_ml"] == 0
    assert liq["B液"]["remaining_ml"] == 0

    # full projection kept after depletion; remaining never negative
    for day in d["day_results"]:
        for l in day["liquids"]:
            assert l["remaining_ml"] >= 0, l
            assert l["consumed_ml"] <= l["required_ml"] + 1e-9
    last = {x["concentrate"]: x for x in d["day_results"][-1]["liquids"]}
    assert abs(last["A液"]["required_ml"] - 150) < 1e-6
    assert last["A液"]["consumed_ml"] == 0

    # 23) merged / cross-midnight batches counted exactly once ----------- #
    days = {x["date"]: x for x in d["day_results"]}
    assert days["2026-05-01"]["batches"] == 2
    assert days["2026-05-01"]["water_ml"] == 1000
    assert days["2026-05-03"]["batches"] == 2
    xb = [b for b in d["batches"] if b["crosses_midnight"]]
    assert len(xb) == 3
    assert xb[0]["attributed_date"] == "2026-05-01"
    assert xb[2]["attributed_date"] == "2026-05-03"
    mb = d["batches"][0]  # merged 500+200=700 mL batch
    assert mb["merged"] and mb["batch_count_before_merge"] == 2
    mla = {x["concentrate"]: x for x in mb["liquids"]}
    assert abs(mla["A液"]["required_ml"] - 105) < 1e-9  # 0.15 * 700
    assert mla["A液"]["consumed_ml"] == 100
    assert mla["A液"]["shortfall_ml"] == 5

    # 24) sufficient stock and exact end-of-horizon exhaustion ----------- #
    ok = client.post(fc_url, json=fc_body(stock=[
        {"concentrate": "A液", "stock_ml": 100000},
        {"concentrate": "B液", "stock_ml": 100000}])).json()
    assert ok["sufficient"] is True and ok["first_shortfall_date"] is None
    assert all(b["sufficient"] for b in ok["batches"])
    exact = client.post(fc_url, json=fc_body(stock=[
        {"concentrate": "A液", "stock_ml": 100000},
        {"concentrate": "B液", "stock_ml": 37.5}])).json()
    assert exact["sufficient"] is True
    assert exact["liquids"][1]["remaining_ml"] == 0
    # shortage first appearing on day 3
    late = client.post(fc_url, json=fc_body(stock=[
        {"concentrate": "A液", "stock_ml": 100000},
        {"concentrate": "B液", "stock_ml": 32.5}])).json()
    assert late["sufficient"] is False
    assert late["first_shortfall_date"] == "2026-05-03"
    assert late["liquids"][1]["first_shortfall_date"] == "2026-05-03"
    assert late["liquids"][0]["first_shortfall_date"] is None

    # 25) forecast 4xx: missing/unknown/negative/duplicate stock, bad plan #
    assert client.post(
        f"/api/recipes/999/versions/1/consumption-forecast",
        json=fc_body()).status_code == 404
    assert client.post(
        f"/api/recipes/{fc_rid}/versions/99/consumption-forecast",
        json=fc_body()).status_code == 404

    def fc_422(overrides, label):
        rr = client.post(fc_url, json=fc_body(**overrides))
        assert rr.status_code == 422, (label, rr.status_code, rr.text[:300])

    fc_422({"stock": [{"concentrate": "A液", "stock_ml": 100},
                      {"concentrate": "C液", "stock_ml": 10}]},
           "unknown concentrate")
    fc_422({"stock": [{"concentrate": "A液", "stock_ml": 100}]},
           "missing concentrate")
    fc_422({"stock": [{"concentrate": "A液", "stock_ml": 100},
                      {"concentrate": "A液", "stock_ml": 10},
                      {"concentrate": "B液", "stock_ml": 10}]},
           "duplicate stock")
    fc_422({"stock": [{"concentrate": "A液", "stock_ml": -1},
                      {"concentrate": "B液", "stock_ml": 10}]},
           "negative stock")
    fc_422({"stock": []}, "empty stock")
    fc_422({"timezone": "Mars/Olympus"}, "bad tz")
    fc_422({"end_date": "2026-04-30"}, "bad range")
    fc_422({"sessions": []}, "empty sessions")
    bad_fc = fc_body()
    bad_fc["bogus"] = 1
    assert client.post(fc_url, json=bad_fc).status_code == 422
    rr = client.post(fc_url, json=fc_body(stock=[
        {"concentrate": "A液", "stock_ml": 1},
        {"concentrate": "C液", "stock_ml": 1}]))
    assert "未知浓缩液" in rr.json()["detail"]["message"]
    print("22-25) consumption forecast: proportional deduction, "
          "single-count batches, clamping, 4xx all ok")

    # 26) infeasible force-saved version: warning but forecast computed -- #
    bad_req = base_request()
    bad_req["concentrates"][0]["available_volume"] = 50
    r = client.post("/api/recipes?force=true",
                    json={"name": "bad-forecast", "request": bad_req})
    assert r.status_code == 201
    bad_id = r.json()["recipe_id"]
    d = client.post(
        f"/api/recipes/{bad_id}/versions/1/consumption-forecast",
        json=fc_body(stock=[
            {"concentrate": "A液", "stock_ml": 50},
            {"concentrate": "B液", "stock_ml": 50}])).json()
    assert d["recipe_feasible"] is False
    assert d["warning"] and "不可行" in d["warning"]
    print("26) infeasible version forecast carries warning")

    # 27) suspended days schedule no batches and consume no stock -------- #
    d = client.post(fc_url, json=fc_body(
        suspended_dates=["2026-05-02"],
        stock=[{"concentrate": "A液", "stock_ml": 100000},
               {"concentrate": "B液", "stock_ml": 100000}])).json()
    assert d["suspended_dates"] == ["2026-05-02"]
    assert d["planned_water_ml"] == 2000  # day 1 + day 3 only
    assert d["batch_count_before_merge"] == 6
    assert d["batch_count_after_merge"] == 4
    liq = {x["concentrate"]: x for x in d["liquids"]}
    assert abs(liq["A液"]["total_required_ml"] - 300) < 1e-6
    assert abs(liq["B液"]["total_required_ml"] - 25) < 1e-6
    fdays = {x["date"]: x for x in d["day_results"]}
    sday = fdays["2026-05-02"]
    assert sday["suspended"] is True
    assert sday["batches"] == 0 and sday["water_ml"] == 0
    assert sday["sufficient"] is True
    # end-of-day stock unchanged across the suspended day
    rem1 = {x["concentrate"]: x["remaining_ml"]
            for x in fdays["2026-05-01"]["liquids"]}
    rem2 = {x["concentrate"]: x["remaining_ml"]
            for x in sday["liquids"]}
    assert rem1 == rem2
    # cross-midnight batch begun day 1 still deducted once on day 1
    attrs = sorted(b["attributed_date"] for b in d["batches"])
    assert attrs == ["2026-05-01", "2026-05-01",
                     "2026-05-03", "2026-05-03"]
    # shortage that would land on day 2 is skipped to the next active day
    late = client.post(fc_url, json=fc_body(
        suspended_dates=["2026-05-02"],
        stock=[{"concentrate": "A液", "stock_ml": 100000},
               {"concentrate": "B液", "stock_ml": 20}])).json()
    assert late["first_shortfall_date"] == "2026-05-03"
    fc_422({"suspended_dates": ["2026-05-02", "2026-05-02"]},
           "duplicate suspended date")
    fc_422({"suspended_dates": ["2026-05-04"]}, "suspended after range")
    print("27) forecast suspended days: no deduction, cross-midnight kept")

    # =================================================================== #
    # Integer pump-count dispense plan
    # (POST /api/recipes/{id}/versions/{v}/dispense-plan)
    # =================================================================== #
    # Saved 10 L recipe: continuous optimum A 1500 mL + B 125 mL + 8375 water
    dp_rid = rid  # 叶菜通用配方 v1 created in step 9
    dp_url = f"/api/recipes/{dp_rid}/versions/1/dispense-plan"

    def dp_body(a_step, b_step, **extra):
        body = {"pumps": [
            {"concentrate": "A液", "pump_step_ml": a_step},
            {"concentrate": "B液", "pump_step_ml": b_step},
        ]}
        body.update(extra)
        return body

    def brute_force_dispense(a_step, b_step,
                             a_cap=2000.0, b_cap=2000.0,
                             V=10000.0, min_water=7000.0, frac=0.20):
        """Enumerate integer pump counts (mirrors dispense.py coefficients)."""
        import itertools
        import math as _math
        targets = {"N": (180.0, {"A液": 1.2, "B液": 0.0}),
                   "P": (50.0, {"A液": 0.0, "B液": 4.0}),
                   "K": (85.0, {"A液": 0.4, "B液": 2.0})}
        kmax = [int(_math.floor(a_cap / a_step + 1e-7)),
                int(_math.floor(b_cap / b_step + 1e-7))]
        smax = min(frac * V, a_step * kmax[0] + b_step * kmax[1])
        best = None
        for ka, kb in itertools.product(range(kmax[0] + 1), range(kmax[1] + 1)):
            S = a_step * ka + b_step * kb
            if S > smax + 1e-9 or V - S < min_water - 1e-9:
                continue
            w = V - S
            worst = 0.0
            rows = {}
            for e, (tc, coef) in targets.items():
                T = tc * V / 1000.0
                act = coef["A液"] * a_step * ka + coef["B液"] * b_step * kb
                rows[e] = act
                worst = max(worst, abs(act - T) / T)
            if best is None or worst < best[0] - 1e-12:
                best = (worst, (ka, kb), S, w, rows)
        return kmax, smax, best

    # 35) aligned pump steps: exact mix 15 A shots + 5 B shots ------------ #
    r = client.post(dp_url, json=dp_body(100, 25))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["feasible"] is True and d["within_tolerance"] is True
    assert d["optimal"] is True
    assert d["max_relative_deviation"] == 0.0
    al = {x["concentrate"]: x for x in d["allocations"]}
    assert al["A液"]["pump_count"] == 15
    assert al["A液"]["volume_ml"] == 1500.0
    assert al["A液"]["max_pumps"] == 20
    assert al["B液"]["pump_count"] == 5
    assert al["B液"]["volume_ml"] == 125.0
    assert d["concentrate_total_ml"] == 1625.0
    assert d["water_volume_ml"] == 8375.0
    assert d["total_volume_ml"] == 10000.0
    assert all(e["status"] == "ok" for e in d["elements"])
    assert abs(sum(x["volume_ml"] for x in d["allocations"])
               + d["water_volume_ml"] - 10000) < 1e-6
    # source water baseline rows exist and are zero here
    for e in d["elements"]:
        assert e["source_water_amount_mg"] == 0.0
        assert e["concentrate_amount_mg"] == e["actual_amount_mg"]
    print("35) aligned dispense: A 15x100 + B 5x25, water 8375, dev 0")

    # 36) unaligned steps: MILP result equals exhaustive brute-force best - #
    for a_step, b_step in [(120, 15), (97, 23), (150, 40), (33, 7)]:
        kmax_b, smax_b, bf = brute_force_dispense(a_step, b_step)
        assert bf is not None
        r = client.post(dp_url, json=dp_body(a_step, b_step))
        assert r.status_code == 200, r.text
        d = r.json()
        al = {x["concentrate"]: x for x in d["allocations"]}
        got_counts = (al["A液"]["pump_count"], al["B液"]["pump_count"])
        # minimax optimum: worst deviation must match the brute-force value
        assert abs(d["max_relative_deviation"] - bf[0]) < 1e-5, (
            a_step, b_step, got_counts, bf, d["max_relative_deviation"])
        assert abs(d["concentrate_total_ml"]
                   - (a_step * got_counts[0] + b_step * got_counts[1])) < 1e-9
        assert abs(d["water_volume_ml"]
                   - 10000 + d["concentrate_total_ml"]) < 1e-6
        # every allocation respects bottle caps / water / fraction
        assert d["concentrate_total_ml"] <= 2000 + 1e-9
        assert d["water_volume_ml"] >= 7000 - 1e-9
        # element rows are internally consistent
        S = d["concentrate_total_ml"]
        for el in d["elements"]:
            assert abs(el["actual_amount_mg"]
                       - el["concentrate_amount_mg"]
                       - el["source_water_amount_mg"]) < 1e-6
            assert abs(el["actual_amount_mg"] * 1000.0 / 10000
                       - el["actual_concentration"]) < 1e-6
        if bf[0] <= 0.05 + 1e-9:
            assert d["within_tolerance"] is True
            assert d["violations"] == []
        else:
            assert d["within_tolerance"] is False
            assert d["violations"], (a_step, b_step)
            # still the best executable plan, feasible with volume hard limits
            assert d["feasible"] is True
        print(f"36) steps ({a_step},{b_step}): MILP counts {got_counts} "
              f"== brute {bf[1]}, max dev {bf[0]:.4%}")

    # 37) step beyond tolerance still returns best executable plan -------- #
    r = client.post(dp_url, json=dp_body(250, 80))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["feasible"] is True
    assert d["within_tolerance"] is False
    assert d["max_relative_deviation"] > 0.05
    assert d["violations"]
    codes = {v["code"] for v in d["violations"]}
    assert codes <= {"ELEMENT_SHORTAGE", "ELEMENT_SURPLUS",
                     "ELEMENT_DEVIATION", "TOLERANCE_UNREACHABLE"}
    # counts and water remain executable
    al = {x["concentrate"]: x for x in d["allocations"]}
    assert al["A液"]["pump_count"] * 250 <= 2000 + 1e-9
    assert al["B液"]["pump_count"] * 80 <= 2000 + 1e-9
    print("37) coarse steps: best executable plan still returned:",
          d["message"][:40])

    # 38) source-water baseline: source contribution tracks actual water -- #
    sw_url = f"/api/recipes/{sw_rid}/versions/1/dispense-plan"
    r = client.post(sw_url, json={"pumps": [
        {"concentrate": "C液", "pump_step_ml": 50}]})
    assert r.status_code == 200, r.text
    d = r.json()
    al = {x["concentrate"]: x for x in d["allocations"]}
    # continuous optimum C = 1000 mL -> 20 shots of 50 mL exactly
    assert al["C液"]["pump_count"] == 20
    assert d["concentrate_total_ml"] == 1000.0
    assert d["water_volume_ml"] == 9000.0
    rows = {e["element"]: e for e in d["elements"]}
    assert abs(rows["Ca"]["concentrate_amount_mg"] - 640) < 1e-3
    assert abs(rows["Ca"]["source_water_amount_mg"] - 360) < 1e-3
    assert abs(rows["Ca"]["actual_amount_mg"] - 1000) < 1e-3
    assert abs(rows["Mg"]["source_water_amount_mg"] - 270) < 1e-3
    assert d["within_tolerance"] is True
    # unaligned 60 mL step -> brute force expected k=17, water 8980
    r = client.post(sw_url, json={"pumps": [
        {"concentrate": "C液", "pump_step_ml": 60}]})
    d = r.json()
    assert d["allocations"][0]["pump_count"] == 17, d
    assert d["water_volume_ml"] == 8980.0
    rows = {e["element"]: e for e in d["elements"]}
    assert abs(rows["Ca"]["concentrate_amount_mg"] - 652.8) < 1e-3
    assert abs(rows["Ca"]["source_water_amount_mg"] - 359.2) < 1e-3
    assert abs(rows["Ca"]["actual_amount_mg"] - 1012.0) < 1e-3
    print("38) dispense raw-water contribution follows actual water")

    # 39) volume hard-constraint infeasible version -> feasible=false ------ #
    conflict_req = base_request()
    conflict_req["min_water_ml"] = 9600
    conflict_req["max_concentrate_fraction"] = 0.05
    cid = client.post("/api/recipes?force=true",
                      json={"name": "体积冲突配方", "request": conflict_req}
                      ).json()["recipe_id"]
    r = client.post(
        f"/api/recipes/{cid}/versions/1/dispense-plan",
        json=dp_body(100, 25))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["feasible"] is False
    assert d["within_tolerance"] is False
    assert any(v["code"] == "CONCENTRATE_FRACTION_CONFLICT"
               for v in d["violations"]), d["violations"]
    # min_water > V variant
    over_req = base_request()
    over_req["min_water_ml"] = 12000
    oid = client.post("/api/recipes?force=true",
                      json={"name": "水超体积配方", "request": over_req}
                      ).json()["recipe_id"]
    d = client.post(
        f"/api/recipes/{oid}/versions/1/dispense-plan",
        json=dp_body(100, 25)).json()
    assert d["feasible"] is False
    assert any(v["code"] == "WATER_MIN_EXCEEDS_VOLUME"
               for v in d["violations"])
    print("39) hard volume infeasibility reported with reason")

    # 40) 404 on missing recipe / version --------------------------------- #
    assert client.post(
        "/api/recipes/999/versions/1/dispense-plan",
        json=dp_body(100, 25)).status_code == 404
    assert client.post(
        f"/api/recipes/{dp_rid}/versions/99/dispense-plan",
        json=dp_body(100, 25)).status_code == 404

    # 41) 422 on illegal / mismatched pump configs ------------------------ #
    def dp_422(body, label):
        rr = client.post(dp_url, json=body)
        assert rr.status_code == 422, (label, rr.status_code, rr.text[:300])

    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": 100},
                      {"concentrate": "C液", "pump_step_ml": 25}]},
           "unknown concentrate")
    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": 100}]},
           "missing B")
    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": 100},
                      {"concentrate": "B液", "pump_step_ml": 25},
                      {"concentrate": "A液", "pump_step_ml": 10}]},
           "duplicate pump")
    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": 0},
                      {"concentrate": "B液", "pump_step_ml": 25}]},
           "zero step")
    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": -5},
                      {"concentrate": "B液", "pump_step_ml": 25}]},
           "negative step")
    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": "NaN"},
                      {"concentrate": "B液", "pump_step_ml": 25}]},
           "NaN string step")
    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": "Infinity"},
                      {"concentrate": "B液", "pump_step_ml": 25}]},
           "Infinity string step")
    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": "x"},
                      {"concentrate": "B液", "pump_step_ml": 25}]},
           "non-numeric step")
    dp_422({"pumps": []}, "empty pumps")
    bad_pumps = dp_body(100, 25)
    bad_pumps["bogus"] = 1
    dp_422(bad_pumps, "extra field")
    bad_pumps = dp_body(100, 25)
    bad_pumps["pumps"][0]["bogus"] = 1
    dp_422(bad_pumps, "extra nested field")
    # step larger than the bottle cap (even one shot is impossible)
    rr = client.post(dp_url, json=dp_body(2001, 25))
    assert rr.status_code == 422 and "上限" in rr.json()["detail"]["message"]
    # unknown-concentrate message is actionable
    rr = client.post(dp_url, json={"pumps": [
        {"concentrate": "A液", "pump_step_ml": 100},
        {"concentrate": "X液", "pump_step_ml": 25}]})
    assert "未知浓缩液" in rr.json()["detail"]["message"]
    print("40-41) dispense 404/422: missing versions, illegal/mismatched "
          "pump configs all rejected")

    # 42) force-saved infeasible (element-wise) version: warning but plan
    #     is still computed against the version's targets/constraints ----- #
    d = client.post(
        f"/api/recipes/{bad_id}/versions/1/dispense-plan",
        json={"pumps": [
            {"concentrate": "A液", "pump_step_ml": 25},
            {"concentrate": "B液", "pump_step_ml": 25}]}).json()
    assert d["recipe_feasible"] is False
    assert d["warning"] and "不可行" in d["warning"]
    assert d["feasible"] is True
    print("42) dispense over infeasible force-saved version carries warning")

print("\nALL SMOKE TESTS PASSED")
