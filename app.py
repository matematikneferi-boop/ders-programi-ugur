"""
Ders Programi CP-SAT Cozucu - Flask API
=========================================
HTML sayfasindaki state (teachers, classes, subjects, assignments, settings)
JSON olarak POST edilir; Google OR-Tools CP-SAT ile gercek, tam kisitli bir
ders programi hesaplanip JSON olarak donulur.

Beklenen girdi (POST /solve):
{
  "teachers": [
    {"id": "ot0", "name": "...", "branch": "...", "maxHours": 27,
     "unavail": {"days": ["Pazartesi"], "slots": ["Sali-3", "Cuma-7"]}}
  ],
  "classes": [
    {"id": "oc0", "name": "5/A", "guideTeacherId": "ot14"}
  ],
  "subjects": [{"id": "s0", "name": "Matematik"}],
  "assignments": [
    {"id": "a0", "teacherId": "ot0", "classId": "oc0", "subject": "Beden Egitimi", "hours": 2}
  ],
  "settings": {"periods": 7, "saturday": false},
  "maxPerDay": 2   // optional, ayni sinifa ayni dersin bir gunde en fazla kac saat girebilecegi (default 2)
}

Cikti:
{
  "status": "OPTIMAL" | "FEASIBLE" | "INFEASIBLE",
  "schedule": { classId: { day: { period(str): {"teacherId":..,"subject":..,"assignmentId":..} } } },
  "unplaced": [ {"assignmentId":..., "teacherId":..., "classId":..., "subject":..., "missingHours":.. } ],
  "solveSeconds": 0.0
}
"""

import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from ortools.sat.python import cp_model

app = Flask(__name__)
CORS(app)  # HTML sayfa farkli bir origin'den (GitHub Pages / file://) cagiracak

DAY_NAMES_HAFTAICI = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma"]
DAY_NAME_CUMARTESI = "Cumartesi"


def build_days(settings):
    days = list(DAY_NAMES_HAFTAICI)
    if settings.get("saturday"):
        days.append(DAY_NAME_CUMARTESI)
    return days


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/solve", methods=["POST"])
def solve():
    t0 = time.time()
    data = request.get_json(force=True, silent=True) or {}

    teachers = data.get("teachers", [])
    classes = data.get("classes", [])
    assignments = data.get("assignments", [])
    settings = data.get("settings", {"periods": 7, "saturday": False})
    max_per_day = int(data.get("maxPerDay", 2))

    periods_count = int(settings.get("periods", 7))
    days = build_days(settings)
    periods = list(range(1, periods_count + 1))

    teacher_ids = {t["id"] for t in teachers}
    class_ids = {c["id"] for c in classes}

    # --- Onceden filtrele: hours <= 0 ya da bilinmeyen teacher/class olanlari atla ---
    valid_assignments = []
    for a in assignments:
        if a.get("hours", 0) <= 0:
            continue
        if a.get("teacherId") not in teacher_ids or a.get("classId") not in class_ids:
            continue
        valid_assignments.append(a)

    if not valid_assignments:
        return jsonify({
            "status": "OPTIMAL",
            "schedule": {},
            "unplaced": [],
            "solveSeconds": round(time.time() - t0, 2),
        })

    # --- Ogretmen musaitlik haritasi: (teacherId, day, period) -> True/False (musait mi) ---
    unavail_map = {}
    for t in teachers:
        un = t.get("unavail") or {}
        blocked_days = set(un.get("days", []))
        blocked_slots = set(un.get("slots", []))  # "Gun-Saat" formatinda, ornek "Salı-3"
        for d in days:
            for p in periods:
                blocked = (d in blocked_days) or (f"{d}-{p}" in blocked_slots)
                unavail_map[(t["id"], d, p)] = blocked

    model = cp_model.CpModel()

    # x[assignment_id][day][period] = bu ders saati bu (gun,saat)'te mi islenecek?
    x = {}
    for a in valid_assignments:
        aid = a["id"]
        x[aid] = {}
        for d in days:
            for p in periods:
                blocked = unavail_map.get((a["teacherId"], d, p), False)
                if blocked:
                    continue  # o hucre icin degisken bile olusturma -> otomatik 0
                x[aid][(d, p)] = model.NewBoolVar(f"x_{aid}_{d}_{p}")

    # 1) Her assignment tam olarak 'hours' kadar saate yerlessin
    #    (musaitlik yuzunden yer bulunamayan durumlar icin gevsetme degiskeni ekliyoruz)
    shortfall = {}
    for a in valid_assignments:
        aid = a["id"]
        cells = list(x[aid].values())
        shortfall[aid] = model.NewIntVar(0, a["hours"], f"short_{aid}")
        model.Add(sum(cells) + shortfall[aid] == a["hours"])

    # 2) Bir ogretmen ayni (gun,saat)'te en fazla 1 yerde olabilir
    by_teacher_slot = {}
    for a in valid_assignments:
        for (d, p), var in x[a["id"]].items():
            by_teacher_slot.setdefault((a["teacherId"], d, p), []).append(var)
    for key, vars_ in by_teacher_slot.items():
        if len(vars_) > 1:
            model.Add(sum(vars_) <= 1)

    # 3) Bir sinif ayni (gun,saat)'te en fazla 1 ders alabilir
    by_class_slot = {}
    for a in valid_assignments:
        for (d, p), var in x[a["id"]].items():
            by_class_slot.setdefault((a["classId"], d, p), []).append(var)
    for key, vars_ in by_class_slot.items():
        if len(vars_) > 1:
            model.Add(sum(vars_) <= 1)

    # 4) Ayni sinif + ayni ders, bir gunde en fazla 'max_per_day' saat olsun
    by_class_subject_day = {}
    for a in valid_assignments:
        for (d, p), var in x[a["id"]].items():
            key = (a["classId"], a["subject"], d)
            by_class_subject_day.setdefault(key, []).append(var)
    for key, vars_ in by_class_subject_day.items():
        model.Add(sum(vars_) <= max_per_day)

    # --- Amac: (a) yerlesemeyen saatleri (shortfall) minimize et,
    #           (b) ayni dersin ayni gune yigilmasini hafifce cezalandirarak
    #               haftaya yaymayi tesvik et.
    total_shortfall = sum(shortfall.values())

    spread_penalty_terms = []
    for (cls, subj, d), vars_ in by_class_subject_day.items():
        if len(vars_) >= 2:
            over = model.NewIntVar(0, len(vars_), f"over_{cls}_{subj}_{d}")
            model.Add(over >= sum(vars_) - 1)
            spread_penalty_terms.append(over)

    model.Minimize(total_shortfall * 1000 + sum(spread_penalty_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 25.0
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    status_name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return jsonify({
            "status": status_name,
            "schedule": {},
            "unplaced": [
                {"assignmentId": a["id"], "teacherId": a["teacherId"], "classId": a["classId"],
                 "subject": a["subject"], "missingHours": a["hours"]}
                for a in valid_assignments
            ],
            "solveSeconds": round(time.time() - t0, 2),
        }), 200

    # --- Sonucu okunabilir yapiya cevir ---
    schedule = {}
    for a in valid_assignments:
        aid = a["id"]
        for (d, p), var in x[aid].items():
            if solver.Value(var) == 1:
                schedule.setdefault(a["classId"], {}).setdefault(d, {})[str(p)] = {
                    "teacherId": a["teacherId"],
                    "subject": a["subject"],
                    "assignmentId": aid,
                }

    unplaced = []
    for a in valid_assignments:
        missing = solver.Value(shortfall[a["id"]])
        if missing > 0:
            unplaced.append({
                "assignmentId": a["id"], "teacherId": a["teacherId"], "classId": a["classId"],
                "subject": a["subject"], "missingHours": missing,
            })

    return jsonify({
        "status": status_name,
        "schedule": schedule,
        "unplaced": unplaced,
        "solveSeconds": round(time.time() - t0, 2),
    })


if __name__ == "__main__":
    # Render, PORT ortam degiskenini kendisi verir; gunicorn ile calistirilir
    # (bkz. Procfile / start command). Yerel test icin:
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
