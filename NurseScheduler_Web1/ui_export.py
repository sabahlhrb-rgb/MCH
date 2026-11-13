"""
ui_export.py — Export / Print Helper Module
Maternity & Children Hospital – Hafr Al Batin Health Cluster
Standalone functions for exporting schedule to Excel (.xlsx), PDF, HTML, and Image
"""

import json
import os
import calendar
import webbrowser
from datetime import date
from pathlib import Path
from database import DepartmentDB
from constants import (
    SHIFT_COLORS_RGB, MONTH_NAMES, DAY_ABBR, WEEKEND_DAYS_SET,
    HEADER_BG_RGB, SUMMARY_BG_RGB, get_cell_colors
)

try:
    from schedule_engine import SHIFT_CONFIGS, OFF
except ImportError:
    SHIFT_CONFIGS = {
        "12": {"shifts": ["D", "N"], "labels": {"D": "Day", "N": "Night"}},
        "8":  {"shifts": ["M", "E", "N"], "labels": {"M": "Morning", "E": "Evening", "N": "Night"}},
        "6":  {"shifts": ["M1", "M2", "E", "N"], "labels": {"M1": "Morning-1", "M2": "Morning-2", "E": "Evening", "N": "Night"}},
    }
    OFF = "OFF"


def _get_export_dir():
    """Get export directory (same folder as database)."""
    from database import get_app_data_dir
    export_dir = get_app_data_dir() / "exports"
    export_dir.mkdir(exist_ok=True)
    return export_dir


def _get_logo_path():
    """Get path to hospital logo if it exists (asset dirs incl. the
    PyInstaller one-file bundle, then cwd as legacy fallback).
    Headless-safe: works without the desktop UI stack (web version)."""
    try:
        from ui_theme import get_asset_dirs
        asset_dirs = get_asset_dirs()
    except Exception:
        asset_dirs = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "assets")]
    for name in ["cluster_logo.png", "logo.png"]:
        for assets in asset_dirs:
            p = Path(assets) / name
            if p.exists():
                return str(p)
        p = Path(os.getcwd()) / name
        if p.exists():
            return str(p)
    return None


def _get_leave_map(db, year, month):
    """{(emp_id, day): leave-display dict} — leave days show their TYPE code
    and color in all exports instead of a plain OFF cell."""
    from datetime import date as _date
    from constants import get_leave_display
    m = {}
    try:
        for c in db.get_constraints("mandatory"):
            if c["constraint_type"] != "leave":
                continue
            p = c["params"]
            eid = p.get("employee_id")
            if eid is None:
                continue
            try:
                sd = _date.fromisoformat(p["start_date"])
                ed = _date.fromisoformat(p["end_date"])
            except (ValueError, KeyError, TypeError):
                continue
            ld = get_leave_display(p.get("leave_type") or "Other")
            cur = sd
            while cur <= ed:
                if cur.year == year and cur.month == month:
                    m[(eid, cur.day)] = ld
                cur = _date.fromordinal(cur.toordinal() + 1)
    except Exception:
        pass
    return m


def _get_so_map(db, year, month):
    """{emp_id: set(days)} — staff-requested OFFs (forced_shift constraints
    with shifts=["OFF"]); rendered as "SO" in all exports so employee
    requests are distinguishable from scheduler-assigned OFFs."""
    from datetime import date as _date
    m = {}
    try:
        for c in db.get_constraints("mandatory"):
            if c["constraint_type"] != "forced_shift":
                continue
            p = c["params"]
            if set(p.get("shifts", []) or []) != {"OFF"}:
                continue
            so_days = set()
            dates_list = p.get("dates", []) or []
            if dates_list:
                for ds in dates_list:
                    try:
                        dd = _date.fromisoformat(ds)
                        if dd.year == year and dd.month == month:
                            so_days.add(dd.day)
                    except (ValueError, TypeError):
                        pass
            else:
                so_days = {int(d) for d in (p.get("days", []) or [])
                           if str(d).isdigit()}
            for eid in (p.get("employee_ids", []) or []):
                m.setdefault(eid, set()).update(so_days)
    except Exception:
        pass
    return m


def _shade_hex(hex6, factor=0.92):
    """Darken a 6-char hex color (no #) — used to band weekend columns
    down the whole exported table, not just the header."""
    h = hex6.lstrip("#")
    return "".join(f"{max(0, min(255, int(int(h[i:i + 2], 16) * factor))):02X}"
                   for i in (0, 2, 4))


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _count_shift_staff(schedule_data, display_order, day, shift_code):
    """Count how many employees are assigned to a specific shift on a given day."""
    return sum(
        1 for eid_str in display_order
        if schedule_data.get(eid_str, {}).get(str(day)) == shift_code
    )


def _get_dept_info(db):
    """Get department information from database."""
    info = db.get_department_info()
    return {
        "name": info.get("name", ""),
        "head_nurse": info.get("head_nurse", ""),
        "nursing_director": info.get("nursing_director", ""),
    }


def _get_export_data(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                     start_day=None, end_day=None):
    """
    Common data preparation shared by all export formats.
    Returns a dict with all fields needed for rendering the schedule.

    Eliminates repeated data-loading across Excel, PDF/HTML, and Image exports.
    FIX #1: display_order is filtered to only include IDs present in emp_map
    to prevent ghost employee counts in summary rows.

    Args:
        db: DepartmentDB instance
        year: Year (int)
        month: Month (1-12, int)
        schedule_data: Dict of employee schedules
        shift_codes: List of shift codes (e.g., ["M", "E", "N"])
        shift_labels: Dict mapping shift codes to display labels
        work_hours: Work hours setting (e.g., "8", "12")
        start_day: First day of schedule range (default=1)
        end_day: Last day of schedule range (default=last day of month)

    Returns:
        dict: Export data with keys: num_days, month_name, dept, employees, emp_map,
              display_order, shifts_text, weekend_flags, day_names, min_staff,
              active_days, start_day, end_day
    """
    max_days = calendar.monthrange(year, month)[1]
    sd = max(1, min(start_day or 1, max_days))
    ed = max(sd, min(end_day or max_days, max_days))
    num_days = ed - sd + 1
    active_days = list(range(sd, ed + 1))

    month_name = MONTH_NAMES[month - 1]
    dept = _get_dept_info(db)
    employees = db.get_all_employees()
    emp_map = {e["id"]: e for e in employees}

    # FIX #1: Filter display_order — only keep IDs that exist in emp_map
    # Prevents ghost employees from inflating summary row counts
    raw_order = list(schedule_data.keys())
    display_order = [eid_str for eid_str in raw_order if int(eid_str) in emp_map]

    shifts_text = " / ".join(shift_labels.values())

    # Pre-compute weekend flags for active days only
    weekend_flags = {}
    day_names = {}
    for d in active_days:
        dt = date(year, month, d)
        weekend_flags[d] = dt.weekday() in WEEKEND_DAYS_SET
        day_names[d] = DAY_ABBR[dt.weekday()]

    # Pre-compute min staff per shift
    min_staff = {}
    for s in shift_codes:
        min_staff[s] = int(db.get_setting(f"staff_{s}_min", "1") or "1")

    return {
        "num_days": num_days,
        "month_name": month_name,
        "dept": dept,
        "employees": employees,
        "emp_map": emp_map,
        "display_order": display_order,
        "shifts_text": shifts_text,
        "weekend_flags": weekend_flags,
        "day_names": day_names,
        "min_staff": min_staff,
        "active_days": active_days,
        "start_day": sd,
        "end_day": ed,
    }


# ──────────────────────────────────────────
#  EXCEL EXPORT
# ──────────────────────────────────────────

def do_excel_export(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                    start_day=None, end_day=None):
    """
    Generate Excel (.xlsx) schedule with colored cells, summary rows, and signatures.

    Args:
        db: DepartmentDB instance
        year: Year (int)
        month: Month (1-12, int)
        schedule_data: Dict of employee schedules
        shift_codes: List of shift codes
        shift_labels: Dict mapping shift codes to labels
        work_hours: Work hours setting
        start_day: First day of range (default=1)
        end_day: Last day of range (default=last day of month)

    Returns:
        Path: Filepath to the generated Excel file
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, Protection
    from openpyxl.utils import get_column_letter
    from datetime import datetime
    from constants import (DOC_FORM_NO, DOC_VERSION, hijri_month_label,
                           LEAVE_CODES, LEAVE_BG, LEAVE_FG)

    ed = _get_export_data(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                          start_day=start_day, end_day=end_day)
    num_days, month_name, dept = ed["num_days"], ed["month_name"], ed["dept"]
    emp_map, display_order = ed["emp_map"], ed["display_order"]
    weekend_flags, day_names = ed["weekend_flags"], ed["day_names"]
    active_days = ed["active_days"]

    wb = Workbook()
    ws = wb.active
    ws.title = f"{month_name} {year}"

    # Two totals columns (Work / Nights) after the day columns.
    # Day columns are indexed SEQUENTIALLY from the range start so partial
    # ranges (start_day > 1) can never collide with the totals columns.
    total_cols = 2
    last_col = num_days + 2 + total_cols
    _sd0 = active_days[0]

    def day_col(d):
        return 3 + (d - _sd0)

    thin_border = Border(
        left=Side(style='thin', color='CCCCCC'),
        right=Side(style='thin', color='CCCCCC'),
        top=Side(style='thin', color='CCCCCC'),
        bottom=Side(style='thin', color='CCCCCC')
    )

    # ── Row 1: Hospital Name (bilingual) ──
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    ws['A1'] = ("مستشفى الولادة والأطفال — تجمع حفر الباطن الصحي   ·   "
                "Maternity & Children Hospital – Hafr Al Batin Health Cluster")
    ws['A1'].font = Font(name='Tahoma', size=13, bold=True, color='1E3A5F')
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 28

    # ── Row 2: Document title + Month (Gregorian + Hijri) ──
    hijri = hijri_month_label(year, month, calendar.monthrange(year, month)[1])
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
    ws['A2'] = (f"جدول المناوبات الشهري — Monthly Duty Roster   ·   {dept['name']}   ·   "
                f"{month_name} {year}  ({hijri})")
    ws['A2'].font = Font(name='Tahoma', size=11, bold=True, color='1E3A5F')
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 24

    # ── Row 3: Shift mode + staffing stats ──
    shifts_text = " / ".join(shift_labels.values())
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=last_col)
    ws['A3'] = (f"Shift Mode: {work_hours}h ({shifts_text})   ·   "
                f"Staff: {len(display_order)}   ·   "
                f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    ws['A3'].font = Font(name='Tahoma', size=9, color='64748B')
    ws['A3'].alignment = Alignment(horizontal='center')
    ws.row_dimensions[3].height = 18

    # ── Row 4: Empty spacer ──
    ws.row_dimensions[4].height = 6

    # ── Row 5: Header (Code | Name | 1..N) ──
    hdr_row = 5
    hdr_fill = PatternFill('solid', fgColor='1E3A5F')
    hdr_font = Font(name='Arial', size=9, bold=True, color='FFFFFF')
    hdr_align = Alignment(horizontal='center', vertical='center', wrap_text=True)

    ws.cell(row=hdr_row, column=1, value="Code").font = hdr_font
    ws.cell(row=hdr_row, column=1).fill = hdr_fill
    ws.cell(row=hdr_row, column=1).alignment = hdr_align
    ws.cell(row=hdr_row, column=1).border = thin_border

    ws.cell(row=hdr_row, column=2, value="Employee Name").font = hdr_font
    ws.cell(row=hdr_row, column=2).fill = hdr_fill
    ws.cell(row=hdr_row, column=2).alignment = Alignment(horizontal='left', vertical='center')
    ws.cell(row=hdr_row, column=2).border = thin_border

    for d in active_days:
        col = day_col(d)
        cell = ws.cell(row=hdr_row, column=col, value=f"{d}\n{day_names[d]}")
        cell.font = hdr_font
        cell.alignment = hdr_align
        cell.border = thin_border
        if weekend_flags[d]:
            cell.fill = PatternFill('solid', fgColor='475569')  # calm slate
        else:
            cell.fill = hdr_fill

    # Totals headers (Work days / Nights per nurse)
    for off, label in ((1, "Work"), (2, "Nights")):
        cell = ws.cell(row=hdr_row, column=num_days + 2 + off, value=label)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = hdr_align
        cell.border = thin_border

    ws.row_dimensions[hdr_row].height = 32

    # Column widths
    ws.column_dimensions['A'].width = 6
    ws.column_dimensions['B'].width = 24
    for d in active_days:
        ws.column_dimensions[get_column_letter(day_col(d))].width = 5.5
    ws.column_dimensions[get_column_letter(num_days + 3)].width = 6.5
    ws.column_dimensions[get_column_letter(num_days + 4)].width = 6.5

    # ── Employee Rows ──
    data_start_row = hdr_row + 1
    leave_map = _get_leave_map(db, year, month)
    so_map = _get_so_map(db, year, month)
    from constants import SO_CODE, SO_PRINT_BG_RGB, SO_PRINT_FG_RGB
    so_bg_hex = '{:02X}{:02X}{:02X}'.format(*SO_PRINT_BG_RGB)
    so_fg_hex = '{:02X}{:02X}{:02X}'.format(*SO_PRINT_FG_RGB)

    for idx, eid_str in enumerate(display_order):
        emp_id = int(eid_str)
        emp = emp_map[emp_id]
        row = data_start_row + idx
        emp_schedule = schedule_data.get(eid_str, {})
        is_even = idx % 2 == 0
        row_fill = PatternFill('solid', fgColor='F8FAFC' if is_even else 'FFFFFF')

        # Code
        code_cell = ws.cell(row=row, column=1, value=emp.get("code", ""))
        code_cell.font = Font(name='Arial', size=9, bold=True, color='2563EB')
        code_cell.alignment = Alignment(horizontal='center', vertical='center')
        code_cell.fill = row_fill
        code_cell.border = thin_border

        # Name
        name_cell = ws.cell(row=row, column=2, value=emp["name"])
        name_cell.font = Font(name='Arial', size=9)
        name_cell.alignment = Alignment(horizontal='left', vertical='center')
        name_cell.fill = row_fill
        name_cell.border = thin_border

        # Days (muted print palette; leave gold with type code; SO cyan;
        # weekend columns banded slightly darker all the way down)
        for d in active_days:
            col = day_col(d)
            shift = emp_schedule.get(str(d), OFF)

            bg_hex, fg_hex = get_cell_colors(shift, weekend_flags[d], fmt="hex",
                                             print_palette=True)
            text = shift if shift != OFF else "O"
            ld = leave_map.get((emp_id, d))
            if ld and shift == OFF:
                text = ld["code"]
                bg_hex = ld["bg"].lstrip("#")
                fg_hex = ld["fg"].lstrip("#")
            elif shift == OFF and d in so_map.get(emp_id, set()):
                text = SO_CODE
                bg_hex, fg_hex = so_bg_hex, so_fg_hex
            elif weekend_flags[d]:
                # extend the Fri/Sat header band through the body cells
                bg_hex = _shade_hex(bg_hex)

            cell = ws.cell(row=row, column=col, value=text)
            cell.font = Font(name='Arial', size=8, bold=True, color=fg_hex)
            cell.fill = PatternFill('solid', fgColor=bg_hex)
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.border = thin_border
            cell.protection = Protection(locked=False)  # editable shift cells

        # Totals: work days & nights for this nurse
        work_total = sum(1 for d in active_days
                         if emp_schedule.get(str(d), OFF) != OFF)
        night_total = sum(1 for d in active_days
                          if emp_schedule.get(str(d), OFF) == "N")
        for off, val in ((1, work_total), (2, night_total)):
            cell = ws.cell(row=row, column=num_days + 2 + off, value=val)
            cell.font = Font(name='Arial', size=8, bold=True, color='1E3A5F')
            cell.fill = row_fill
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.border = thin_border

        ws.row_dimensions[row].height = 20

    # ── Summary Rows ──
    summary_start = data_start_row + len(display_order)
    summary_fill = PatternFill('solid', fgColor='1E293B')
    summary_font = Font(name='Arial', size=8, bold=True, color='FBBF24')

    for s_idx, shift_code in enumerate(shift_codes):
        row = summary_start + s_idx

        label_cell = ws.cell(row=row, column=1, value="")
        label_cell.fill = summary_fill
        label_cell.border = thin_border

        name_cell = ws.cell(row=row, column=2, value=f"{shift_code} Staff")
        name_cell.font = summary_font
        name_cell.fill = summary_fill
        name_cell.alignment = Alignment(horizontal='left', vertical='center')
        name_cell.border = thin_border

        shift_min = ed["min_staff"].get(shift_code, 1)

        for d in active_days:
            col = day_col(d)
            count = _count_shift_staff(schedule_data, display_order, d, shift_code)
            cell = ws.cell(row=row, column=col, value=count)
            cell.fill = summary_fill
            cell.border = thin_border
            cell.alignment = Alignment(horizontal='center', vertical='center')
            if count < shift_min:
                cell.font = Font(name='Arial', size=8, bold=True, color='FCA5A5')
            else:
                cell.font = summary_font

        ws.row_dimensions[row].height = 18

    # ── Legend (وسيلة الإيضاح) ──
    legend_row = summary_start + len(shift_codes) + 1
    ws.row_dimensions[legend_row].height = 8  # spacer
    legend_row += 1
    shift_legend = "   ·   ".join(f"{code} = {label}" for code, label in shift_labels.items())
    leave_legend = "   ·   ".join(f"{c} = {t}" for t, c in list(LEAVE_CODES.items())[:6])
    leave_legend2 = "   ·   ".join(f"{c} = {t}" for t, c in list(LEAVE_CODES.items())[6:])
    ws.merge_cells(start_row=legend_row, start_column=1, end_row=legend_row, end_column=last_col)
    ws.cell(row=legend_row, column=1,
            value=(f"Legend:   {shift_legend}   ·   O = Day Off   ·   "
                   f"SO = Staff-requested OFF (أوف بطلب الموظف/الموظفة)")).font = Font(
        name='Tahoma', size=8, color='475569')
    legend_row += 1
    ws.merge_cells(start_row=legend_row, start_column=1, end_row=legend_row, end_column=last_col)
    gold_cell = ws.cell(row=legend_row, column=1,
                        value=f"Leaves (gold):   {leave_legend}")
    gold_cell.font = Font(name='Tahoma', size=8, bold=True, color=LEAVE_FG.lstrip('#'))
    gold_cell.fill = PatternFill('solid', fgColor=LEAVE_BG.lstrip('#'))
    legend_row += 1
    ws.merge_cells(start_row=legend_row, start_column=1, end_row=legend_row, end_column=last_col)
    gold_cell2 = ws.cell(row=legend_row, column=1, value=f"                {leave_legend2}")
    gold_cell2.font = Font(name='Tahoma', size=8, bold=True, color=LEAVE_FG.lstrip('#'))
    gold_cell2.fill = PatternFill('solid', fgColor=LEAVE_BG.lstrip('#'))

    # ── Approval Section — Supervisor slot is OPTIONAL (Department Info) ──
    show_sup = db.get_setting("supervisor_show", "0") == "1"
    sup_name = db.get_setting("supervisor_name", "") or ""
    slot_labels = [f"Prepared by — Head Nurse:\n{dept['head_nurse']}"]
    if show_sup:
        slot_labels.append(f"Reviewed by — Supervisor:\n{sup_name}")
    slot_labels.append(f"Approved by — Nursing Director:\n{dept['nursing_director']}")
    n_slots = len(slot_labels)
    seg = max(3, last_col // n_slots)
    slots = []
    for i, label in enumerate(slot_labels):
        c1 = i * seg + 1
        c2 = last_col if i == n_slots - 1 else (i + 1) * seg
        slots.append((c1, c2, label))
    sig_row = legend_row + 2
    for c1, c2, label in slots:
        ws.merge_cells(start_row=sig_row, start_column=c1, end_row=sig_row + 1, end_column=c2)
        cell = ws.cell(row=sig_row, column=c1, value=label)
        cell.font = Font(name='Tahoma', size=9, bold=True, color='1E293B')
        cell.alignment = Alignment(horizontal='center', vertical='top', wrap_text=True)
    sig_row += 2
    for c1, c2, _ in slots:
        ws.merge_cells(start_row=sig_row, start_column=c1, end_row=sig_row, end_column=c2)
        cell = ws.cell(row=sig_row, column=c1,
                       value="Signature: ______________    Date: ____/____/______")
        cell.font = Font(name='Tahoma', size=8, color='64748B')
        cell.alignment = Alignment(horizontal='center')

    # ── Document control footer ──
    doc_row = sig_row + 2
    ws.merge_cells(start_row=doc_row, start_column=1, end_row=doc_row, end_column=last_col)
    doc_cell = ws.cell(
        row=doc_row, column=1,
        value=(f"Form No: {DOC_FORM_NO}   ·   Version: {DOC_VERSION}   ·   "
               f"Issued: {datetime.now().strftime('%Y-%m-%d')}   ·   "
               f"NurseScheduler — {dept['name']}"))
    doc_cell.font = Font(name='Tahoma', size=7.5, color='94A3B8')
    doc_cell.alignment = Alignment(horizontal='center')

    # ── Print settings ──
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_area = f'A1:{get_column_letter(last_col)}{doc_row}'
    ws.print_title_rows = f'{hdr_row}:{hdr_row}'   # repeat header on each page
    ws.oddFooter.center.text = f"{DOC_FORM_NO} · v{DOC_VERSION} · Page &P of &N"
    ws.oddFooter.center.size = 7

    # Freeze panes (Code + Name columns + header row stay visible)
    ws.freeze_panes = ws.cell(row=hdr_row + 1, column=3)

    # Sheet protection: everything locked except the day cells
    ws.protection.sheet = True
    ws.protection.formatCells = False

    # ── Statistics sheet ──
    ws2 = wb.create_sheet("Statistics")
    ws2.sheet_view.rightToLeft = False
    stats_headers = ["Code", "Name", "Work Days", "Nights", "Evenings",
                     "Weekend Shifts", "Days Off", "Leave Days"]
    for c, h in enumerate(stats_headers, 1):
        cell = ws2.cell(row=1, column=c, value=h)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal='center')
    weekend_days = [d for d in active_days if weekend_flags[d]]
    for idx, eid_str in enumerate(display_order, start=2):
        emp_id = int(eid_str)
        emp = emp_map[emp_id]
        sched = schedule_data.get(eid_str, {})
        vals = [
            emp.get("code", ""), emp["name"],
            sum(1 for d in active_days if sched.get(str(d), OFF) != OFF),
            sum(1 for d in active_days if sched.get(str(d), OFF) == "N"),
            sum(1 for d in active_days if sched.get(str(d), OFF) == "E"),
            sum(1 for d in weekend_days if sched.get(str(d), OFF) != OFF),
            sum(1 for d in active_days if sched.get(str(d), OFF) == OFF),
            sum(1 for d in active_days if (emp_id, d) in leave_map),
        ]
        for c, v in enumerate(vals, 1):
            cell = ws2.cell(row=idx, column=c, value=v)
            cell.alignment = Alignment(horizontal='center' if c != 2 else 'left')
            if idx % 2 == 0:
                cell.fill = PatternFill('solid', fgColor='F8FAFC')
    ws2.column_dimensions['B'].width = 26
    for col in ('A', 'C', 'D', 'E', 'F', 'G', 'H'):
        ws2.column_dimensions[col].width = 13
    ws2.freeze_panes = "A2"

    # Save
    export_dir = _get_export_dir()
    filename = f"Schedule_{month_name}_{year}.xlsx"
    filepath = export_dir / filename
    wb.save(str(filepath))

    return filepath


# ──────────────────────────────────────────
#  PDF EXPORT
# ──────────────────────────────────────────

def do_pdf_export(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                  start_day=None, end_day=None):
    """
    Export schedule as PDF.
    Tries reportlab first, falls back to HTML (printable from browser).

    FIX #8: Cleaned up dead/confusing fallback code path.

    Args:
        db: DepartmentDB instance
        year: Year (int)
        month: Month (1-12, int)
        schedule_data: Dict of employee schedules
        shift_codes: List of shift codes
        shift_labels: Dict mapping shift codes to labels
        work_hours: Work hours setting
        start_day: First day of range (default=1)
        end_day: Last day of range (default=last day of month)

    Returns:
        Path: Filepath to the generated PDF file
    """
    # Try reportlab first
    try:
        return _export_pdf_with_reportlab(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                                          start_day=start_day, end_day=end_day)
    except ImportError:
        pass  # reportlab not installed — fall through to HTML
    except Exception:
        pass  # reportlab failed — fall through to HTML

    # Fallback: generate HTML and open in browser for Print-to-PDF
    try:
        html_path = _generate_pdf_html(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                                        start_day=start_day, end_day=end_day)
        webbrowser.open(f"file:///{html_path}")
        return html_path
    except Exception as e:
        raise Exception(f"PDF export failed: {str(e)[:80]}")


def _export_pdf_with_reportlab(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                               start_day=None, end_day=None):
    """
    Generate actual PDF using reportlab library.

    FIX #2: Per-cell shift coloring (was missing — all shifts looked identical)
    FIX #3: Signature section added (was completely missing)
    FIX #4: Explicit column widths (was auto-calculated, risked overflow)
    """
    try:
        from reportlab.lib.pagesizes import A3, landscape
        from reportlab.pdfgen import canvas
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch, cm
    except ImportError:
        raise ImportError("reportlab not installed. Falling back to HTML export.")

    ed = _get_export_data(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                          start_day=start_day, end_day=end_day)
    num_days, month_name, dept = ed["num_days"], ed["month_name"], ed["dept"]
    emp_map, display_order = ed["emp_map"], ed["display_order"]
    weekend_flags = ed["weekend_flags"]
    active_days = ed["active_days"]

    export_dir = _get_export_dir()
    pdf_path = export_dir / f"Schedule_{month_name}_{year}.pdf"

    page_w, page_h = landscape(A3)
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=landscape(A3),
        rightMargin=0.5*cm,
        leftMargin=0.5*cm,
        topMargin=0.5*cm,
        bottomMargin=0.5*cm
    )

    story = []
    styles = getSampleStyleSheet()

    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=14,
        textColor=colors.HexColor('#1E3A5F'),
        spaceAfter=6,
        alignment=1,
    )
    story.append(Paragraph("Maternity &amp; Children Hospital - Hafr Al Batin Health Cluster", title_style))

    # Subtitle
    subtitle_style = ParagraphStyle(
        'CustomSubtitle',
        parent=styles['Heading2'],
        fontSize=12,
        textColor=colors.HexColor('#2563EB'),
        spaceAfter=3,
        alignment=1,
    )
    story.append(Paragraph(f"{dept['name']} — Schedule for {month_name} {year}", subtitle_style))

    # Shift mode
    info_style = ParagraphStyle(
        'CustomInfo',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.HexColor('#64748B'),
        spaceAfter=12,
        alignment=1,
    )
    story.append(Paragraph(f"Shift Mode: {work_hours}h ({ed['shifts_text']})", info_style))

    # Build table data
    table_data = []

    # Header row
    header_row = ["Code", "Employee Name"]
    for d in active_days:
        header_row.append(f"{d}\n{ed['day_names'][d]}")
    table_data.append(header_row)

    # Employee rows — track shift positions for per-cell coloring
    leave_map = _get_leave_map(db, year, month)
    shift_cells = []  # list of (row_idx, col_idx, shift_code, is_weekend, leave_display)
    for eid_str in display_order:
        emp_id = int(eid_str)
        emp = emp_map[emp_id]
        emp_schedule = schedule_data.get(eid_str, {})
        row = [emp.get("code", ""), emp["name"]]

        row_idx = len(table_data)
        for d in active_days:
            shift = emp_schedule.get(str(d), OFF)
            ld = leave_map.get((emp_id, d)) if shift == OFF else None
            text = ld["code"] if ld else (shift if shift != OFF else "O")
            row.append(text)
            shift_cells.append((row_idx, d + 1, shift, weekend_flags[d], ld))

        table_data.append(row)

    # Summary rows
    summary_start_idx = len(table_data)
    for shift_code in shift_codes:
        row = ["", f"{shift_code} Staff"]
        for d in active_days:
            count = _count_shift_staff(schedule_data, display_order, d, shift_code)
            row.append(str(count))
        table_data.append(row)

    # FIX #4: Explicit column widths
    usable_w = page_w - 1.0 * cm  # minus margins
    code_w = 28
    name_w = 100
    day_w = (usable_w - code_w - name_w) / num_days
    col_widths = [code_w, name_w] + [day_w] * num_days

    # Create table with explicit widths
    table = Table(table_data, colWidths=col_widths, repeatRows=1)

    # Base table style
    table_style_cmds = [
        # Header styling
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1E3A5F')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 7),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
        ('TOPPADDING', (0, 0), (-1, 0), 4),

        # Row styling defaults
        ('ALIGN', (0, 1), (-1, -1), 'CENTER'),
        ('FONTSIZE', (0, 1), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),

        # Alternating row backgrounds for employee rows
        ('ROWBACKGROUNDS', (0, 1), (-1, summary_start_idx - 1),
         [colors.HexColor('#F8FAFC'), colors.white]),

        # Summary rows styling
        ('BACKGROUND', (0, summary_start_idx), (-1, -1), colors.HexColor('#1E293B')),
        ('TEXTCOLOR', (0, summary_start_idx), (-1, -1), colors.HexColor('#FBBF24')),
        ('FONTNAME', (0, summary_start_idx), (-1, -1), 'Helvetica-Bold'),

        # All borders
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
    ]

    # Weekend header columns
    for d in active_days:
        if weekend_flags[d]:
            col_idx = d + 1
            table_style_cmds.append(
                ('BACKGROUND', (col_idx, 0), (col_idx, 0), colors.HexColor('#7C2D12'))
            )

    # FIX #2: Per-cell shift coloring (leave days use their type color)
    for row_idx, col_idx, shift, is_weekend, ld in shift_cells:
        bg_hex, fg_hex = get_cell_colors(shift, is_weekend, fmt="hex",
                                         print_palette=True)
        if ld:
            bg_hex = ld["bg"].lstrip("#")
            fg_hex = ld["fg"].lstrip("#")
        table_style_cmds.append(
            ('BACKGROUND', (col_idx, row_idx), (col_idx, row_idx),
             colors.HexColor(f'#{bg_hex}'))
        )
        table_style_cmds.append(
            ('TEXTCOLOR', (col_idx, row_idx), (col_idx, row_idx),
             colors.HexColor(f'#{fg_hex}'))
        )
        table_style_cmds.append(
            ('FONTNAME', (col_idx, row_idx), (col_idx, row_idx), 'Helvetica-Bold')
        )

    table.setStyle(TableStyle(table_style_cmds))
    story.append(table)

    # FIX #3: Signature section
    story.append(Spacer(1, 20))

    sig_style = ParagraphStyle(
        'SigLabel',
        parent=styles['Normal'],
        fontSize=10,
        textColor=colors.HexColor('#1E293B'),
        leading=14,
    )
    sig_line_style = ParagraphStyle(
        'SigLine',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.HexColor('#64748B'),
        leading=14,
    )

    sig_data = [
        [
            Paragraph(f"<b>Head Nurse: {dept['head_nurse']}</b>", sig_style),
            "",
            Paragraph(f"<b>Nursing Director: {dept['nursing_director']}</b>", sig_style),
        ],
        [
            Paragraph("Signature: ____________________", sig_line_style),
            "",
            Paragraph("Signature: ____________________", sig_line_style),
        ],
        [
            Paragraph("Date: ____/____/________", sig_line_style),
            "",
            Paragraph("Date: ____/____/________", sig_line_style),
        ],
    ]
    sig_table = Table(sig_data, colWidths=[usable_w * 0.4, usable_w * 0.2, usable_w * 0.4])
    sig_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(sig_table)

    # Footer
    story.append(Spacer(1, 10))
    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#999999'),
        alignment=1,
    )
    story.append(Paragraph(
        f"Generated: {date.today().strftime('%Y-%m-%d')} | "
        f"Maternity &amp; Children Hospital — Hafr Al Batin Health Cluster",
        footer_style
    ))

    # Build PDF
    doc.build(story)
    return pdf_path


def _generate_pdf_html(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                       start_day=None, end_day=None):
    """
    Generate a styled HTML file for schedule that can be printed to PDF.
    Supports automatic pagination: when employees > 25, each page holds up to
    25 employees with page numbers, repeated headers, and summary/signature
    only on the last page.

    Args:
        db: DepartmentDB instance
        year: Year (int)
        month: Month (1-12, int)
        schedule_data: Dict of employee schedules
        shift_codes: List of shift codes
        shift_labels: Dict mapping shift codes to labels
        work_hours: Work hours setting
        start_day: First day of range (default=1)
        end_day: Last day of range (default=last day of month)

    Returns:
        Path: Filepath to the generated HTML file
    """
    ROWS_PER_PAGE = 25

    ed = _get_export_data(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                          start_day=start_day, end_day=end_day)
    num_days, month_name, dept = ed["num_days"], ed["month_name"], ed["dept"]
    emp_map, display_order = ed["emp_map"], ed["display_order"]
    weekend_flags, day_names = ed["weekend_flags"], ed["day_names"]
    active_days = ed["active_days"]

    # Filter valid employees
    valid_order = [eid for eid in display_order if int(eid) in emp_map]
    total_employees = len(valid_order)

    # Split into page chunks
    page_chunks = []
    for i in range(0, total_employees, ROWS_PER_PAGE):
        page_chunks.append(valid_order[i:i + ROWS_PER_PAGE])
    if not page_chunks:
        page_chunks = [[]]
    total_pages = len(page_chunks)

    export_dir = _get_export_dir()
    html_path = export_dir / f"Schedule_{month_name}_{year}.html"

    # Helper function to convert RGB to hex
    def rgb_to_hex(rgb):
        return '#{:02X}{:02X}{:02X}'.format(*rgb)

    # ── Build CSS ──
    css = f"""
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: 'Segoe UI', Arial, sans-serif;
            background-color: #f9fafb;
            padding: 0;
        }}

        /* ── Page container — one per printed page ── */
        .page {{
            position: relative;
            background-color: white;
            padding: 18px 20px 40px 20px;
            margin: 0;
            min-height: 100vh;
        }}

        .page + .page {{
            page-break-before: always;
        }}

        /* ── Header ── */
        .header {{
            text-align: center;
            margin-bottom: 12px;
            border-bottom: 3px solid {rgb_to_hex((30, 58, 95))};
            padding-bottom: 10px;
        }}

        .hospital-name {{
            font-size: 18px;
            font-weight: bold;
            color: {rgb_to_hex((30, 58, 95))};
            margin-bottom: 3px;
        }}

        .cluster-name {{
            font-size: 12px;
            color: {rgb_to_hex((100, 116, 139))};
            margin-bottom: 6px;
        }}

        .dept-month {{
            font-size: 15px;
            font-weight: bold;
            color: {rgb_to_hex((37, 99, 235))};
            margin-bottom: 3px;
        }}

        .shift-mode {{
            font-size: 11px;
            color: {rgb_to_hex((100, 116, 139))};
        }}

        /* ── Schedule table ── */
        .schedule-table {{
            width: 100%;
            border-collapse: collapse;
            margin: 10px 0;
            font-size: 10px;
        }}

        .schedule-table thead th {{
            background-color: {rgb_to_hex((30, 58, 95))};
            color: white;
            padding: 7px 3px;
            text-align: center;
            font-weight: bold;
            font-size: 9px;
            border: 1px solid #999;
        }}

        .schedule-table thead th.weekend {{
            background-color: {rgb_to_hex((124, 45, 18))};
        }}

        .schedule-table tbody tr:nth-child(odd) {{
            background-color: {rgb_to_hex((248, 250, 252))};
        }}

        .schedule-table tbody tr:nth-child(even) {{
            background-color: white;
        }}

        .schedule-table td {{
            padding: 5px 2px;
            text-align: center;
            border: 1px solid #ddd;
            height: 22px;
            font-size: 10px;
        }}

        .schedule-table td.code {{
            text-align: center;
            font-weight: bold;
            color: {rgb_to_hex((37, 99, 235))};
            width: 35px;
            min-width: 35px;
        }}

        .schedule-table td.emp-name {{
            text-align: left;
            padding-left: 6px;
            width: 160px;
            min-width: 160px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 160px;
        }}

        .schedule-table td.shift {{
            font-weight: bold;
            vertical-align: middle;
        }}

        .schedule-table td.summary-row {{
            background-color: {rgb_to_hex((30, 41, 59))};
            color: {rgb_to_hex((251, 191, 36))};
            font-weight: bold;
        }}

        /* ── Row number column ── */
        .schedule-table td.row-num {{
            width: 25px;
            min-width: 25px;
            color: {rgb_to_hex((100, 116, 139))};
            font-size: 9px;
            font-weight: normal;
        }}

        .schedule-table thead th.row-num-hdr {{
            width: 25px;
            min-width: 25px;
        }}

        /* ── Shift color coding ── */
"""
    from constants import SHIFT_COLORS_PRINT_RGB as _PRINT_COLORS
    for shift_code, colors_dict in _PRINT_COLORS.items():
        bg_hex = rgb_to_hex(colors_dict["bg"])
        fg_hex = rgb_to_hex(colors_dict["fg"])
        css += f"""        .shift-{shift_code} {{
            background-color: {bg_hex} !important;
            color: {fg_hex} !important;
        }}
"""

    css += f"""
        .weekend-off {{
            background-color: {rgb_to_hex((241, 245, 249))} !important;
            color: {rgb_to_hex((100, 116, 139))} !important;
        }}

        .legend-box {{
            margin-top: 10px;
            font-size: 9.5px;
            color: #475569;
            line-height: 1.7;
        }}
        .legend-gold {{
            display: inline-block;
            background: #f1c40f;
            color: #6b4e00;
            font-weight: bold;
            padding: 1px 8px;
            margin-right: 4px;
        }}
        .doc-control {{
            margin-top: 14px;
            text-align: center;
            font-size: 8.5px;
            color: #94a3b8;
            border-top: 1px solid #e5e7eb;
            padding-top: 5px;
        }}
        td.totals {{
            font-weight: bold;
            color: {rgb_to_hex((30, 58, 95))};
            background: {rgb_to_hex((248, 250, 252))};
        }}

        /* ── Signature section ── */
        .signature-section {{
            margin-top: 20px;
            display: flex;
            justify-content: space-between;
            font-size: 11px;
        }}

        .signature-block {{
            flex: 1;
        }}

        .signature-line {{
            border-bottom: 1px solid #333;
            margin-top: 20px;
            min-width: 150px;
            display: inline-block;
        }}

        /* ── Page footer with page number ── */
        .page-footer {{
            position: absolute;
            bottom: 10px;
            left: 0;
            right: 0;
            text-align: center;
            font-size: 9px;
            color: {rgb_to_hex((100, 116, 139))};
            border-top: 1px solid #e5e7eb;
            padding-top: 6px;
            margin: 0 20px;
        }}

        /* ── A3 landscape page size for print ── */
        @page {{
            size: A3 landscape;
            margin: 8mm;
        }}

        /* ── Print styles ── */
        @media print {{
            body {{
                background-color: white;
                padding: 0;
            }}
            .page {{
                padding: 10px 12px 35px 12px;
                box-shadow: none;
                min-height: auto;
            }}
            .page + .page {{
                page-break-before: always;
            }}
            .no-print {{
                display: none !important;
            }}
        }}

        /* ── Screen-only styling ── */
        @media screen {{
            body {{
                padding: 20px;
                background-color: #e5e7eb;
            }}
            .page {{
                max-width: 1400px;
                margin: 0 auto 30px auto;
                box-shadow: 0 2px 8px rgba(0,0,0,0.15);
                border-radius: 4px;
            }}
        }}
"""

    # ── Build table header row HTML (reused on every page) ──
    thead_html = '<thead><tr><th class="row-num-hdr">#</th><th>Code</th><th>Employee Name</th>'
    for d in active_days:
        wk_cls = ' class="weekend"' if weekend_flags[d] else ""
        thead_html += f"<th{wk_cls}>{d}<br>{day_names[d]}</th>"
    thead_html += "<th>Work</th><th>Nights</th></tr></thead>"

    # ── Build page header HTML (reused on every page) ──
    from constants import (hijri_month_label, DOC_FORM_NO, DOC_VERSION,
                           LEAVE_CODES)
    from datetime import datetime as _dt
    hijri_label = hijri_month_label(year, month, calendar.monthrange(year, month)[1])
    shift_mode_text = f"{work_hours}h ({' / '.join(shift_labels.values())})"
    range_note = ""
    max_days_month = calendar.monthrange(year, month)[1]
    if num_days < max_days_month:
        range_note = f" (Day {ed['start_day']}–{ed['end_day']})"
    page_header_html = f"""
        <div class="header">
            <div class="hospital-name">مستشفى الولادة والأطفال &nbsp;·&nbsp; Maternity &amp; Children Hospital</div>
            <div class="cluster-name">تجمع حفر الباطن الصحي &nbsp;·&nbsp; Hafr Al Batin Health Cluster</div>
            <div class="dept-month">جدول المناوبات الشهري — Monthly Duty Roster &nbsp;·&nbsp; {dept['name']} &mdash; {month_name} {year} ({hijri_label}){range_note}</div>
            <div class="shift-mode">Shift Mode: {shift_mode_text} &nbsp;·&nbsp; Staff: {total_employees} &nbsp;·&nbsp; Generated: {_dt.now().strftime('%Y-%m-%d %H:%M')}</div>
        </div>
"""

    # ── Legend + document control (last page only) ──
    shift_legend = " · ".join(f"<b>{c}</b> = {l}" for c, l in shift_labels.items())
    leave_legend = " · ".join(f"<b>{c}</b> = {t}" for t, c in LEAVE_CODES.items())
    from constants import SO_CODE as _SO, SO_BG as _SO_BG, SO_FG as _SO_FG
    legend_html = f"""
        <div class="legend-box">
            Legend: &nbsp; {shift_legend} · <b>O</b> = Day Off ·
            <span style="background:{_SO_BG};color:{_SO_FG};padding:1px 6px;
            border-radius:3px;font-weight:bold">{_SO} = أوف بطلب الموظف/الموظفة</span><br>
            <span class="legend-gold">Leaves (gold)</span> {leave_legend}
        </div>
"""
    doc_control_html = f"""
        <div class="doc-control">
            Form No: {DOC_FORM_NO} &nbsp;·&nbsp; Version: {DOC_VERSION} &nbsp;·&nbsp;
            Issued: {_dt.now().strftime('%Y-%m-%d')} &nbsp;·&nbsp; NurseScheduler — {dept['name']}
        </div>
"""

    # ── Helper: build employee rows for a chunk ──
    leave_map = _get_leave_map(db, year, month)
    so_map = _get_so_map(db, year, month)

    def build_emp_rows(chunk, start_index):
        rows = ""
        for idx, eid_str in enumerate(chunk):
            emp_id = int(eid_str)
            emp = emp_map[emp_id]
            emp_schedule = schedule_data.get(eid_str, {})
            row_num = start_index + idx + 1

            rows += f'<tr><td class="row-num">{row_num}</td>'
            rows += f'<td class="code">{emp.get("code", "")}</td>'
            rows += f'<td class="emp-name">{emp["name"]}</td>'

            for d in active_days:
                shift = emp_schedule.get(str(d), OFF)
                shift_class = f"shift shift-{shift}"
                if weekend_flags[d] and shift == OFF:
                    shift_class += " weekend-off"
                text = shift if shift != OFF else "O"
                ld = leave_map.get((emp_id, d)) if shift == OFF else None
                is_so = (shift == OFF and not ld
                         and d in so_map.get(emp_id, set()))
                if ld:
                    text = ld["code"]
                    rows += (f'<td class="shift" style="background:{ld["bg"]};'
                             f'color:{ld["fg"]};font-weight:bold">{text}</td>')
                elif is_so:
                    rows += (f'<td class="shift" style="background:{_SO_BG};'
                             f'color:{_SO_FG};font-weight:bold">{_SO}</td>')
                elif weekend_flags[d]:
                    # extend the Fri/Sat header band through the body cells
                    bgh, fgh = get_cell_colors(shift, True, fmt="hex",
                                               print_palette=True)
                    rows += (f'<td class="{shift_class}" '
                             f'style="background:#{_shade_hex(bgh)};'
                             f'color:#{fgh}">{text}</td>')
                else:
                    rows += f'<td class="{shift_class}">{text}</td>'

            work_total = sum(1 for d in active_days
                             if emp_schedule.get(str(d), OFF) != OFF)
            night_total = sum(1 for d in active_days
                              if emp_schedule.get(str(d), OFF) == "N")
            rows += (f'<td class="totals">{work_total}</td>'
                     f'<td class="totals">{night_total}</td>')
            rows += "</tr>\n"
        return rows

    # ── Helper: build summary rows (only on last page) ──
    def build_summary_rows():
        rows = ""
        for shift_code in shift_codes:
            rows += '<tr><td class="row-num"></td><td class="code"></td>'
            rows += f'<td class="emp-name summary-row">{shift_code} Staff</td>'
            for d in active_days:
                count = _count_shift_staff(schedule_data, display_order, d, shift_code)
                rows += f'<td class="summary-row">{count}</td>'
            rows += '<td class="summary-row"></td><td class="summary-row"></td>'
            rows += "</tr>\n"
        return rows

    # ── Helper: approval section — Supervisor slot is OPTIONAL ──
    def build_signature():
        show_sup = db.get_setting("supervisor_show", "0") == "1"
        sup_name = db.get_setting("supervisor_name", "") or "&nbsp;"
        supervisor_block = f"""
            <div class="signature-block" style="text-align: center;">
                <div><strong>Reviewed by — Supervisor</strong></div>
                <div>{sup_name}</div>
                <div>Signature: <span class="signature-line"></span></div>
                <div>Date: ____/____/________</div>
            </div>""" if show_sup else ""
        return f"""
        <div class="signature-section">
            <div class="signature-block" style="text-align: center;">
                <div><strong>Prepared by — Head Nurse</strong></div>
                <div>{dept['head_nurse']}</div>
                <div>Signature: <span class="signature-line"></span></div>
                <div>Date: ____/____/________</div>
            </div>{supervisor_block}
            <div class="signature-block" style="text-align: center;">
                <div><strong>Approved by — NURSING DIRECTOR</strong></div>
                <div>{dept['nursing_director']}</div>
                <div>Signature: <span class="signature-line"></span></div>
                <div>Date: ____/____/________</div>
            </div>
        </div>
"""

    # ── Assemble pages ──
    body_html = ""
    for page_idx, chunk in enumerate(page_chunks):
        page_num = page_idx + 1
        is_last = (page_num == total_pages)
        start_index = page_idx * ROWS_PER_PAGE

        body_html += '<div class="page">\n'
        body_html += page_header_html

        # Page subtitle (employees range)
        if total_pages > 1:
            first_num = start_index + 1
            last_num = start_index + len(chunk)
            body_html += f'<div style="text-align:center;font-size:10px;color:#6b7280;margin-bottom:6px;">'
            body_html += f'الموظفون/الموظفات {first_num} – {last_num} من {total_employees}'
            body_html += f' &nbsp;|&nbsp; Employees {first_num}–{last_num} of {total_employees}</div>\n'

        # Table
        body_html += '<table class="schedule-table">\n'
        body_html += thead_html + "\n<tbody>\n"
        body_html += build_emp_rows(chunk, start_index)

        # Summary rows only on last page
        if is_last:
            body_html += build_summary_rows()

        body_html += "</tbody></table>\n"

        # Legend + approval + document control only on last page
        if is_last:
            body_html += legend_html
            body_html += build_signature()
            body_html += doc_control_html

        # Page footer
        if total_pages > 1:
            body_html += f'<div class="page-footer">Page {page_num} of {total_pages}'
            body_html += f' &nbsp;&mdash;&nbsp; صفحة {page_num} من {total_pages}</div>\n'

        body_html += "</div>\n"  # close .page

    # ── Assemble full HTML ──
    html_content = f"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Schedule {month_name} {year}</title>
    <style>{css}    </style>
</head>
<body>
{body_html}
</body>
</html>
"""

    # Write HTML file
    with open(str(html_path), 'w', encoding='utf-8') as f:
        f.write(html_content)

    return html_path


def do_print_html(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                  start_day=None, end_day=None):
    """
    Generate HTML file and open in browser for printing to PDF.
    This is what the "Print" button will call.

    Args:
        db: DepartmentDB instance
        year: Year (int)
        month: Month (1-12, int)
        schedule_data: Dict of employee schedules
        shift_codes: List of shift codes
        shift_labels: Dict mapping shift codes to labels
        work_hours: Work hours setting
        start_day: First day of range (default=1)
        end_day: Last day of range (default=last day of month)

    Returns:
        Path: Filepath to the generated HTML file
    """
    html_path = _generate_pdf_html(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                                    start_day=start_day, end_day=end_day)
    webbrowser.open(f"file:///{html_path}")
    return html_path


# ──────────────────────────────────────────
#  IMAGE EXPORT
# ──────────────────────────────────────────

def do_image_export(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                    start_day=None, end_day=None):
    """
    Generate high-resolution PNG image of the schedule.

    FIX #9: 2x scale for 300 DPI professional print quality
    FIX #11: Name truncation by pixel width (not char count)
    FIX #12: Removed dead for_pdf parameter

    Args:
        db: DepartmentDB instance
        year: Year (int)
        month: Month (1-12, int)
        schedule_data: Dict of employee schedules
        shift_codes: List of shift codes
        shift_labels: Dict mapping shift codes to labels
        work_hours: Work hours setting
        start_day: First day of range (default=1)
        end_day: Last day of range (default=last day of month)

    Returns:
        Path: Filepath to the generated PNG file
    """
    from PIL import Image, ImageDraw, ImageFont

    ed = _get_export_data(db, year, month, schedule_data, shift_codes, shift_labels, work_hours,
                          start_day=start_day, end_day=end_day)
    num_days, month_name, dept = ed["num_days"], ed["month_name"], ed["dept"]
    emp_map, display_order = ed["emp_map"], ed["display_order"]
    weekend_flags, day_names = ed["weekend_flags"], ed["day_names"]
    active_days = ed["active_days"]
    num_employees = len(display_order)

    # FIX #9: 2x scale factor for 300 DPI output
    S = 2
    CODE_W = 50 * S
    NAME_W = 200 * S
    DAY_W = 46 * S
    ROW_H = 28 * S
    HDR_H = 38 * S
    HEADER_SECTION_H = 100 * S
    SIGNATURE_H = 100 * S
    SUMMARY_ROW_H = 24 * S

    total_width = CODE_W + NAME_W + (num_days * DAY_W) + 20 * S
    grid_height = HDR_H + (num_employees * ROW_H) + (len(shift_codes) * SUMMARY_ROW_H)
    total_height = HEADER_SECTION_H + grid_height + SIGNATURE_H + 40 * S

    img = Image.new('RGB', (total_width, total_height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Fonts — scaled for 300 DPI
    try:
        font_bold_16 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16 * S)
        font_bold_12 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12 * S)
        font_bold_10 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 10 * S)
        font_9 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 9 * S)
        font_bold_9 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 9 * S)
        font_8 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 8 * S)
        font_bold_8 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 8 * S)
    except Exception:
        font_bold_16 = ImageFont.load_default()
        font_bold_12 = font_bold_16
        font_bold_10 = font_bold_16
        font_9 = font_bold_16
        font_bold_9 = font_bold_16
        font_8 = font_bold_16
        font_bold_8 = font_bold_16

    # ── HEADER SECTION ──
    # Logo (if exists)
    logo_path = _get_logo_path()
    logo_x_end = 10 * S
    if logo_path:
        try:
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((60 * S, 60 * S))
            # FIX #5: Use alpha mask to handle transparent logos properly
            img.paste(logo, (10 * S, 10 * S), logo)
            logo_x_end = 80 * S
        except Exception:
            pass

    # Hospital name
    draw.text((logo_x_end, 15 * S), "Maternity & Children Hospital", fill=(30, 58, 95), font=font_bold_16)
    draw.text((logo_x_end, 35 * S), "Hafr Al Batin Health Cluster", fill=(100, 116, 139), font=font_bold_10)

    # Department + Month
    draw.text((logo_x_end, 58 * S), f"{dept['name']} — {month_name} {year}",
              fill=(37, 99, 235), font=font_bold_12)

    # Shift mode
    shifts_text = " / ".join(shift_labels.values())
    draw.text((logo_x_end, 78 * S), f"Shift Mode: {work_hours}h ({shifts_text})",
              fill=(100, 116, 139), font=font_9)

    # ── GRID ──
    y_start = HEADER_SECTION_H
    x_start = 10 * S

    # Header row background
    draw.rectangle([x_start, y_start, x_start + CODE_W + NAME_W + num_days * DAY_W, y_start + HDR_H],
                   fill=HEADER_BG_RGB)

    # Header labels
    draw.text((x_start + 10 * S, y_start + 12 * S), "Code", fill=(255, 255, 255), font=font_bold_9)
    draw.text((x_start + CODE_W + 6 * S, y_start + 12 * S), "Employee Name", fill=(255, 255, 255), font=font_bold_9)

    for d in active_days:
        x = x_start + CODE_W + NAME_W + (d - 1) * DAY_W
        is_weekend = weekend_flags[d]

        if is_weekend:
            draw.rectangle([x, y_start, x + DAY_W, y_start + HDR_H], fill=(124, 45, 18))

        # Center day number
        d_text = str(d)
        d_bbox = draw.textbbox((0, 0), d_text, font=font_bold_9)
        d_tw = d_bbox[2] - d_bbox[0]
        draw.text((x + (DAY_W - d_tw) // 2, y_start + 5 * S), d_text, fill=(255, 255, 255), font=font_bold_9)

        dn_bbox = draw.textbbox((0, 0), day_names[d], font=font_8)
        dn_tw = dn_bbox[2] - dn_bbox[0]
        draw.text((x + (DAY_W - dn_tw) // 2, y_start + 20 * S), day_names[d], fill=(254, 202, 202) if is_weekend else (200, 210, 220), font=font_8)

    # ── Employee Rows ──
    y = y_start + HDR_H
    name_max_px = NAME_W - 12 * S  # FIX #11: max pixel width for name column
    leave_map = _get_leave_map(db, year, month)

    for idx, eid_str in enumerate(display_order):
        emp_id = int(eid_str)
        emp = emp_map[emp_id]
        emp_schedule = schedule_data.get(eid_str, {})
        is_even = idx % 2 == 0
        row_bg = (248, 250, 252) if is_even else (255, 255, 255)

        # Row background
        draw.rectangle([x_start, y, x_start + CODE_W + NAME_W + num_days * DAY_W, y + ROW_H], fill=row_bg)

        # Code
        draw.text((x_start + 14 * S, y + 8 * S), str(emp.get("code", "")), fill=(37, 99, 235), font=font_bold_9)

        # FIX #11: Name truncation by pixel width (not char count)
        name = emp["name"]
        name_bbox = draw.textbbox((0, 0), name, font=font_9)
        if (name_bbox[2] - name_bbox[0]) > name_max_px:
            while len(name) > 1:
                name = name[:-1]
                test_bbox = draw.textbbox((0, 0), name + "..", font=font_9)
                if (test_bbox[2] - test_bbox[0]) <= name_max_px:
                    name = name + ".."
                    break
        draw.text((x_start + CODE_W + 6 * S, y + 8 * S), name, fill=(30, 41, 59), font=font_9)

        # Days
        for d in active_days:
            x = x_start + CODE_W + NAME_W + (d - 1) * DAY_W
            shift = emp_schedule.get(str(d), OFF)

            bg, fg = get_cell_colors(shift, weekend_flags[d], fmt="rgb",
                                     print_palette=True)
            text = shift if shift != OFF else "O"
            ld = leave_map.get((emp_id, d)) if shift == OFF else None
            if ld:
                text = ld["code"]
                bg = _hex_to_rgb(ld["bg"])
                fg = _hex_to_rgb(ld["fg"])

            draw.rectangle([x, y, x + DAY_W, y + ROW_H], fill=bg)
            draw.rectangle([x, y, x + DAY_W, y + ROW_H], outline=(209, 213, 219))
            # Center text
            bbox = draw.textbbox((0, 0), text, font=font_bold_8)
            tw = bbox[2] - bbox[0]
            draw.text((x + (DAY_W - tw) // 2, y + 8 * S), text, fill=fg, font=font_bold_8)

        # Row border
        draw.line([x_start, y + ROW_H, x_start + CODE_W + NAME_W + num_days * DAY_W, y + ROW_H],
                 fill=(229, 231, 235))

        y += ROW_H

    # ── Summary Rows ──
    for shift_code in shift_codes:
        draw.rectangle([x_start, y, x_start + CODE_W + NAME_W + num_days * DAY_W, y + SUMMARY_ROW_H],
                      fill=SUMMARY_BG_RGB)

        draw.text((x_start + CODE_W + 6 * S, y + 6 * S), f"{shift_code} Staff",
                 fill=(251, 191, 36), font=font_bold_8)

        shift_min = ed["min_staff"].get(shift_code, 1)

        for d in active_days:
            x = x_start + CODE_W + NAME_W + (d - 1) * DAY_W
            count = _count_shift_staff(schedule_data, display_order, d, shift_code)
            color = (252, 165, 165) if count < shift_min else (251, 191, 36)
            bbox = draw.textbbox((0, 0), str(count), font=font_bold_8)
            tw = bbox[2] - bbox[0]
            draw.text((x + (DAY_W - tw) // 2, y + 6 * S), str(count), fill=color, font=font_bold_8)

        y += SUMMARY_ROW_H

    # ── SIGNATURE SECTION ──
    y += 20 * S
    draw.text((x_start + 10 * S, y), f"Head Nurse: {dept['head_nurse']}", fill=(30, 41, 59), font=font_bold_10)
    draw.text((x_start + total_width // 2, y), f"Nursing Director: {dept['nursing_director']}",
              fill=(30, 41, 59), font=font_bold_10)

    y += 20 * S
    draw.text((x_start + 10 * S, y), "Signature: ____________________", fill=(100, 116, 139), font=font_9)
    draw.text((x_start + total_width // 2, y), "Signature: ____________________",
              fill=(100, 116, 139), font=font_9)

    y += 18 * S
    draw.text((x_start + 10 * S, y), "Date: ____/____/________", fill=(100, 116, 139), font=font_9)
    draw.text((x_start + total_width // 2, y), "Date: ____/____/________",
              fill=(100, 116, 139), font=font_9)

    # FIX #9: Save at 300 DPI (2x pixel dimensions)
    export_dir = _get_export_dir()
    filename = f"Schedule_{month_name}_{year}.png"
    filepath = export_dir / filename
    img.save(str(filepath), "PNG", dpi=(300, 300))

    return filepath
