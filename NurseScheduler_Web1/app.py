# -*- coding: utf-8 -*-
"""
NurseScheduler — Web Trial Edition
© 2026 Sabah Alharbi — All rights reserved
Maternity & Children Hospital · Hafr Al Batin Health Cluster

Streamlit wrapper around the SAME desktop scheduling engine
(schedule_engine.py / database.py / ui_export.py — unmodified).
Every visitor gets an isolated temporary database that vanishes
when their session ends.
"""

import os
import json
import tempfile
import calendar
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

# ─────────────────────────────────────────────────────────────
#  Page config + RTL styling
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="NurseScheduler — جدولة التمريض",
                   page_icon="🏥", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown("""
<style>
  .main .block-container {direction: rtl; text-align: right; padding-top: 1.2rem;}
  section[data-testid="stSidebar"] {direction: rtl; text-align: right;}
  .stRadio > div {direction: rtl;}
  h1,h2,h3,h4 {text-align: right;}
  div[data-testid="stMarkdownContainer"] ul {direction: rtl; text-align: right;}
  .grid-wrap {overflow-x: auto; border: 1px solid #e2e8f0; border-radius: 8px;
              direction: ltr;}
  table.sched {border-collapse: collapse; font-family: Tahoma, Arial;
               font-size: 11px; white-space: nowrap;}
  table.sched th {background:#1e3a5f; color:#fff; padding:4px 7px;
                  border:1px solid #cbd5e1; position: sticky; top: 0;}
  table.sched th.we {background:#7c2d12;}
  table.sched td {padding:3px 7px; border:1px solid #e2e8f0;
                  text-align:center; font-weight:bold;}
  table.sched td.nm {text-align:right; background:#f8fafc; font-weight:normal;
                     position: sticky; right: 0;}
  .chip {display:inline-block; padding:2px 10px; border-radius:4px;
         font-size:12px; font-weight:bold; margin-left:6px;}
  .foot {color:#64748b; font-size:12px; text-align:center;
         border-top:1px solid #e2e8f0; padding-top:8px; margin-top:18px;}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
#  Per-session isolated database
# ─────────────────────────────────────────────────────────────
if "workdir" not in st.session_state:
    st.session_state.workdir = tempfile.mkdtemp(prefix="nsched_")
    st.session_state.db_path = os.path.join(st.session_state.workdir,
                                            "department.db")

import database
database.get_db_path = lambda: st.session_state.db_path
database.get_app_data_dir = lambda: Path(st.session_state.workdir)
database.backup_database = lambda tag="x": None   # no backups needed per session

import ui_export
ui_export._get_export_dir = lambda: Path(st.session_state.workdir)

from database import DepartmentDB
from schedule_engine import ScheduleEngine, SHIFT_CONFIGS, OFF
from constants import (SHIFT_COLORS, get_leave_display, LEAVE_CODES,
                       SO_CODE, SO_BG, SO_FG, NATIONALITIES,
                       derive_nationality_flags, WEEKEND_DAYS_SET, DAY_ABBR,
                       MONTH_NAMES, rule_max_offs, block_min_length)
from import_wizard import (read_grid, detect_header, build_records,
                           FIELD_LABELS, CERT_OPTIONS)
from ui_export import do_excel_export, _generate_pdf_html, _get_leave_map, _get_so_map

db = DepartmentDB()

MAX_STAFF = 100   # Streamlit Community Cloud ~1GB RAM — measured safe ceiling
LEAVE_TYPES = list(LEAVE_CODES.keys())

# ─────────────────────────────────────────────────────────────
#  Sidebar
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🏥 NurseScheduler")
    st.markdown("**نظام جدولة التمريض الذكي**")
    st.caption("مستشفى الولادة والأطفال — تجمع حفر الباطن الصحي")
    page = st.radio("التنقل", ["📖 عن الأداة", "🖥 جرّبي الأداة"],
                    label_visibility="collapsed")
    st.divider()
    st.caption("نسخة ويب تجريبية — بياناتك معزولة\nوتُحذف تلقائيًا بانتهاء الجلسة")
    st.markdown("<div class='foot'>© 2026 <b>Sabah Alharbi</b><br>"
                "All rights reserved</div>", unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════
#  PAGE 1 — Landing / About
# ═════════════════════════════════════════════════════════════
def landing_page():
    st.title("🏥 NurseScheduler — نظام جدولة التمريض الذكي")
    st.markdown("##### جداول مناوبات شهرية عادلة وآمنة خلال دقائق — بدل ساعات الجدولة اليدوية")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("قواعد مطبقة تلقائيًا", "+23 قاعدة")
    c2.metric("أنظمة دوام", "12 / 8 / 6 ساعات")
    c3.metric("زمن التوليد", "ثوانٍ إلى دقائق")
    c4.metric("انتهاكات القواعد الصلبة", "صفر")

    st.divider()
    st.header("ما الأداة؟ ولمن؟")
    st.markdown("""
أداة مبنية خصيصًا لرئيسات أقسام التمريض لتوليد **جدول المناوبات الشهري** تلقائيًا
باستخدام محرك رياضي (Google OR-Tools CP-SAT) يوازن بين تغطية الورديات، راحة الطاقم،
والعدالة بين الموظفات — مع احترام كل طلب والتزام معتمد.

**الفوائد المباشرة:**
- ⏱ توفير ساعات العمل اليدوي كل شهر — الجدول يتولّد ويُصدَّر بضغطة
- ⚖️ عدالة مقيسة رقميًا في توزيع الليالي والويكندات (مؤشر Gini)
- 🛡 أمان المريضة: لا تسلسلات خطرة (ليل ← صباح)، حدود صارمة للأيام المتتالية
- 📋 مستند رسمي جاهز للاعتماد: ترويسة ثنائية اللغة، التاريخ الهجري، خانات التوقيع
""")

    st.header("المزايا الرئيسية")
    f1, f2 = st.columns(2)
    with f1:
        st.markdown("""
- **ثلاثة أنظمة دوام**: 12 ساعة (D/N)، 8 ساعات (M/E/N)، 6 ساعات (M1/M2/E/N)
- **نظام البلوكات**: كتل عمل متتالية بنفس المناوبة (12h: 2–4 أيام · 8h/6h: 4–6) وراحات 1–2 يوم
- **حصص الأوف** حسب الجنسية (سعودية/غير سعودية) مع خصم تلقائي للطلبات
- **طلبات الموظفات (SO)**: أوف بطلب الموظفة يظهر بلون مميز ويُحترم كقيد ملزم
- **الإجازات** بأنواعها الـ12 بخلايا ذهبية تحمل رمز النوع
""")
    with f2:
        st.markdown("""
- **أدوار خاصة**: رئيسة القسم (نمط ثابت)، مسؤولات الورديات، ساعة الرضاعة 👶
- **إشراف المتدربات**: كل متدربة تعمل مع مدربة في نفس الوردية
- **حضور متحدثة عربية** في كل وردية
- **الأون كول**: توزيع عادل بالتناوب من المتاحات (أوف بلا إجازة)
- **استيراد ذكي من Excel**: أي ملف بأي ترتيب — يتنظف ويُفحص قبل الإدخال
""")

    st.header("القيود المطبقة في كل جدول")
    st.markdown("""
| النوع | القاعدة | الوصف |
|---|---|---|
| 🔴 صلب | راحة بعد الليل | ممنوع ليل ← صباح في اليوم التالي (وعبر حدود الشهر أيضًا) |
| 🔴 صلب | الحد الأقصى المتتالي | 12h: ‏4 أيام/4 ليالٍ · 8h/6h: ‏6 أيام/6 ليالٍ |
| 🔴 صلب | البلوكات | نفس المناوبة داخل البلوك، وتناوب قانوني بين البلوكات (D↔N) |
| 🔴 صلب | الإجازات والطلبات | إجازة = أوف دائمًا · طلبات SO والفترات الإجبارية تُلبّى أولًا |
| 🔴 صلب | حصة الأوف | حد أدنى مضمون لكل موظفة حسب جنسيتها |
| 🟡 مرن | التغطية | الحد الأدنى لكل وردية (أقوى وزن — يُنبَّه عند العجز بدل الفشل) |
| 🟡 مرن | العدالة | توازن الليالي والمساءات والويكندات بين الجميع |
| 🟡 مرن | التغطية النوعية | متحدثة عربية + حاملة شهادة (PALS/NRP...) + مسؤولة وردية في كل وردية |
""")

    st.header("كيف تستخدمينها؟ — 4 خطوات")
    st.markdown("""
1. **⚙️ الإعدادات** — اختاري نظام الدوام والشهر وحصص الأوف والحد الأدنى للتغطية
2. **👩‍⚕️ الموظفات** — أدخليهن يدويًا، أو ارفعي ملف Excel، أو اضغطي «بيانات تجريبية» للتجربة الفورية
3. **📋 القيود** — سجّلي الإجازات وطلبات الأوف (SO)
4. **⚡ التوليد** — اضغطي زرًا واحدًا، راجعي الجدول الملون، ونزّلي Excel الرسمي

👈 جاهزة؟ اختاري **«🖥 جرّبي الأداة»** من القائمة الجانبية
""")
    st.info("💡 هذه نسخة تجريبية للتعريف بالأداة. النسخة الكاملة لسطح المكتب تتضمن "
            "شبكة تفاعلية بالتحرير المباشر، تقرير الجودة والإرهاق، جدول الأون كول، "
            "والطباعة الرسمية الكاملة.")


# ═════════════════════════════════════════════════════════════
#  Demo helpers
# ═════════════════════════════════════════════════════════════
DEMO_NAMES_AR = ["فاطمة أحمد", "نورة خالد", "سارة محمد", "ريم عبدالله", "منيرة سعد",
                 "هند فهد", "العنود ناصر", "لطيفة سلطان", "جواهر تركي", "مها عادل",
                 "أمل يوسف", "رهف ماجد", "شهد بندر", "غادة طلال", "دانة مشعل"]
DEMO_NAMES_EN = ["Maria Santos", "Angela Reyes", "Priya Sharma", "Grace Fernandes",
                 "Joy Mendoza", "Anita Kumar", "Rose Villanueva", "Leila Hassan",
                 "Mona Ibrahim", "Sara Mostafa", "Huda Ali", "Nour Khalil",
                 "Rita Thomas", "Jasmine Cruz", "Divya Nair"]


def seed_demo_data(n=45):
    """Fill the session DB with a realistic demo department."""
    import random
    rnd = random.Random(42)
    # wipe staff
    for e in db.get_all_employees():
        db.delete_employee(e["id"])
    sa_pool = DEMO_NAMES_AR + [f"{n1} {n2}" for n1 in ["عائشة", "بدور", "وضحى"]
                               for n2 in ["العتيبي", "الشمري", "القحطاني", "الدوسري"]]
    ns_pool = DEMO_NAMES_EN + [f"{n} {s}" for n in ["Ana", "Liza", "Farah"]
                               for s in ["Garcia", "Dela Cruz", "Ahmed", "Khan"]]
    ids = []
    for i in range(n):
        saudi = i < int(n * 0.45)
        name = (sa_pool[i % len(sa_pool)] if saudi
                else ns_pool[i % len(ns_pool)])
        if i >= len(sa_pool) and saudi:
            name += f" {i}"
        nat = "Saudi" if saudi else rnd.choice(
            ["Egyptian", "Jordanian", "Indian", "Filipino", "Pakistani"])
        certs = rnd.sample(CERT_OPTIONS, k=rnd.choice([0, 0, 1, 1, 2]))
        eid = db.add_employee(
            name=name, nationality_code=nat,
            experience_years=rnd.randint(1, 20), age=rnd.randint(24, 52),
            breastfeeding=1 if rnd.random() < 0.08 else 0,
            certificates=certs)
        ids.append(eid)
    # roles: first = head nurse, next 4 = shift incharges
    db.set_setting("head_nurse_id", str(ids[0]))
    db.set_setting("shift_incharge_ids", json.dumps(ids[1:5]))
    return len(ids)


def staffing_defaults(n_staff, codes):
    """Reasonable min/max per shift for the demo."""
    per = max(1, int(n_staff * 0.72 / max(1, len(codes))))
    return {c: (max(1, per - 1), per + 3) for c in codes}


def build_grid_html(schedule, year, month, sd, edy):
    """Colored roster table (screen palette) incl. leaves gold + SO cyan."""
    emp_map = {e["id"]: e for e in db.get_all_employees()}
    leave_map = _get_leave_map(db, year, month)
    so_map = _get_so_map(db, year, month)
    days = list(range(sd, edy + 1))
    hn = db.get_setting("head_nurse_id", "")
    rows_html = []
    head = "<tr><th class='nm' style='position:sticky;right:0'>الاسم</th>"
    for d in days:
        we = date(year, month, d).weekday() in WEEKEND_DAYS_SET
        head += (f"<th class='{'we' if we else ''}'>{d}<br>"
                 f"<span style='font-size:9px'>{DAY_ABBR[date(year, month, d).weekday()]}</span></th>")
    head += "<th>Work</th><th>N</th></tr>"
    for eid_str, day_map in schedule.items():
        emp = emp_map.get(int(eid_str))
        if not emp:
            continue
        name = emp["name"]
        if str(emp["id"]) == hn:
            name = "⭐ " + name
        if emp.get("breastfeeding"):
            name = "👶 " + name
        tr = f"<tr><td class='nm'>{name}</td>"
        work = nights = 0
        for d in days:
            s = day_map.get(str(d), OFF)
            if s != OFF:
                work += 1
            if s == "N":
                nights += 1
            we = date(year, month, d).weekday() in WEEKEND_DAYS_SET
            ld = leave_map.get((emp["id"], d))
            if ld and s == OFF:
                txt, bg, fg = ld["code"], ld["bg"], ld["fg"]
            elif s == OFF and d in so_map.get(emp["id"], set()):
                txt, bg, fg = SO_CODE, SO_BG, SO_FG
            else:
                c = SHIFT_COLORS.get(s, SHIFT_COLORS["OFF"])
                bg, fg = c["bg"], c["fg"]
                txt = s if s != OFF else "O"
                if we and s == OFF:
                    bg, fg = "#fecaca", "#7f1d1d"
            tr += f"<td style='background:{bg};color:{fg}'>{txt}</td>"
        tr += f"<td style='background:#f1f5f9'>{work}</td><td style='background:#f1f5f9'>{nights}</td></tr>"
        rows_html.append(tr)
    legend = ("<div style='margin:6px 0'>"
              + "".join(f"<span class='chip' style='background:{SHIFT_COLORS[c]['bg']};"
                        f"color:{SHIFT_COLORS[c]['fg']}'>{c}</span>"
                        for c in st.session_state.get('shift_codes', []))
              + f"<span class='chip' style='background:{SHIFT_COLORS['OFF']['bg']};"
                f"color:{SHIFT_COLORS['OFF']['fg']}'>O = أوف</span>"
              + f"<span class='chip' style='background:{SO_BG};color:{SO_FG}'>SO = أوف بطلب الموظفة</span>"
              + "<span class='chip' style='background:#f1c40f;color:#6b4e00'>AL/SL/... = إجازة</span>"
              + "<span class='chip' style='background:#7c2d12;color:#fff'>عمود غامق = ويكند</span></div>")
    return legend + ("<div class='grid-wrap' style='max-height:560px;overflow-y:auto'>"
                     f"<table class='sched'>{head}{''.join(rows_html)}</table></div>")


# ═════════════════════════════════════════════════════════════
#  PAGE 2 — Live demo
# ═════════════════════════════════════════════════════════════
def demo_page():
    st.title("🖥 جرّبي الأداة")

    tab_set, tab_staff, tab_cons, tab_gen = st.tabs(
        ["⚙️ ١· الإعدادات", "👩‍⚕️ ٢· الموظفات", "📋 ٣· القيود", "⚡ ٤· التوليد والجدول"])

    # ── TAB 1: Settings ──
    with tab_set:
        c1, c2, c3 = st.columns(3)
        with c1:
            wh = st.selectbox("نظام الدوام", ["8", "12", "6"],
                              format_func=lambda v: {"8": "8 ساعات (M/E/N)",
                                                     "12": "12 ساعة (D/N)",
                                                     "6": "6 ساعات (M1/M2/E/N)"}[v])
        with c2:
            month = st.selectbox("الشهر", list(range(1, 13)), index=10,
                                 format_func=lambda m: f"{m} — {MONTH_NAMES[m-1]}")
        with c3:
            year = st.selectbox("السنة", [2026, 2027], index=0)

        db.set_setting("work_hours", wh)
        db.set_setting("schedule_month", str(month))
        db.set_setting("schedule_year", str(year))
        codes = SHIFT_CONFIGS[wh]["shifts"]
        labels = SHIFT_CONFIGS[wh]["labels"]
        st.session_state.shift_codes = codes
        st.session_state.shift_labels = labels
        st.session_state.ym = (year, month)

        n_days = calendar.monthrange(year, month)[1]
        max_off = rule_max_offs(n_days, block_min_length(int(wh)))
        q1, q2 = st.columns(2)
        with q1:
            sa_off = st.selectbox("أوفات الموظفة السعودية/شهر",
                                  list(range(1, max_off + 1)),
                                  index=min(7, max_off - 1))
        with q2:
            ns_off = st.selectbox("أوفات غير السعودية/شهر",
                                  list(range(1, max_off + 1)),
                                  index=min(6, max_off - 1))
        db.set_setting("saudi_offs_per_month", str(sa_off))
        db.set_setting("nonsaudi_offs_per_month", str(ns_off))
        st.caption(f"الحد الأقصى المسموح لنظام {wh} ساعة هذا الشهر: {max_off} "
                   f"(محسوب من قاعدة الراحات 1–2 يوم)")

        st.markdown("**الحد الأدنى/الأقصى للتغطية لكل وردية:**")
        n_staff = len(db.get_all_employees())
        defaults = staffing_defaults(max(n_staff, 20), codes)
        cols = st.columns(len(codes))
        for i, c in enumerate(codes):
            with cols[i]:
                mn = st.number_input(f"{c} — أدنى", 1, 60, defaults[c][0], key=f"mn{c}")
                mx = st.number_input(f"{c} — أقصى", mn, 80,
                                     max(defaults[c][1], mn), key=f"mx{c}")
                db.set_setting(f"staff_{c}_min", str(mn))
                db.set_setting(f"staff_{c}_max", str(mx))

        st.session_state.time_limit = st.slider(
            "الحد الزمني للحل (ثانية) — وقت أطول = جودة وعدالة أعلى", 30, 180, 60, 15)

    # ── TAB 2: Staff ──
    with tab_staff:
        emps = db.get_all_employees()
        b1, b2, b3 = st.columns([1.3, 1, 1])
        with b1:
            demo_n = st.selectbox("حجم البيانات التجريبية", [20, 45, 100],
                                  index=1, label_visibility="collapsed")
        with b2:
            if st.button("✨ تعبئة بيانات تجريبية", use_container_width=True):
                with st.spinner("إنشاء القسم التجريبي..."):
                    seed_demo_data(demo_n)
                st.rerun()
        with b3:
            if emps and st.button("🗑 مسح كل الموظفات", use_container_width=True):
                for e in emps:
                    db.delete_employee(e["id"])
                st.rerun()

        with st.expander("➕ إضافة موظفة يدويًا"):
            with st.form("add_emp", clear_on_submit=True):
                a1, a2, a3, a4 = st.columns([2, 1.3, 1, 1])
                nm = a1.text_input("الاسم")
                nat = a2.selectbox("الجنسية", NATIONALITIES)
                age = a3.number_input("العمر", 18, 70, 30)
                exp = a4.number_input("الخبرة", 0, 50, 5)
                a5, a6 = st.columns([2, 1])
                certs = a5.multiselect("الشهادات", CERT_OPTIONS)
                bf = a6.checkbox("ساعة رضاعة 👶")
                if st.form_submit_button("إضافة"):
                    if len(nm.strip()) >= 3:
                        db.add_employee(name=nm.strip(), nationality_code=nat,
                                        age=age, experience_years=exp,
                                        certificates=certs,
                                        breastfeeding=1 if bf else 0)
                        st.rerun()
                    else:
                        st.warning("أدخلي اسمًا صحيحًا (3 أحرف فأكثر)")

        with st.expander("📥 استيراد ذكي من Excel / CSV"):
            up = st.file_uploader("ارفعي أي ملف موظفات — سيُنظَّف ويُفحص قبل الإدخال",
                                  type=["xlsx", "xls", "csv"])
            if up is not None:
                tmp_path = os.path.join(st.session_state.workdir, up.name)
                with open(tmp_path, "wb") as f:
                    f.write(up.getbuffer())
                try:
                    grid = read_grid(tmp_path)
                    hdr, mapping, _sc = detect_header(grid)
                except Exception as ex:
                    st.error(f"تعذّرت قراءة الملف: {ex}")
                    grid, mapping, hdr = [], {}, -1
                if grid and "name" in mapping:
                    headers = (grid[hdr] if hdr >= 0
                               else [f"عمود {i+1}" for i in
                                     range(max(len(r) for r in grid[:5]))])
                    opts = ["— تجاهل —"] + [f"{i+1}: {str(h).strip()[:24]}"
                                            for i, h in enumerate(headers)]
                    st.markdown("**مطابقة الأعمدة** (تعرّفت تلقائيًا — صححي إن لزم):")
                    mcols = st.columns(len(FIELD_LABELS))
                    chosen = {}
                    for i, (f, lbl) in enumerate(FIELD_LABELS.items()):
                        cur = mapping.get(f)
                        with mcols[i]:
                            sel = st.selectbox(lbl, opts,
                                               index=(cur + 1) if cur is not None else 0,
                                               key=f"map_{f}_{up.name}")
                        if not sel.startswith("—"):
                            chosen[f] = int(sel.split(":")[0]) - 1
                    if "name" in chosen:
                        existing = [e["name"] for e in db.get_all_employees()]
                        recs = build_records(grid, hdr, chosen, existing)
                        icon = {"ok": "✅", "warn": "⚠️", "reject": "⛔"}
                        import pandas as pd
                        df = pd.DataFrame([{
                            "الحالة": icon[r["status"]], "الاسم": r["name"],
                            "الجنسية": r["nationality_code"], "عربي؟": r["arabic"],
                            "العمر": r["age"] or "", "الخبرة": r["experience"] or "",
                            "الشهادات": " ".join(r["certificates"]),
                            "الملاحظات": " · ".join(r["issues"])} for r in recs])
                        st.dataframe(df, use_container_width=True, height=240)
                        ok_n = sum(1 for r in recs if r["status"] == "ok")
                        wr_n = sum(1 for r in recs if r["status"] == "warn")
                        inc_w = st.checkbox("استيراد التي تحتاج مراجعة (⚠) أيضًا", True)
                        todo = [r for r in recs if r["status"] == "ok"
                                or (inc_w and r["status"] == "warn")]
                        if st.button(f"⬇ استيراد ({len(todo)})"):
                            for r in todo:
                                db.add_employee(
                                    name=r["name"],
                                    nationality_code=r["nationality_code"],
                                    experience_years=r["experience"],
                                    age=r["age"] or 25,
                                    breastfeeding=r["breastfeeding"],
                                    certificates=r["certificates"])
                            st.success(f"تم استيراد {len(todo)} موظفة بكل بياناتهن")
                            st.rerun()
                elif grid:
                    st.warning("لم أتعرف على عمود الأسماء — تأكدي من وجود عنوان "
                               "مثل: الاسم / Name")

        emps = db.get_all_employees()
        st.markdown(f"**الموظفات الحاليات: {len(emps)}** (الحد الأقصى للتجربة {MAX_STAFF})")
        if emps:
            hn = db.get_setting("head_nurse_id", "")
            try:
                ic = set(json.loads(db.get_setting("shift_incharge_ids", "[]")))
            except Exception:
                ic = set()
            import pandas as pd
            df = pd.DataFrame([{
                "الدور": ("⭐ رئيسة" if str(e["id"]) == hn
                          else "🟢 انشارج" if e["id"] in ic else ""),
                "الاسم": e["name"], "الجنسية": e.get("nationality_code", ""),
                "عربي؟": e.get("arabic_speaker", ""),
                "العمر": e.get("age", ""), "الخبرة": e.get("experience_years", ""),
                "الشهادات": " ".join(e.get("certificates") or []),
                "👶": "👶" if e.get("breastfeeding") else ""} for e in emps])
            st.dataframe(df, use_container_width=True, height=320)

            names = {f'{e["name"]} (#{e["id"]})': e["id"] for e in emps}
            r1, r2 = st.columns(2)
            with r1:
                hn_sel = st.selectbox("⭐ رئيسة القسم", ["— بدون —"] + list(names))
                db.set_setting("head_nurse_id",
                               str(names[hn_sel]) if hn_sel in names else "")
            with r2:
                ic_sel = st.multiselect("🟢 مسؤولات الورديات (انشارج)", list(names),
                                        default=[k for k, v in names.items() if v in ic])
                db.set_setting("shift_incharge_ids",
                               json.dumps([names[k] for k in ic_sel]))

    # ── TAB 3: Constraints ──
    with tab_cons:
        emps = db.get_all_employees()
        if not emps:
            st.info("أضيفي موظفات أولًا (تبويب ٢)")
        else:
            names = {f'{e["name"]} (#{e["id"]})': e for e in emps}
            year, month = st.session_state.get("ym", (2026, 11))
            n_days = calendar.monthrange(year, month)[1]

            st.markdown("#### 🏖 تسجيل إجازة")
            with st.form("add_leave", clear_on_submit=True):
                l1, l2, l3, l4 = st.columns([2, 1, 1, 1.4])
                who = l1.selectbox("الموظفة", list(names))
                d_from = l2.number_input("من يوم", 1, n_days, 1)
                d_to = l3.number_input("إلى يوم", 1, n_days, 5)
                ltype = l4.selectbox("النوع", LEAVE_TYPES)
                if st.form_submit_button("➕ إضافة الإجازة"):
                    e = names[who]
                    db.add_constraint("leave", "mandatory", {
                        "employee_id": e["id"],
                        "employee_name": f'{e.get("code", "")}. {e["name"]}',
                        "start_date": date(year, month, min(d_from, d_to)).isoformat(),
                        "end_date": date(year, month, max(d_from, d_to)).isoformat(),
                        "leave_type": ltype})
                    st.rerun()

            st.markdown("#### 💠 طلب أوف بطلب الموظفة (SO)")
            with st.form("add_so", clear_on_submit=True):
                s1, s2 = st.columns([2, 2])
                who2 = s1.selectbox("الموظفة", list(names), key="so_who")
                so_days = s2.multiselect("الأيام المطلوبة",
                                         list(range(1, n_days + 1)))
                if st.form_submit_button("➕ تسجيل الطلب") and so_days:
                    e = names[who2]
                    iso = [date(year, month, d).isoformat() for d in so_days]
                    db.add_constraint("forced_shift", "mandatory", {
                        "employee_ids": [e["id"]], "days": so_days,
                        "start_date": iso[0], "end_date": iso[-1],
                        "dates": iso, "shifts": ["OFF"]})
                    st.rerun()

            cons = [c for c in db.get_constraints("mandatory")
                    if c["constraint_type"] in ("leave", "forced_shift")]
            if cons:
                st.markdown("**القيود المسجلة:**")
                emap = {e["id"]: e["name"] for e in emps}
                for c in cons:
                    p = c["params"]
                    if c["constraint_type"] == "leave":
                        txt = (f"🏖 إجازة {p.get('leave_type')} — "
                               f"{emap.get(p.get('employee_id'), '?')} "
                               f"({p.get('start_date')} ← {p.get('end_date')})")
                    else:
                        kind = "SO أوف" if set(p.get("shifts", [])) == {"OFF"} \
                            else f"فترة {'/'.join(p.get('shifts', []))}"
                        nm = "، ".join(emap.get(i, "?") for i in p.get("employee_ids", []))
                        txt = f"💠 {kind} — {nm} (أيام {p.get('days')})"
                    cc1, cc2 = st.columns([6, 1])
                    cc1.write(txt)
                    if cc2.button("🗑", key=f"delc{c['id']}"):
                        db.delete_constraint(c["id"])
                        st.rerun()

    # ── TAB 4: Generate ──
    with tab_gen:
        emps = db.get_all_employees()
        year, month = st.session_state.get("ym", (2026, 11))
        if not emps:
            st.info("أضيفي موظفات أولًا — أسرع طريق: زر «✨ تعبئة بيانات تجريبية» في تبويب ٢")
        elif len(emps) > MAX_STAFF:
            st.error(f"حد التجربة {MAX_STAFF} موظفة (لديك {len(emps)})")
        else:
            st.markdown(f"**الشهر:** {MONTH_NAMES[month-1]} {year} · "
                        f"**الموظفات:** {len(emps)} · "
                        f"**النظام:** {db.get_setting('work_hours', '8')} ساعة")
            if st.button("⚡ توليد الجدول", type="primary", use_container_width=True):
                with st.spinner("المحرك يحسب أفضل جدول... "
                                f"(حتى {st.session_state.get('time_limit', 60)} ثانية)"):
                    eng = ScheduleEngine(db)
                    schedule, order, warns, info = eng.generate(
                        year, month,
                        time_limit=st.session_state.get("time_limit", 60))
                st.session_state.result = (schedule, warns, info, year, month)

            if "result" in st.session_state:
                schedule, warns, info, ry, rm = st.session_state.result
                if (ry, rm) != (year, month):
                    st.warning("النتيجة المعروضة لشهر سابق — ولّدي من جديد")
                if schedule:
                    st.success(f"✓ تم التوليد — الحالة: {info.get('status')}")
                    for w in warns:
                        st.warning(w)
                    n_days = calendar.monthrange(ry, rm)[1]
                    st.markdown(build_grid_html(schedule, ry, rm, 1, n_days),
                                unsafe_allow_html=True)

                    st.divider()
                    codes = st.session_state.get("shift_codes", ["M", "E", "N"])
                    labels = st.session_state.get("shift_labels", {})
                    d1, d2 = st.columns(2)
                    with d1:
                        try:
                            fp = do_excel_export(db, ry, rm, schedule, codes,
                                                 labels,
                                                 db.get_setting("work_hours", "8"))
                            st.download_button(
                                "📥 تنزيل Excel الرسمي (بالهوية والاعتمادات)",
                                data=open(fp, "rb").read(),
                                file_name=os.path.basename(str(fp)),
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True)
                        except Exception as ex:
                            st.error(f"تعذّر تجهيز Excel: {ex}")
                    with d2:
                        try:
                            hp = _generate_pdf_html(db, ry, rm, schedule, codes,
                                                    labels,
                                                    db.get_setting("work_hours", "8"))
                            st.download_button(
                                "🖨 تنزيل نسخة الطباعة (HTML → PDF من المتصفح)",
                                data=open(hp, "rb").read(),
                                file_name=os.path.basename(str(hp)),
                                mime="text/html", use_container_width=True)
                        except Exception as ex:
                            st.error(f"تعذّر تجهيز نسخة الطباعة: {ex}")
                else:
                    st.error("تعذّر التوليد — راجعي التحذيرات:")
                    for w in warns:
                        st.warning(w)


# ─────────────────────────────────────────────────────────────
if page.startswith("📖"):
    landing_page()
else:
    demo_page()

st.markdown("<div class='foot'>NurseScheduler Web Trial · "
            "Empowered by <b>Sabah Alharbi</b> · "
            "Maternity &amp; Children Hospital — Hafr Al Batin Health Cluster</div>",
            unsafe_allow_html=True)
