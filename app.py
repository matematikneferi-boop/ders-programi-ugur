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

    # Render'in ucretsiz plani sadece ~0.1 CPU (bir cekirdegin onda biri)
    # veriyor. num_search_workers=8 gibi cok sayida paralel worker,
    # OLMAYAN CPU'ya zorlanir -> gercek aramaya ayrilan zaman artmaz, sadece
    # context-switch yuku biner ve sonuc TEK worker'dan bile kotu cikabilir.
    # Bu yuzden ucretsiz/dusuk-CPU ortamda num_search_workers=1 cok daha
    # iyi sonuc verir. Istemci "maxTimeSeconds" gonderirse onu kullan
    # (gunicorn --timeout 120 oldugu icin 100 sn'ye kadar guvenli).
    max_time = float(data.get("maxTimeSeconds", 90.0))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)

    status_name = solver.StatusName(status)
    is_proven_optimal = (status == cp_model.OPTIMAL)
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

    # --- Yerlesemeyen saatler icin gercek tani: KAPASITE mi, CAKISMA mi? ---
    # Bir ogretmenin haftalik musait (kilitli olmayan) saat sayisi, o
    # ogretmene atanmis TUM derslerin toplam saatinden azsa, bu gercek bir
    # kapasite asimi (musaitligi gevsetmeden cozulemez). Musaitlik
    # yetiyorsa ama yine de yerlesemediyse, sorun bu ogretmenin/sinifin
    # diger derslerle saat orta-catismasi (musaitlik biraz genisletilebilir
    # ya da ders/sinif dagilimi gozden gecirilebilir).
    teacher_capacity = {}
    for t in teachers:
        cap = 0
        for d in days:
            for p in periods:
                if not unavail_map.get((t["id"], d, p), False):
                    cap += 1
        teacher_capacity[t["id"]] = cap

    teacher_total_hours = {}
    for a in valid_assignments:
        teacher_total_hours[a["teacherId"]] = teacher_total_hours.get(a["teacherId"], 0) + a["hours"]

    unplaced = []
    for a in valid_assignments:
        missing = solver.Value(shortfall[a["id"]])
        if missing > 0:
            tid = a["teacherId"]
            cap = teacher_capacity.get(tid, 0)
            total_need = teacher_total_hours.get(tid, 0)
            if total_need > cap:
                diag_code = "KAPASITE_ASIMI"
                diag_text = (
                    f"KAPASITE ASIMI: bu ogretmenin toplam ders yuku {total_need} saat, "
                    f"ama musait oldugu (kilitli/kirmizi olmayan) saat sayisi sadece {cap} saat. "
                    f"Bu, Musaitlik sekmesindeki kirmizi saatleri azaltmadan ya da ders yukunu "
                    f"dusurmeden cozulemez."
                )
            else:
                diag_code = "CAKISMA"
                if is_proven_optimal:
                    kesinlik = (
                        "Cozucu suresi icinde ISPATLANMIS OPTIMUM'a ulasti; yani bu "
                        "kisitlar altinda matematiksel olarak daha iyisi YOK."
                    )
                else:
                    kesinlik = (
                        "ONEMLI: cozucu suresi (max_time_in_seconds) dolmadan durdu, yani "
                        "bu SADECE o ana kadar bulunan en iyi sonuc — daha uzun sure ile "
                        "(ya da tekrar calistirarak) daha az yerlesemeyen saat cikma "
                        "ihtimali var, bu henuz kesin 'imkansiz' anlamina gelmez."
                    )
                diag_text = (
                    f"CAKISMA: bu ogretmenin musait saati ({cap}) toplam yukune ({total_need}) "
                    f"teorik olarak yetiyor, ama diger sinif/derslerle ayni saatlere denk "
                    f"geldigi icin bu {missing} saat yerlesemedi. {kesinlik} Musaitligi biraz "
                    f"genisletmek, bu dersin gunlere dagilimini (max_per_day) gevsetmek ya da "
                    f"bu ogretmenin diger derslerinin saatlerini gozden gecirmek gerekebilir."
                )
            unplaced.append({
                "assignmentId": a["id"], "teacherId": a["teacherId"], "classId": a["classId"],
                "subject": a["subject"], "missingHours": missing,
                "teacherCapacity": cap, "teacherTotalHours": total_need,
                "diagnosisCode": diag_code, "diagnosis": diag_text,
            })

    return jsonify({
        "status": status_name,
        "isProvenOptimal": is_proven_optimal,
        "schedule": schedule,
        "unplaced": unplaced,
        "solveSeconds": round(time.time() - t0, 2),
    })


if __name__ == "__main__":
    # Render, PORT ortam degiskenini kendisi verir; gunicorn ile calistirilir
    # (bkz. Procfile / start command). Yerel test icin:
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
