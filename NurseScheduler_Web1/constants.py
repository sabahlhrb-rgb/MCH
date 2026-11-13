"""
constants.py — Shared Constants for NurseScheduler
Maternity & Children Hospital – Hafr Al Batin Health Cluster

Single source of truth for colors, labels, and configuration values
used across UI, export, and engine modules.
"""

from typing import Dict, Tuple

# ─────────────────────────────────────────────────
#  SHIFT COLORS — Hex format (UI: Canvas, CustomTkinter)
# ─────────────────────────────────────────────────
SHIFT_COLORS: Dict[str, Dict[str, str]] = {
    # 12-hour mode
    "D":   {"bg": "#dbeafe", "fg": "#1e40af"},   # Blue — Day
    "N":   {"bg": "#1e1e2e", "fg": "#e2e8f0"},   # Dark/Black — Night

    # 8-hour mode
    "M":   {"bg": "#dbeafe", "fg": "#1e40af"},   # Blue — Morning
    "E":   {"bg": "#d1fae5", "fg": "#065f46"},   # Green — Evening

    # 6-hour mode
    "M1":  {"bg": "#dbeafe", "fg": "#1e40af"},   # Blue — Morning 1
    "M2":  {"bg": "#bae6fd", "fg": "#0c4a6e"},   # Lighter Blue — Morning 2

    # Shared
    "OFF": {"bg": "#fee2e2", "fg": "#991b1b"},   # Red — OFF
}

# ─────────────────────────────────────────────────
#  SHIFT COLORS — RGB tuple format (Export: openpyxl, PIL, HTML)
# ─────────────────────────────────────────────────
SHIFT_COLORS_RGB: Dict[str, Dict[str, Tuple[int, int, int]]] = {
    "D":   {"bg": (219, 234, 254), "fg": (30, 64, 175)},
    "N":   {"bg": (30, 30, 46),    "fg": (226, 232, 240)},
    "M":   {"bg": (219, 234, 254), "fg": (30, 64, 175)},
    "E":   {"bg": (209, 250, 229), "fg": (6, 95, 70)},
    "M1":  {"bg": (219, 234, 254), "fg": (30, 64, 175)},
    "M2":  {"bg": (186, 230, 253), "fg": (12, 74, 110)},
    "OFF": {"bg": (254, 226, 226), "fg": (153, 27, 27)},
}

# ─────────────────────────────────────────────────
#  PRINT PALETTE — muted, ink-friendly colors for exported documents.
#  Screen UI keeps the vivid palette; printed/exported rosters use these.
# ─────────────────────────────────────────────────
SHIFT_COLORS_PRINT_RGB: Dict[str, Dict[str, Tuple[int, int, int]]] = {
    "D":   {"bg": (239, 246, 255), "fg": (30, 58, 138)},
    "N":   {"bg": (203, 213, 225), "fg": (15, 23, 42)},
    "M":   {"bg": (239, 246, 255), "fg": (30, 58, 138)},
    "E":   {"bg": (236, 253, 245), "fg": (6, 95, 70)},
    "M1":  {"bg": (239, 246, 255), "fg": (30, 58, 138)},
    "M2":  {"bg": (224, 242, 254), "fg": (12, 74, 110)},
    "OFF": {"bg": (248, 250, 252), "fg": (148, 163, 184)},
}
WEEKEND_OFF_PRINT_BG_RGB = (241, 245, 249)
WEEKEND_OFF_PRINT_FG_RGB = (100, 116, 139)

# Document control (hospital form identity)
DOC_FORM_NO = "NUR-SCH-001"
DOC_VERSION = "2.0"


# ─────────────────────────────────────────────────
#  HIJRI DATE — Kuwaiti/tabular algorithm (±1 day, fine for month labels)
# ─────────────────────────────────────────────────
HIJRI_MONTHS = [
    "محرم", "صفر", "ربيع الأول", "ربيع الآخر", "جمادى الأولى", "جمادى الآخرة",
    "رجب", "شعبان", "رمضان", "شوال", "ذو القعدة", "ذو الحجة",
]


def gregorian_to_hijri(gy: int, gm: int, gd: int):
    """Tabular conversion → (hijri_year, hijri_month 1-12, hijri_day)."""
    a = (14 - gm) // 12
    y = gy + 4800 - a
    m = gm + 12 * a - 3
    jd = gd + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    l = jd - 1948440 + 10632
    n = (l - 1) // 10631
    l = l - 10631 * n + 354
    j = ((10985 - l) // 5316) * ((50 * l) // 17719) + (l // 5670) * ((43 * l) // 15238)
    l = l - ((30 - j) // 15) * ((17719 * j) // 50) - (j // 16) * ((15238 * j) // 43) + 29
    hm = (24 * l) // 709
    hd = l - (709 * hm) // 24
    hy = 30 * n + j - 30
    return hy, hm, hd


def hijri_month_label(year: int, month: int, last_day: int) -> str:
    """Hijri label for a Gregorian month, e.g. 'ذو القعدة – ذو الحجة 1447هـ'."""
    y1, m1, _ = gregorian_to_hijri(year, month, 1)
    y2, m2, _ = gregorian_to_hijri(year, month, last_day)
    if (y1, m1) == (y2, m2):
        return f"{HIJRI_MONTHS[m1 - 1]} {y1}هـ"
    if y1 == y2:
        return f"{HIJRI_MONTHS[m1 - 1]} – {HIJRI_MONTHS[m2 - 1]} {y1}هـ"
    return f"{HIJRI_MONTHS[m1 - 1]} {y1} – {HIJRI_MONTHS[m2 - 1]} {y2}هـ"


# ─────────────────────────────────────────────────
#  WEEKEND COLORS
# ─────────────────────────────────────────────────
WEEKEND_HEADER_BG = "#7c2d12"          # Header background for weekend columns (hex)
WEEKEND_OFF_BG_HEX = "FECACA"          # Weekend OFF cell background (hex, no #)
WEEKEND_OFF_FG_HEX = "7F1D1D"          # Weekend OFF cell text color (hex, no #)
WEEKEND_OFF_BG_RGB = (254, 202, 202)   # Weekend OFF cell background (RGB)
WEEKEND_OFF_FG_RGB = (127, 29, 29)     # Weekend OFF cell text color (RGB)

# Header colors for export
HEADER_BG_RGB = (30, 58, 95)
SUMMARY_BG_RGB = (30, 41, 59)

# ─────────────────────────────────────────────────
#  CALENDAR & WEEKEND
# ─────────────────────────────────────────────────
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

WEEKEND_DAYS_SET = {4, 5}   # Friday=4, Saturday=5 in Python weekday()
FRIDAY = 4
SATURDAY = 5

# ─────────────────────────────────────────────────
#  CONSTRAINT WEIGHTS — Centralized for easy tuning
# ─────────────────────────────────────────────────
CONSTRAINT_WEIGHTS: Dict[str, int] = {
    # Layer 2 — High importance
    "staff_min":        2500,   # Rule 2: Staff MIN per shift/day — highest weight;
                                # soft so generation NEVER fails, shortage is reported
    "staff_max":        2000,   # Rule 2b: Staff MAX per shift per day
    "block_types":      1500,   # Rule 14c: Max 2 shift types per work block
    "incharge_cover":    800,   # Rule 8: Shift incharge coverage
    "pairing":           800,   # Rule 9: Mandatory pairing
    "separation":        800,   # Rule 10: Mandatory separation
    "trainee_pairing":  1800,   # Rule 23: Trainee must share shift with a trainer
    "night_fairness":    600,   # Rule 11: Night fairness tolerance
    "evening_fairness":  600,   # Rule 12: Evening fairness tolerance
    "continuity":        500,   # Rule 14: Shift continuity (4-6 days blocks)
    "max_nights_month":  500,   # Rule 14b: Max 13 nights per month
    "breastfeeding":     500,   # Rule 13: Breastfeeding non-overlap (avoid same shift)
    "arabic_presence":   800,   # Rule 18b: >=1 Arabic speaker per shift per day
    "off_exact":         400,   # Rule 5b: Nationality OFF exact match

    # Rule 22 — Certificate coverage (SOFT, never blocks generation)
    "cert_coverage":     600,   # Each certificate held by staff present in every shift

    # Block system (Rule B) — soft companions to the hard block rules
    "single_off":        700,   # Isolated 1-day OFF between two work blocks (prefer 2-day rests)
    "block_start":       120,   # Each extra block start (prefer longer blocks, up to 6 days)

    # Layer 3 — Preferences
    "weekend_off":       100,   # Rule 16: Weekend OFF preferences
    "forced_shift":     3000,   # Rule 20: Forced specific-day shifts — approved
                                # staff requests outrank even staff_min shortage
    "forced_weekend_off": 3000, # Rule 21: Forced weekend OFF — same tier
    "stability":           2,   # Rule 24: keep previously saved cells on regeneration
                                # (tie-breaker only — never fights real constraints)
    "dominance":          50,   # Rule 15: Shift dominance cap ≤70%
    "preferred_shift":    40,   # Rule 19: Personal shift preferences
    "arabic_balance":      4,   # Rule 18: Arabic speaker balance
    "experience_balance":  3,   # Rule 17: Experience balance across shifts
}

# ─────────────────────────────────────────────────
#  LEAVE TYPES — one distinctive GOLD color for ALL leave types.
#  The cell text still carries the type code (AL/SL/ML/...) so the
#  reader knows WHICH leave it is at a glance.
# ─────────────────────────────────────────────────
LEAVE_BG = "#f1c40f"   # gold
LEAVE_FG = "#6b4e00"   # dark bronze text on gold

LEAVE_CODES: Dict[str, str] = {
    "Annual Leave": "AL",
    "Maternity Leave": "ML",
    "Sick Leave": "SL",
    "Companion Leave": "CL",
    "Exam Leave": "XL",
    "Baby Care Leave": "BC",
    "Study Leave": "ST",
    "Haji Leave": "HJ",
    "Eid Leave": "EID",
    "Training Leave": "TR",
    "Compensatory Leave": "CO",
    "Other": "LV",
}


def get_leave_display(leave_type: str):
    """Return {'code','bg','fg'} — gold cell with the leave-type code."""
    return {"code": LEAVE_CODES.get(leave_type, "LV"),
            "bg": LEAVE_BG, "fg": LEAVE_FG}


# ─────────────────────────────────────────────────
#  STAFF-REQUESTED OFF (SO) — a forced-OFF day requested by the employee
#  (Forced Shifts tab, shifts=["OFF"]). Shown as "SO" so the roster reader
#  can tell an employee request apart from a scheduler-assigned OFF.
# ─────────────────────────────────────────────────
SO_CODE = "SO"
SO_BG = "#cffafe"            # light cyan — distinct from red OFF and gold leave
SO_FG = "#155e75"
SO_BG_RGB = (207, 250, 254)
SO_FG_RGB = (21, 94, 117)
SO_PRINT_BG_RGB = (236, 254, 255)   # muted print variant
SO_PRINT_FG_RGB = (21, 94, 117)


# ─────────────────────────────────────────────────
#  NATIONALITIES — English dropdown; Saudi/Arabic flags derived automatically
# ─────────────────────────────────────────────────
NATIONALITIES = [
    "Saudi", "Egyptian", "Jordanian", "Sudanese", "Lebanese", "Syrian",
    "Indian", "Filipino", "Pakistani", "Other",
]

# Nationalities counted as Arabic speakers (everything else = non-Arabic)
ARABIC_SPEAKING_NATIONALITIES = {
    "Saudi", "Egyptian", "Jordanian", "Sudanese", "Lebanese", "Syrian",
}


def derive_nationality_flags(nationality_code: str):
    """
    Derive the internal (Arabic) classification fields from the English
    nationality code. Returns (nationality_ar, arabic_speaker_ar) using the
    exact values the engine/export/quotas expect.
    """
    nat = "سعودي" if nationality_code == "Saudi" else "غير سعودي"
    arabic = "نعم" if nationality_code in ARABIC_SPEAKING_NATIONALITIES else "لا"
    return nat, arabic


# ─────────────────────────────────────────────────
#  SOLVER DEFAULTS
# ─────────────────────────────────────────────────
SOLVER_TIME_LIMIT = 240        # seconds (normal mode)
SOLVER_EMERGENCY_LIMIT = 30    # seconds (emergency mode)
SOLVER_WORKERS = 8             # parallel workers
SOLVER_LINEARIZATION = 2       # linearization level


# ─────────────────────────────────────────────────
#  OFF QUOTA LIMIT — derived from the block rule
# ─────────────────────────────────────────────────
def block_min_length(work_hours) -> int:
    """Shortest work block: 12h shifts 2 days, 8h/6h shifts 4 days."""
    return 2 if str(work_hours) == "12" else 4


def rule_max_offs(n_days: int, min_block: int) -> int:
    """Most OFF days per month the block rule allows: rests of 1-2 days
    between work blocks of at least `min_block` days, keeping 2 days of
    slack so the rhythm is not rigid (rigid rhythms leave whole shifts empty).
    8h/6h → 10 · 12h → 14 (30-day month) / 15 (31-day month).
    The user's quota is capped to this — the rule itself is never relaxed."""
    best = 0
    for t in range(n_days + 1):
        rest_gaps = (n_days - t) // min_block + 1   # incl. both month edges
        if rest_gaps * 2 >= t + 2:
            best = t
    return best


def get_cell_colors(shift: str, is_weekend: bool, fmt: str = "hex",
                    print_palette: bool = False):
    """
    Get cell background and foreground colors for a shift.
    Centralizes the weekend color override logic in ONE place.

    Args:
        shift: Shift code (M, E, N, OFF, etc.)
        is_weekend: Whether the day is a weekend day
        fmt: "hex" for hex strings (no #), "rgb" for RGB tuples
        print_palette: True → muted ink-friendly palette (exports/printing)

    Returns:
        (bg, fg) tuple in the requested format
    """
    palette = SHIFT_COLORS_PRINT_RGB if print_palette else SHIFT_COLORS_RGB
    sc = palette.get(shift, palette["OFF"])
    bg, fg = sc["bg"], sc["fg"]
    if is_weekend and shift == "OFF":
        if print_palette:
            bg, fg = WEEKEND_OFF_PRINT_BG_RGB, WEEKEND_OFF_PRINT_FG_RGB
        else:
            bg, fg = WEEKEND_OFF_BG_RGB, WEEKEND_OFF_FG_RGB
    if fmt == "rgb":
        return bg, fg
    return ('{:02X}{:02X}{:02X}'.format(*bg),
            '{:02X}{:02X}{:02X}'.format(*fg))
