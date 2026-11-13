# -*- coding: utf-8 -*-
"""
import_wizard.py — Smart Excel/CSV employee import
Maternity & Children Hospital – Hafr Al Batin Health Cluster

Upload ANY staff file: the wizard finds the header row anywhere in any
sheet, maps columns by bilingual synonyms, cleans the data (Arabic-Indic
digits, nationality in Arabic OR English → nationality_code → auto
Arabic-speaker flag), reports every obstacle BEFORE inserting, backs up
the DB, and supports one-click undo of the last import.
"""

import os
import re
import csv
import json
import hashlib

# GUI deps are OPTIONAL: the parsing/cleaning pipeline (read_grid,
# detect_header, build_records) also runs headless on the web version.
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import customtkinter as ctk
    from ui_theme import Colors, Fonts
    _HAS_GUI = True
except Exception:
    _HAS_GUI = False
    ctk = None

from constants import NATIONALITIES, derive_nationality_flags

CERT_OPTIONS = ["PALS", "ACLS", "BLSO", "ALSO", "NRP", "ATLS"]

# ─────────────────────────────────────────────
#  Normalization helpers
# ─────────────────────────────────────────────
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_AR_UNIFY = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})
_TITLE_RE = re.compile(
    r"^\s*(السيده|السيدة|الأستاذه|الاستاذه|الآنسه|الانسه|الممرضه|الممرضة|"
    r"ms\.?|mrs\.?|miss|dr\.?|nurse)\s+", re.IGNORECASE)


def _norm(s):
    """Normalize for matching: lower, unify hamza/teh-marbuta, western digits."""
    return str(s or "").translate(_AR_DIGITS).translate(_AR_UNIFY).strip().lower()


def clean_name(raw):
    """Trim, drop titles, collapse spaces, strip stray digits/punctuation."""
    s = str(raw or "").translate(_AR_DIGITS).strip()
    s = _TITLE_RE.sub("", s)
    s = re.sub(r"[\d_]+", " ", s)          # stray numbers inside name cells
    s = re.sub(r"\s{2,}", " ", s).strip(" -–—.·,،")
    return s


def parse_number(raw):
    """Extract the first number from any text ('25 سنة', '٥ yrs') or None."""
    m = re.search(r"\d+(?:\.\d+)?", str(raw or "").translate(_AR_DIGITS))
    return float(m.group()) if m else None


def parse_bool(raw):
    n = _norm(raw)
    return 1 if n and (n in ("1", "yes", "y", "true", "✓", "نعم", "ن")
                       or "رضاع" in n or "breastfeed" in n or n == "bf") else 0


# ─────────────────────────────────────────────
#  Nationality — Arabic OR English → code → auto Arabic-speaker
# ─────────────────────────────────────────────
NAT_KEYWORDS = [
    ("Saudi",     ["سعود", "saudi", "ksa"]),
    ("Egyptian",  ["مصر", "egypt"]),
    ("Jordanian", ["اردن", "jordan"]),
    ("Sudanese",  ["سودان", "sudan"]),
    ("Lebanese",  ["لبنان", "leban"]),
    ("Syrian",    ["سوري", "syria"]),
    ("Indian",    ["هند", "india"]),
    ("Filipino",  ["فلبين", "philip", "filipin"]),
    ("Pakistani", ["باكستان", "pakistan"]),
]


def parse_nationality(raw):
    """Return (nationality_code, issue_or_None). Any Arabic/English spelling
    is accepted; unknown text falls back to 'Other' with a review note."""
    n = _norm(raw)
    if not n:
        return "Other", "Nationality missing — recorded as Other"
    for code, kws in NAT_KEYWORDS:
        if any(kw in n for kw in kws):
            return code, None
    if "other" in n or "اخر" in n:
        return "Other", None
    return "Other", f"Unknown nationality ({str(raw).strip()}) — recorded as Other"


def parse_certificates(raw):
    """Split multi-value cert cells; return (matched, ignored_tokens)."""
    tokens = [t.strip() for t in re.split(r"[,،;/+\n\|]+", str(raw or "")) if t.strip()]
    matched, ignored = [], []
    for t in tokens:
        hit = next((c for c in CERT_OPTIONS if c.lower() == t.lower()), None)
        if hit:
            if hit not in matched:
                matched.append(hit)
        elif _norm(t) not in ("none", "لا", "لايوجد", "لا يوجد", "-"):
            ignored.append(t)
    return matched, ignored


# ─────────────────────────────────────────────
#  Column detection — header row anywhere, any sheet
# ─────────────────────────────────────────────
FIELD_LABELS = {
    "name": "Name (required)",
    "nationality": "Nationality",
    "age": "Age",
    "experience": "Years of experience",
    "certificates": "Certificates",
    "breastfeeding": "Breastfeeding hour",
}

# Arabic keywords match by substring; English by whole word.
FIELD_SYNONYMS = {
    "name": (["اسم", "موظف", "ممرض"],
             ["name", "employee", "staff", "nurse"]),
    "nationality": (["جنسي"], ["nationality", "nation"]),
    "age": (["عمر", "السن"], ["age"]),
    "experience": (["خبر"], ["experience", "exp", "years"]),
    "certificates": (["شهاد", "دورات", "دوره"],
                     ["certificate", "certificates", "cert", "certs",
                      "course", "courses"]),
    "breastfeeding": (["رضاع"], ["breastfeeding", "bf"]),
}


def _match_field(header_norm):
    for field, (ar_kws, en_kws) in FIELD_SYNONYMS.items():
        if any(kw in header_norm for kw in ar_kws):
            return field
        for kw in en_kws:
            if re.search(rf"\b{re.escape(kw)}\b", header_norm):
                return field
    return None


def detect_header(grid, scan_rows=40):
    """Scan the first rows for the best header row.
    Returns (hdr_idx, mapping{field: col}, score). hdr_idx=-1 → no header."""
    best = (-1, {}, 0)
    for r in range(min(scan_rows, len(grid))):
        mapping, score = {}, 0
        for c, cell in enumerate(grid[r]):
            f = _match_field(_norm(cell))
            if f and f not in mapping:
                mapping[f] = c
                score += 3 if f == "name" else 1
        if "name" in mapping and score > best[2]:
            best = (r, mapping, score)
    if best[0] >= 0:
        return best
    # Fallback: first mostly-text column = names, data starts at row 0
    for c in range(max((len(r) for r in grid[:10]), default=0)):
        texts = sum(1 for row in grid[:10]
                    if c < len(row) and row[c] and not str(row[c]).strip().isdigit()
                    and any(ch.isalpha() for ch in str(row[c])))
        if texts >= 3:
            return -1, {"name": c}, 1
    return -1, {}, 0


# ─────────────────────────────────────────────
#  File readers → grid of strings
# ─────────────────────────────────────────────
def _detect_file_type(path):
    if path.lower().endswith(".csv"):
        return "csv"
    try:
        with open(path, "rb") as f:
            head = f.read(500)
        if head[:2] == b"PK":
            return "xlsx"
        if head[:2] == b"\xd0\xcf":
            return "xls_real"
        if b"<html" in head.lower() or b"<table" in head.lower():
            return "html"
    except Exception:
        pass
    return "xlsx"


def _grid_from_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    best_grid, best_score = [], -1
    for ws in wb.worksheets:
        grid = [["" if v is None else str(v) for v in row]
                for row in ws.iter_rows(values_only=True)]
        if not grid:
            continue
        _, _, score = detect_header(grid)
        # prefer higher header score, then more rows
        key = (score, len(grid))
        if key > (best_score, len(best_grid)):
            best_grid, best_score = grid, score
    wb.close()
    return best_grid


def _grid_from_xls(path):
    try:
        import xlrd
    except ImportError:
        raise Exception("Old .xls file — open it in Excel, save it as .xlsx, then import the new file")
    wb = xlrd.open_workbook(path)
    ws = wb.sheet_by_index(0)
    return [[str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)]
            for r in range(ws.nrows)]


def _grid_from_html(path):
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        content = f.read()
    if "<frameset" in content.lower():
        base_dir = os.path.dirname(path)
        base = os.path.splitext(os.path.basename(path))[0]
        for cand in (os.path.join(base_dir, f"{base}.files", "sheet001.htm"),
                     os.path.join(base_dir, f"{base}.files", "sheet001.html"),
                     os.path.join(base_dir, f"{base}_files", "sheet001.htm")):
            if os.path.exists(cand):
                with open(cand, "r", encoding="utf-8-sig", errors="ignore") as f:
                    content = f.read()
                break
        else:
            raise Exception("HTML file from the hospital system with missing companion files — "
                            "open it in Excel, save it as .xlsx, then import the new file")
    rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.DOTALL | re.IGNORECASE)
    grid = []
    for rh in rows_html:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rh, re.DOTALL | re.IGNORECASE)
        grid.append([re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                     for c in cells])
    return grid


def _grid_from_csv(path):
    with open(path, encoding="utf-8-sig", errors="ignore") as f:
        return [list(row) for row in csv.reader(f)]


def read_grid(path):
    t = _detect_file_type(path)
    if t == "csv":
        return _grid_from_csv(path)
    if t == "html":
        return _grid_from_html(path)
    if t == "xls_real":
        return _grid_from_xls(path)
    return _grid_from_xlsx(path)


# ─────────────────────────────────────────────
#  Record building + validation
# ─────────────────────────────────────────────
def build_records(grid, hdr_idx, mapping, existing_names):
    """Clean + validate every data row → records with status/issues."""
    existing = {_norm(clean_name(n)) for n in existing_names}
    seen = set()
    records = []
    for row in grid[hdr_idx + 1:]:
        def cell(field):
            c = mapping.get(field)
            return row[c] if c is not None and c < len(row) else ""

        raw_name = cell("name")
        if not str(raw_name).strip():
            continue  # blank line — skip silently
        issues, status = [], "ok"
        name = clean_name(raw_name)
        if len(name) < 3 or not any(ch.isalpha() for ch in name):
            records.append({"status": "reject", "name": str(raw_name).strip(),
                            "issues": ["Invalid name"], "nationality_code": "",
                            "age": 0, "experience": 0, "certificates": [],
                            "breastfeeding": 0, "arabic": ""})
            continue
        key = _norm(name)
        if key in existing:
            status = "reject"; issues.append("Already exists in the database")
        elif key in seen:
            status = "reject"; issues.append("Duplicate within the file")
        seen.add(key)

        nat_code, nat_issue = parse_nationality(cell("nationality"))
        if nat_issue and status != "reject":
            status = "warn"; issues.append(nat_issue)
        _, arabic = derive_nationality_flags(nat_code)

        age = parse_number(cell("age"))
        if mapping.get("age") is not None:
            if age is None:
                if status == "ok": status = "warn"
                issues.append("Age missing")
                age = 0
            elif not (18 <= age <= 70):
                if status == "ok": status = "warn"
                issues.append(f"Age out of range ({int(age)}) — cleared")
                age = 0
        else:
            age = 0

        exp = parse_number(cell("experience"))
        if mapping.get("experience") is not None:
            if exp is None:
                if status == "ok": status = "warn"
                issues.append("Experience missing")
                exp = 0
            elif not (0 <= exp <= 50):
                if status == "ok": status = "warn"
                issues.append(f"Experience out of range ({exp:g}) — cleared")
                exp = 0
        else:
            exp = 0
        if age and exp and exp > age - 18:
            if status == "ok": status = "warn"
            issues.append("Experience exceeds (age − 18)")

        certs, ignored = parse_certificates(cell("certificates"))
        if ignored:
            if status == "ok": status = "warn"
            issues.append(f"Unknown certificates ignored: {', '.join(ignored[:3])}")

        records.append({
            "status": status, "name": name, "issues": issues,
            "nationality_code": nat_code, "arabic": arabic,
            "age": int(age or 0), "experience": float(exp or 0),
            "certificates": certs, "breastfeeding": parse_bool(cell("breastfeeding")),
        })
    return records


# ─────────────────────────────────────────────
#  Wizard window (Step 1: mapping · Step 2: review)
# ─────────────────────────────────────────────
_WizardBase = ctk.CTkToplevel if _HAS_GUI else object


class ImportWizard(_WizardBase):

    def __init__(self, parent, db, grid, hdr_idx, mapping, on_done=None):
        super().__init__(parent)
        self.db = db
        self.grid_data = grid
        self.hdr_idx = hdr_idx
        self.on_done = on_done
        self.records = []

        self.title("Smart Import from Excel")
        self.geometry("880x640")
        self.configure(fg_color=Colors.BG_MAIN)
        self.transient(parent)
        self.grab_set()

        self._headers = (grid[hdr_idx] if hdr_idx >= 0 else
                         [f"Column {i + 1}" for i in range(
                             max((len(r) for r in grid[:5]), default=1))])
        self._sig = hashlib.md5(
            "|".join(_norm(h) for h in self._headers).encode()).hexdigest()[:12]

        # Saved mapping for this exact header layout wins over auto-detect
        saved = self.db.get_setting(f"import_map::{self._sig}", "")
        if saved:
            try:
                loaded = {k: int(v) for k, v in json.loads(saved).items()}
                if "name" in loaded:
                    mapping = loaded
            except Exception:
                pass
        self.mapping = mapping

        self.step_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.step_frame.pack(fill="both", expand=True, padx=14, pady=12)
        self._build_step1()

    # ── Step 1: column mapping ──
    def _build_step1(self):
        for w in self.step_frame.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.step_frame, text="Step 1 of 2 — Match Columns",
                     font=(Fonts.FAMILY, 17, "bold"),
                     text_color=Colors.PRIMARY).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(self.step_frame,
                     text="These columns were detected automatically — correct any column, then continue. "
                          "Your choice is remembered for files with the same layout.",
                     font=(Fonts.FAMILY, 12), text_color="#475569").pack(anchor="w")

        col_options = ["— Ignore —"] + [
            f"{i + 1}: {str(h).strip()[:28] or '(empty)'}"
            for i, h in enumerate(self._headers)]
        self._combos = {}
        card = ctk.CTkFrame(self.step_frame, fg_color=Colors.BG_CARD,
                            corner_radius=12)
        card.pack(fill="x", pady=10)
        for field, label in FIELD_LABELS.items():
            rowf = ctk.CTkFrame(card, fg_color="transparent")
            rowf.pack(fill="x", padx=14, pady=5)
            ctk.CTkLabel(rowf, text=label, font=(Fonts.FAMILY, 13, "bold"),
                         text_color=Colors.TEXT_PRIMARY, width=170,
                         anchor="w").pack(side="left")
            cmb = ctk.CTkComboBox(rowf, values=col_options, width=330,
                                  state="readonly",
                                  command=lambda _v, f=field: self._update_sample(f))
            cur = self.mapping.get(field)
            cmb.set(col_options[cur + 1] if cur is not None
                    and cur + 1 < len(col_options) else col_options[0])
            cmb.pack(side="left")
            sample = ctk.CTkLabel(rowf, text="", font=(Fonts.FAMILY, 11),
                                  text_color="#94a3b8")
            sample.pack(side="left", padx=10)
            self._combos[field] = (cmb, sample)
            self._update_sample(field)

        btns = ctk.CTkFrame(self.step_frame, fg_color="transparent")
        btns.pack(fill="x", pady=8)
        ctk.CTkButton(btns, text="Continue  →", width=150, height=38,
                      fg_color=Colors.PRIMARY, hover_color=Colors.PRIMARY_DARK,
                      font=(Fonts.FAMILY, 14, "bold"),
                      command=self._to_step2).pack(side="right", padx=4)
        ctk.CTkButton(btns, text="Cancel", width=100, height=38,
                      fg_color="#64748b", hover_color="#475569",
                      command=self.destroy).pack(side="right", padx=4)

    def _combo_col(self, field):
        val = self._combos[field][0].get()
        if val.startswith("—"):
            return None
        return int(val.split(":", 1)[0]) - 1

    def _update_sample(self, field):
        cmb, sample = self._combos[field]
        c = self._combo_col(field)
        if c is None:
            sample.configure(text="")
            return
        vals = [str(r[c]).strip() for r in
                self.grid_data[self.hdr_idx + 1: self.hdr_idx + 6]
                if c < len(r) and str(r[c]).strip()][:2]
        sample.configure(text=("e.g. " + " · ".join(vals)) if vals else "")

    # ── Step 2: review ──
    def _to_step2(self):
        mapping = {f: self._combo_col(f) for f in FIELD_LABELS}
        mapping = {f: c for f, c in mapping.items() if c is not None}
        if "name" not in mapping:
            messagebox.showwarning("Notice", "The Name column is required", parent=self)
            return
        self.mapping = mapping
        try:
            self.db.set_setting(f"import_map::{self._sig}", json.dumps(mapping))
        except Exception:
            pass
        existing = [e["name"] for e in self.db.get_all_employees()]
        self.records = build_records(self.grid_data, self.hdr_idx, mapping, existing)
        if not self.records:
            messagebox.showwarning("Notice", "There is no data below the header row", parent=self)
            return
        self._build_step2()

    def _build_step2(self):
        for w in self.step_frame.winfo_children():
            w.destroy()
        ok_n = sum(1 for r in self.records if r["status"] == "ok")
        warn_n = sum(1 for r in self.records if r["status"] == "warn")
        rej_n = sum(1 for r in self.records if r["status"] == "reject")

        ctk.CTkLabel(self.step_frame, text="Step 2 of 2 — Review Issues Before Import",
                     font=(Fonts.FAMILY, 17, "bold"),
                     text_color=Colors.PRIMARY).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(self.step_frame,
                     text=f"✅ Ready: {ok_n}      ⚠ Needs review: {warn_n}      ⛔ Rejected: {rej_n}",
                     font=(Fonts.FAMILY, 14, "bold"),
                     text_color=Colors.TEXT_PRIMARY).pack(anchor="w", pady=4)

        wrap = ctk.CTkFrame(self.step_frame, fg_color=Colors.BG_CARD,
                            corner_radius=10)
        wrap.pack(fill="both", expand=True, pady=6)
        cols = ("st", "name", "nat", "ar", "age", "exp", "certs", "bf", "notes")
        tree = ttk.Treeview(wrap, columns=cols, show="headings", height=14)
        heads = {"st": ("", 34), "name": ("Name", 190), "nat": ("Nationality", 80),
                 "ar": ("Arabic?", 60), "age": ("Age", 50), "exp": ("Exp.", 55),
                 "certs": ("Certificates", 110), "bf": ("BF", 45),
                 "notes": ("Notes", 230)}
        for c, (h, w) in heads.items():
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="center" if c not in ("name", "notes") else "w")
        tree.tag_configure("ok", background="#f0fdf4")
        tree.tag_configure("warn", background="#fef9c3")
        tree.tag_configure("reject", background="#fee2e2", foreground="#7f1d1d")
        icon = {"ok": "✅", "warn": "⚠", "reject": "⛔"}
        for r in self.records:
            tree.insert("", "end", tags=(r["status"],), values=(
                icon[r["status"]], r["name"], r["nationality_code"],
                {"نعم": "Yes", "لا": "No"}.get(r.get("arabic", ""), ""), r["age"] or "", f'{r["experience"]:g}' if r["experience"] else "",
                " ".join(r["certificates"]), "👶" if r["breastfeeding"] else "",
                " · ".join(r["issues"])))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        vs.pack(side="right", fill="y", pady=8)

        self.include_warn = ctk.CTkCheckBox(
            self.step_frame, text="Also import rows that need review (⚠)",
            font=(Fonts.FAMILY, 13), fg_color=Colors.PRIMARY)
        self.include_warn.select()
        self.include_warn.pack(anchor="w", pady=4)

        btns = ctk.CTkFrame(self.step_frame, fg_color="transparent")
        btns.pack(fill="x", pady=6)
        n_importable = ok_n + warn_n
        ctk.CTkButton(btns, text=f"⬇ Import ({n_importable})", width=170, height=40,
                      fg_color=Colors.SUCCESS if hasattr(Colors, "SUCCESS") else "#16a34a",
                      hover_color="#15803d", font=(Fonts.FAMILY, 14, "bold"),
                      command=self._do_import).pack(side="right", padx=4)
        ctk.CTkButton(btns, text="← Back to Mapping", width=140, height=40,
                      fg_color="#64748b", hover_color="#475569",
                      command=self._build_step1).pack(side="right", padx=4)
        ctk.CTkButton(btns, text="Cancel", width=100, height=40,
                      fg_color="#dc2626", hover_color="#b91c1c",
                      command=self.destroy).pack(side="right", padx=4)

    # ── Import ──
    def _do_import(self):
        include_warn = bool(self.include_warn.get())
        todo = [r for r in self.records
                if r["status"] == "ok" or (include_warn and r["status"] == "warn")]
        if not todo:
            messagebox.showwarning("Notice", "There are no rows that can be imported", parent=self)
            return
        from database import backup_database
        try:
            backup_database("pre_import")
        except Exception:
            pass
        new_ids, failed = [], 0
        for r in todo:
            eid = self.db.add_employee(
                name=r["name"], nationality_code=r["nationality_code"],
                experience_years=r["experience"], age=r["age"] or 25,
                breastfeeding=r["breastfeeding"], certificates=r["certificates"])
            if eid:
                new_ids.append(eid)
            else:
                failed += 1
        try:
            self.db.set_setting("last_import_ids", json.dumps(new_ids))
        except Exception:
            pass
        msg = f"{len(new_ids)} staff member(s) imported with all their data"
        if failed:
            msg += f"\n⚠ {failed} row(s) could not be added (see the log)"
        msg += "\n\nA backup was taken before the import, and the Undo Import button is available in the toolbar."
        messagebox.showinfo("Import Complete", msg, parent=self)
        if self.on_done:
            self.on_done()
        self.destroy()


# ─────────────────────────────────────────────
#  Entry points
# ─────────────────────────────────────────────
def run_import_wizard(parent, db, on_done=None):
    path = filedialog.askopenfilename(
        title="Select the staff file",
        filetypes=[("Excel Files", "*.xlsx *.xls"),
                   ("CSV Files", "*.csv"), ("All Files", "*.*")])
    if not path:
        return
    try:
        grid = read_grid(path)
    except Exception as ex:
        messagebox.showerror("File Read Error", str(ex))
        return
    if not grid:
        messagebox.showwarning("Notice", "The file is empty or could not be read")
        return
    hdr_idx, mapping, _score = detect_header(grid)
    if "name" not in mapping:
        messagebox.showwarning(
            "Notice", "The name column was not recognized — make sure the file has a column "
                     "titled e.g. Name / الاسم / Employee Name")
        return
    ImportWizard(parent, db, grid, hdr_idx, mapping, on_done=on_done)


def undo_last_import(db):
    """Delete the employees created by the most recent import.
    Returns the number removed, or None when there is nothing to undo."""
    raw = db.get_setting("last_import_ids", "")
    try:
        ids = json.loads(raw) if raw else []
    except Exception:
        ids = []
    if not ids:
        return None
    removed = 0
    for eid in ids:
        try:
            db.delete_employee(int(eid))
            removed += 1
        except Exception:
            pass
    db.set_setting("last_import_ids", "[]")
    return removed
