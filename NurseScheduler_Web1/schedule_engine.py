"""
schedule_engine.py — Nurse Schedule Generator (3-Layer Architecture)
Uses OR-Tools CP-SAT Solver
Supports 10–100 employees, 6h/8h/12h shift modes
Maternity & Children Hospital – Hafr Al Batin Health Cluster

3-Layer Constraint System:
  - Layer 1 (RED): Hard constraints (never break) — AddExactlyOne, min/max staffing, head nurse, leaves, OFF counts, no dangerous sequences, max consecutive
  - Layer 2 (ORANGE): Important soft constraints (weight 800) — incharge coverage, pairing, separation, fairness, shift continuity
  - Layer 3 (GREEN): Preference soft constraints (weight ≤100) — weekend, experience, language, personal preferences
"""

import json
import time
import calendar
from datetime import date, timedelta
from ortools.sat.python import cp_model
from database import DepartmentDB
from constants import CONSTRAINT_WEIGHTS, SOLVER_TIME_LIMIT, SOLVER_EMERGENCY_LIMIT, SOLVER_WORKERS, SOLVER_LINEARIZATION
from constants import block_min_length, rule_max_offs


# ─────────────────────────────────────────────────
#  PROGRESS CALLBACK — Reports percentage to UI
# ─────────────────────────────────────────────────
class _ProgressCallback(cp_model.CpSolverSolutionCallback):
    """Reports REAL solver progress to a shared dict for UI polling:
    every improved solution bumps the count, objective, and bound."""

    def __init__(self, progress_dict, time_limit):
        super().__init__()
        self._progress = progress_dict
        self._time_limit = max(time_limit, 1)
        self._start = time.time()
        self._solutions = 0

    def on_solution_callback(self):
        elapsed = time.time() - self._start
        self._solutions += 1
        pct = max(15, min(95, int((elapsed / self._time_limit) * 100)))
        self._progress["percent"] = pct
        self._progress["objective"] = int(self.ObjectiveValue())
        self._progress["best_bound"] = int(self.BestObjectiveBound())
        self._progress["elapsed"] = round(elapsed, 1)
        self._progress["solutions"] = self._solutions
        self._progress["status"] = (
            f"Improving schedule — solution #{self._solutions} "
            f"(score {int(self.ObjectiveValue())})")


# ─────────────────────────────────────────────────
#  SHIFT CONFIGURATIONS
# ─────────────────────────────────────────────────
SHIFT_CONFIGS = {
    "12": {"shifts": ["D", "N"], "labels": {"D": "Day", "N": "Night"}},
    "8":  {"shifts": ["M", "E", "N"], "labels": {"M": "Morning", "E": "Evening", "N": "Night"}},
    "6":  {"shifts": ["M1", "M2", "E", "N"], "labels": {"M1": "Morning-1", "M2": "Morning-2", "E": "Evening", "N": "Night"}},
}
OFF = "OFF"

# Weekend days (Friday=4, Saturday=5 in Python weekday())
FRIDAY = 4
SATURDAY = 5


class ScheduleEngine:
    """
    Generate a monthly nurse schedule using CP-SAT with 3-layer constraint system.
    The schedule ALWAYS generates — it never fails.
    Returns (schedule_data, display_order, warnings, status_info).
    """

    def __init__(self, db: DepartmentDB):
        self.db = db
        self.warnings = []

    def generate(self, year: int, month: int, time_limit: int = SOLVER_TIME_LIMIT,
                 progress_dict: dict = None, start_day: int = None, end_day: int = None):
        """
        Main entry point for schedule generation.
        Args:
            year, month: Schedule month
            time_limit: Maximum solver time in seconds
            progress_dict: shared dict for UI progress polling
            start_day: First day of schedule range (1-based, default=1)
            end_day: Last day of schedule range (1-based, default=last day of month)
        Returns:
            schedule_data: {emp_id_str: {day_str: shift_code}}
            display_order: [emp_id, ...] — ordered for display
            warnings: [str, ...]
            status_info: {"status": "OK"|"INFEASIBLE"|"TIMEOUT"|"ERROR",
                          "applied_rules": [rule_names],
                          "violated_rules": [rule_names]}
        """
        self.warnings = []
        if progress_dict is not None:
            progress_dict["percent"] = 5
            progress_dict["status"] = "Loading data..."

        self._load_data(year, month, start_day=start_day, end_day=end_day)

        if not self.employees:
            return None, [], ["No active employees found."], {
                "status": "ERROR",
                "applied_rules": [],
                "violated_rules": []
            }

        # Loud reporting of constraints that could not be applied
        for label, chosen, cap in self.off_quota_capped:
            self.warnings.append(
                f"⚠ {label} OFF quota {chosen} is above the maximum the rest rule "
                f"(1-2 day rests) allows this month — {cap} OFF days applied")
        for label in self.unresolved_pairs:
            self.warnings.append(
                f"⚠ Pairing/separation rule not applied — staff members not found: {label}. "
                f"Delete the rule and add it again from the Constraints page.")
        for (a, b) in self.skipped_hard_pairs:
            na = self.emp_map.get(a, {}).get("name", a)
            nb = self.emp_map.get(b, {}).get("name", b)
            self.warnings.append(
                f"⚠ Pairing with the head nurse is not supported — ignored: {na} — {nb}")

        if progress_dict is not None:
            progress_dict["percent"] = 10
            progress_dict["status"] = f"Building model ({self.num_emps} employees)..."

        # Forced staff requests are HARD in the first attempt (guaranteed to be
        # honored); if that makes the model infeasible we retry with them soft
        # so generation still never fails.
        self._forced_hard = bool(self.forced_shifts)

        def _build():
            m = cp_model.CpModel()
            self._create_variables(m)
            self._add_layer1_hard(m)
            pens = self._add_layer2_important(m)
            pens.extend(self._add_layer3_preferences(m))
            self._set_objective(m, pens)
            self._add_prev_hints(m)
            return m

        def _solve(m, limit):
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = limit
            solver.parameters.num_workers = SOLVER_WORKERS
            solver.parameters.linearization_level = SOLVER_LINEARIZATION
            solver.parameters.cp_model_presolve = True
            if progress_dict is not None:
                callback = _ProgressCallback(progress_dict, limit)
                return solver, solver.Solve(m, callback)
            return solver, solver.Solve(m)

        model = _build()

        if progress_dict is not None:
            progress_dict["percent"] = 15
            progress_dict["status"] = "Solving..."
            progress_dict["solve_start"] = time.time()
            progress_dict["time_limit"] = time_limit

        solver, status = _solve(model, time_limit)

        # Retry pass: forced requests conflict with another hard rule (e.g. a
        # leave, pairing, or the head-nurse pattern) — relax them to soft and
        # explain, instead of failing the whole generation.
        if status == cp_model.INFEASIBLE and self._forced_hard:
            self.warnings.append(
                "⚠ Not all staff requests (SO / forced shifts) could be honored as hard rules — "
                "they conflicted with other rules, so the schedule was regenerated with the best possible fit")
            self._forced_hard = False
            model = _build()
            solver, status = _solve(model, max(30, time_limit // 2))

        # Prepare status info
        status_info = {
            "status": "OK",
            "applied_rules": [],
            "violated_rules": []
        }

        if status == cp_model.INFEASIBLE:
            status_info["status"] = "INFEASIBLE"
            self.warnings.append("⚠ Could not find a feasible schedule with current constraints.")
            return None, self.display_order, self.warnings, status_info

        if status == cp_model.MODEL_INVALID:
            status_info["status"] = "ERROR"
            self.warnings.append("⚠ Model error — please check constraints.")
            return None, self.display_order, self.warnings, status_info

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if status == cp_model.FEASIBLE:
                self.warnings.append("⚡ Best effort schedule (not proven optimal). Some preferences may not be fully satisfied.")

            schedule = self._extract_solution(solver)
            # Partial ranges merge into the saved month (never overwrite it);
            # the continuity tail is taken from the merged FULL month.
            merged = self._merge_with_saved(schedule)
            last_shifts = self._extract_last_shifts(merged)

            # Save to DB
            self.db.save_schedule(self.year, self.month, merged, last_shifts)

            # Return the merged month so UI edits/saves keep the full data
            return merged, self.display_order, self.warnings, status_info

        status_info["status"] = "TIMEOUT"
        self.warnings.append("⚠ Solver timed out without finding a solution. Try increasing time limit.")
        return None, self.display_order, self.warnings, status_info

    def _add_prev_hints(self, model):
        """Warm-start hints from this month's previously saved schedule
        (pairs with Rule 24 stability — faster solves, smaller diffs).
        Cells that CONFLICT with a forced-shift constraint are skipped —
        otherwise the old value anchors the solver against the new request."""
        if not self.prev_solution:
            return
        try:
            for eid_str, days_dict in self.prev_solution.items():
                try:
                    eid = int(eid_str)
                except (TypeError, ValueError):
                    continue
                if eid not in self.x:
                    continue
                for d_str, prev_shift in days_dict.items():
                    try:
                        d = int(d_str)
                    except (TypeError, ValueError):
                        continue
                    if d not in self.x[eid] or prev_shift not in self.x[eid][d]:
                        continue
                    allowed = self.forced_shifts.get(eid, {}).get(d)
                    if allowed and prev_shift not in allowed:
                        continue  # forced request overrides the old value
                    model.AddHint(self.x[eid][d][prev_shift], 1)
        except (AttributeError, Exception):
            pass

    def generate_emergency(self, year: int, month: int, remaining_days=None, progress_dict: dict = None,
                            start_day: int = None, end_day: int = None):
        """
        Emergency mode — fast schedule generation with shorter time limit.
        Uses 30-second time limit.
        """
        return self.generate(year, month, time_limit=SOLVER_EMERGENCY_LIMIT, progress_dict=progress_dict,
                             start_day=start_day, end_day=end_day)

    # ─────────────────────────────────────────────────
    #  LOAD DATA
    # ─────────────────────────────────────────────────
    def _load_data(self, year, month, start_day=None, end_day=None):
        self.year = year
        self.month = month
        max_days = calendar.monthrange(year, month)[1]
        self.start_day = max(1, min(start_day or 1, max_days))
        self.end_day = max(self.start_day, min(end_day or max_days, max_days))
        self.num_days = self.end_day - self.start_day + 1
        self.days = list(range(self.start_day, self.end_day + 1))
        self._max_days_in_month = max_days  # Keep full month length for reference

        # Work hours & shifts
        work_hours = self.db.get_setting("work_hours", "8")
        cfg = SHIFT_CONFIGS.get(work_hours, SHIFT_CONFIGS["8"])
        self.shift_codes = cfg["shifts"]
        self.shift_labels = cfg["labels"]
        self.num_shifts = len(self.shift_codes)
        self.work_hours = int(work_hours)

        # Employees
        all_emps = self.db.get_all_employees()
        self.employees = all_emps
        self.emp_ids = [e["id"] for e in all_emps]
        self.emp_map = {e["id"]: e for e in all_emps}
        self.num_emps = len(all_emps)

        # Display order: Head Nurse → Incharges → rest by experience
        self._build_display_order()

        # Staffing
        self.staff_min = {}
        self.staff_max = {}
        for s in self.shift_codes:
            self.staff_min[s] = int(self.db.get_setting(f"staff_{s}_min", "1") or "1")
            self.staff_max[s] = int(self.db.get_setting(f"staff_{s}_max", str(self.num_emps)) or str(self.num_emps))

        # Head Nurse
        hn_id_str = self.db.get_setting("head_nurse_id", "")
        self.head_nurse_id = int(hn_id_str) if hn_id_str and hn_id_str.isdigit() else None

        # Shift Incharges
        si_raw = self.db.get_setting("shift_incharge_ids", "[]")
        try:
            self.incharge_ids = set(json.loads(si_raw or "[]"))
        except (json.JSONDecodeError, TypeError):
            self.incharge_ids = set()

        # OFF counts per nationality (exact)
        sa_off_raw = (self.db.get_setting("saudi_offs_per_month", "") or "").strip()
        ns_off_raw = (self.db.get_setting("nonsaudi_offs_per_month", "") or "").strip()
        self.saudi_offs_fixed = int(sa_off_raw) if sa_off_raw.isdigit() else None
        self.nonsaudi_offs_fixed = int(ns_off_raw) if ns_off_raw.isdigit() else None

        # Leaves: {emp_id: set(day_numbers)}
        self.leave_days = {}
        for c in self.db.get_constraints("mandatory"):
            if c["constraint_type"] == "leave":
                p = c["params"]
                eid = p.get("employee_id")
                if eid is None:
                    continue
                try:
                    s = date.fromisoformat(p["start_date"])
                    e = date.fromisoformat(p["end_date"])
                except (ValueError, KeyError):
                    continue
                days_set = self.leave_days.setdefault(eid, set())
                d = s
                while d <= e:
                    if d.year == year and d.month == month:
                        days_set.add(d.day)
                    d += timedelta(days=1)

        # Breastfeeding — from the employee flag (DB is the source of truth)
        self.breastfeeding_ids = {e["id"] for e in all_emps if e.get("breastfeeding")}

        # Trainers / trainees — from the employee training_role field
        self.trainer_ids = [e["id"] for e in all_emps
                            if (e.get("training_role") or "").lower() == "trainer"]
        self.trainee_ids = [e["id"] for e in all_emps
                            if (e.get("training_role") or "").lower() == "trainee"]

        # Constraints — pairing/separation
        self.mandatory_pairing = []
        self.mandatory_separation = []
        self.pref_pairing = []
        self.pref_separation = []
        self.unresolved_pairs = []   # constraints whose employees can't be found
        self.skipped_hard_pairs = []  # e.g. pairs involving the head nurse

        for c in self.db.get_constraints("mandatory"):
            ct = c["constraint_type"]
            if ct not in ("pairing_mandatory", "separation_mandatory"):
                continue
            pair = self._resolve_pair(c["params"])
            if pair is None:
                continue
            if ct == "pairing_mandatory":
                self.mandatory_pairing.append(pair)
            else:
                self.mandatory_separation.append(pair)

        for c in self.db.get_constraints("preferential"):
            ct = c["constraint_type"]
            if ct not in ("pairing_preferential", "separation_preferential"):
                continue
            pair = self._resolve_pair(c["params"])
            if pair is None:
                continue
            if ct == "pairing_preferential":
                self.pref_pairing.append(pair)
            else:
                self.pref_separation.append(pair)

        # Forced shifts and forced weekend OFF
        self.forced_shifts = {}          # {emp_id: {day: [allowed_shifts]}}
        self.forced_weekend_emp_ids = set()
        valid_emp_set = set(self.emp_ids)
        for c in self.db.get_constraints("mandatory"):
            ct = c["constraint_type"]
            p = c["params"]
            if ct == "forced_shift":
                emp_ids = p.get("employee_ids", []) or []
                fdays = p.get("days", []) or []
                fshifts = [s for s in (p.get("shifts", []) or []) if s in ("M", "E", "N", "OFF", "D", "M1", "M2")]
                if not fshifts:
                    continue
                # Month-aware
                dates_list = p.get("dates", []) or []
                valid_days_for_month = set()
                if dates_list:
                    from datetime import date as _dt
                    for ds in dates_list:
                        try:
                            dd = _dt.fromisoformat(ds)
                            if dd.year == year and dd.month == month:
                                valid_days_for_month.add(dd.day)
                        except (ValueError, TypeError):
                            pass
                else:
                    for d in fdays:
                        try:
                            valid_days_for_month.add(int(d))
                        except (TypeError, ValueError):
                            pass
                for eid in emp_ids:
                    if eid not in valid_emp_set:
                        continue
                    for di in valid_days_for_month:
                        if di in self.days:
                            self.forced_shifts.setdefault(eid, {}).setdefault(di, [])
                            for s in fshifts:
                                if s not in self.forced_shifts[eid][di]:
                                    self.forced_shifts[eid][di].append(s)
            elif ct == "forced_weekend_off":
                for eid in (p.get("employee_ids", []) or []):
                    if eid in valid_emp_set:
                        self.forced_weekend_emp_ids.add(eid)

        # Block system — 4-6 day same-shift blocks, 1-2 day rests, forward rotation
        self.block_system = self.db.get_setting("block_system", "1") == "1"

        # The chosen OFF quota must live INSIDE the block rule (rests 1-2 days):
        # a quota above what the rule allows this month is capped — the rule
        # is never relaxed to fit the quota.
        self.off_quota_capped = []
        if self.block_system:
            month_days = calendar.monthrange(year, month)[1]
            cap = rule_max_offs(month_days, block_min_length(self.work_hours))
            if self.saudi_offs_fixed is not None and self.saudi_offs_fixed > cap:
                self.off_quota_capped.append(("Saudi", self.saudi_offs_fixed, cap))
                self.saudi_offs_fixed = cap
            if self.nonsaudi_offs_fixed is not None and self.nonsaudi_offs_fixed > cap:
                self.off_quota_capped.append(("Non-Saudi", self.nonsaudi_offs_fixed, cap))
                self.nonsaudi_offs_fixed = cap

        # Professional certificates — driven by the EMPLOYEE DATABASE, not a
        # preferences option: every certificate actually held by active staff
        # gets a coverage rule. Holders map: {cert: [emp_ids]}
        self.cert_holders = {}
        for e in all_emps:
            for cert in (e.get("certificates") or []):
                self.cert_holders.setdefault(cert, []).append(e["id"])
        self.required_certs = sorted(self.cert_holders.keys())

        # Previous-month tail per employee: (last_shift, run_len, trailing_offs)
        # derived from the 3-day last_shifts snapshot for cross-month continuity
        self.prev_tail = {}

        # Preferences — every checkbox in the Preferences tab is wired here
        self.pref_fair_weekend = self.db.get_setting("pref_fair_weekend", "1") == "1"
        self.pref_saudi_weekend = self.db.get_setting("pref_saudi_weekend", "1") == "1"
        self.pref_nonsaudi_weekend = self.db.get_setting("pref_nonsaudi_weekend", "1") == "1"
        self.pref_consecutive_offs = self.db.get_setting("pref_consecutive_offs", "1") == "1"
        self.pref_exp_balance = self.db.get_setting("pref_exp_balance", "1") == "1"
        self.pref_arabic_balance = self.db.get_setting("pref_arabic_balance", "1") == "1"
        self.pref_monthly_compare = self.db.get_setting("pref_monthly_compare", "1") == "1"

        # Weekend days in this month
        self.weekend_days = []
        for d in self.days:
            dt = date(year, month, d)
            if dt.weekday() in (FRIDAY, SATURDAY):
                self.weekend_days.append(d)

        # Friday/Saturday pairs
        self.weekend_pairs = []
        for d in self.days:
            dt = date(year, month, d)
            if dt.weekday() == FRIDAY:
                sat = d + 1
                if sat <= self.end_day:
                    self.weekend_pairs.append((d, sat))

        # Previous month data — loaded UNCONDITIONALLY: cross-month safety
        # (rest after nights, consecutive-day caps) must never be switched
        # off by a preferences checkbox. pref_monthly_compare no longer
        # gates any hard constraint.
        self.prev_last_shifts = self.db.get_previous_month_last_shifts(year, month)

        # Rule 24 (stability): previously SAVED schedule for THIS month.
        # On regeneration we prefer keeping unchanged cells so a small
        # constraint edit produces a small, reviewable diff.
        self.prev_solution = {}
        try:
            saved = self.db.get_schedule(year, month)
            if saved and isinstance(saved.get("schedule_data"), dict):
                self.prev_solution = saved["schedule_data"]
        except Exception:
            self.prev_solution = {}

        # Derive per-employee tail from the 3-day snapshot:
        # (last_worked_shift_or_None, same-shift run length, trailing OFF count)
        for eid_str, days_dict in (self.prev_last_shifts or {}).items():
            try:
                eid = int(eid_str)
            except (TypeError, ValueError):
                continue
            if eid not in self.emp_map or not days_dict:
                continue
            try:
                ordered_days = sorted(int(k) for k in days_dict.keys())
            except (TypeError, ValueError):
                continue
            seq = [days_dict[str(k)] for k in ordered_days]
            trailing_offs = 0
            for v in reversed(seq):
                if v == OFF:
                    trailing_offs += 1
                else:
                    break
            work_part = seq[:len(seq) - trailing_offs]
            last_shift, run = None, 0
            if work_part:
                last_shift = work_part[-1]
                for v in reversed(work_part):
                    if v == last_shift:
                        run += 1
                    else:
                        break
            self.prev_tail[eid] = (last_shift, run, trailing_offs)

    def diagnose(self, year: int, month: int):
        """
        Pre-generation diagnostic — checks data quality.
        Returns:
            (issues_list, is_ready_bool)
        """
        self._load_data(year, month)
        issues = []

        # Critical: no active employees
        if not self.num_emps:
            issues.append("⛔ No active staff members")
            return issues, False

        # Warning: staff_min requirements exceed available employees.
        # Generation still proceeds — the shortage is penalized and reported.
        total_staff_min = sum(self.staff_min.values())
        if self.num_emps < total_staff_min:
            issues.append(
                f"⚠ Staff count ({self.num_emps}) is below the required minimum "
                f"({total_staff_min}) — the schedule will still be generated with coverage "
                f"shortage warnings, and the gap will appear in the Quality Report"
            )

        # Warning: missing experience data
        missing_exp_count = sum(
            1 for e in self.employees
            if e.get("experience_years") is None or e.get("experience_years") == 0
        )
        if missing_exp_count > 0:
            issues.append(f"⚠ Experience data missing for {missing_exp_count} staff member(s)")

        # Warning: test data detected
        test_names = set()
        for e in self.employees:
            name = e.get("name", "").strip().lower()
            if name in ("jjjjjjj", "hjfgjgh", "test", "xxxxxxx", "zzzzzzz"):
                test_names.add(e.get("name", "Unknown"))
        if test_names:
            issues.append(f"⚠ Test data detected: {', '.join(test_names)}")

        # Warning: pairing/separation constraints that resolve to no one
        if self.unresolved_pairs:
            issues.append(
                f"⚠ Pairing/separation rules that match no current staff member: "
                f"{', '.join(self.unresolved_pairs)}")

        # Warning: forced-OFF requests exceeding the employee's monthly OFF quota
        for eid, day_map in self.forced_shifts.items():
            forced_off_days = [d for d, allowed in day_map.items()
                               if allowed and set(allowed) == {OFF}]
            if not forced_off_days:
                continue
            emp = self.emp_map.get(eid, {})
            if eid == self.head_nurse_id:
                continue  # head nurse has weekend-only offs, no quota
            is_saudi = emp.get("nationality") == "سعودي"
            quota = self.saudi_offs_fixed if is_saudi else self.nonsaudi_offs_fixed
            if quota is None:
                continue
            leave_count = len(self.leave_days.get(eid, set()))
            target = min(max(quota, leave_count), self.num_days)
            if len(forced_off_days) > target:
                issues.append(
                    f"⚠ Requested OFF days for {emp.get('name', eid)} "
                    f"({len(forced_off_days)} days) exceed the monthly OFF quota ({target}) — "
                    f"the requests will be honored and the quota exceeded")

        # Warning: mandatory pairing with full-month leave employees
        if self.mandatory_pairing:
            problematic_pairs = []
            for emp_a, emp_b in self.mandatory_pairing:
                leave_a = self.leave_days.get(emp_a, set())
                leave_b = self.leave_days.get(emp_b, set())
                if len(leave_a) == self.num_days or len(leave_b) == self.num_days:
                    name_a = self.emp_map.get(emp_a, {}).get("name", f"ID {emp_a}")
                    name_b = self.emp_map.get(emp_b, {}).get("name", f"ID {emp_b}")
                    problematic_pairs.append(f"{name_a}–{name_b}")
            if problematic_pairs:
                issues.append(
                    f"⚠ Mandatory pairing includes staff on full-month leave: "
                    f"{', '.join(problematic_pairs)}"
                )

        # Block system: the OFF quota is capped to what 1-2 day rests allow
        # (see _load_data) — tell the user before generating.
        for label, chosen, cap in self.off_quota_capped:
            issues.append(
                f"⚠ {label} OFF quota {chosen} is above the maximum the rest rule "
                f"(1-2 day rests) allows this month — {cap} will be applied")

        # Certificate coverage feasibility (informational — never blocks)
        for cert in self.required_certs:
            n_holders = len(self.cert_holders.get(cert, []))
            if 0 < n_holders < self.num_shifts:
                issues.append(
                    f"⚠ Certificate {cert}: only {n_holders} holder(s) for {self.num_shifts} shifts "
                    f"— full daily coverage is not possible; the schedule will balance it as far as possible")

        # Trainer/trainee sanity (informational — never blocks)
        if self.trainee_ids and not self.trainer_ids:
            issues.append(
                f"⚠ {len(self.trainee_ids)} trainee(s) but no trainer — "
                f"the supervision rule will not work until a trainer is assigned")
        elif self.trainee_ids and len(self.trainer_ids) < self.num_shifts:
            issues.append(
                f"⚠ Trainer count ({len(self.trainer_ids)}) is below the number of shifts "
                f"({self.num_shifts}) — the schedule will pair trainees with trainers as far as possible")

        # Warning: not enough shift incharges
        if self.incharge_ids:
            num_incharges = len([e for e in self.incharge_ids if e in self.emp_ids])
            if num_incharges < self.num_shifts:
                issues.append(
                    f"⚠ Shift incharge count ({num_incharges}) is below the number of shifts "
                    f"({self.num_shifts})"
                )

        # Success
        is_ready = not any("⛔" in i for i in issues)
        if is_ready:
            issues.append("✓ Data is ready for generation")

        return issues, is_ready

    def _resolve_pair(self, params):
        """
        Resolve employee pair from constraint params to (id_a, id_b).
        Prefers stored employee IDs (stable); falls back to legacy
        "CODE. Name" labels. Unresolvable pairs are recorded and reported
        loudly instead of being silently dropped.
        """
        valid = set(self.emp_ids)
        id_a = params.get("employee_a_id")
        id_b = params.get("employee_b_id")
        if id_a in valid and id_b in valid and id_a != id_b:
            return (id_a, id_b)

        # Legacy fallback: match by display label (codes shift when staff change)
        name_a = params.get("employee_a", "")
        name_b = params.get("employee_b", "")
        la = lb = None
        for e in self.employees:
            full = f"{e['code']}. {e['name']}"
            if full == name_a:
                la = e["id"]
            if full == name_b:
                lb = e["id"]
        if la and lb and la != lb:
            return (la, lb)

        self.unresolved_pairs.append(
            f"{name_a or id_a or '?'} — {name_b or id_b or '?'}")
        return None

    def _build_display_order(self):
        """Build ordered list: Head Nurse → Incharges → employees by experience."""
        hn_id_str = self.db.get_setting("head_nurse_id", "")
        hn_id = int(hn_id_str) if hn_id_str and hn_id_str.isdigit() else None
        si_raw = self.db.get_setting("shift_incharge_ids", "[]")
        try:
            si_ids = json.loads(si_raw or "[]")
        except (json.JSONDecodeError, TypeError):
            si_ids = []

        order = []
        added = set()

        # Head Nurse first
        if hn_id and hn_id in self.emp_map:
            order.append(hn_id)
            added.add(hn_id)

        # Shift Incharges — sorted by experience descending
        incharge_emps = [self.emp_map[sid] for sid in si_ids
                         if sid in self.emp_map and sid not in added]
        incharge_emps.sort(key=lambda e: float(e.get("experience_years", 0) or 0),
                           reverse=True)
        for e in incharge_emps:
            order.append(e["id"])
            added.add(e["id"])

        # Rest sorted by experience descending
        rest = [e for e in self.employees if e["id"] not in added]
        rest.sort(key=lambda e: float(e.get("experience_years", 0) or 0), reverse=True)
        for e in rest:
            order.append(e["id"])

        self.display_order = order

    # ─────────────────────────────────────────────────
    #  VARIABLES
    # ─────────────────────────────────────────────────
    def _create_variables(self, model):
        """
        x[e][d][s] = 1 if employee e works shift s on day d
        x[e][d][OFF_idx] = 1 if employee e is OFF on day d
        """
        self.x = {}
        self.all_options = self.shift_codes + [OFF]
        self.shift_idx = {s: i for i, s in enumerate(self.all_options)}

        for e in self.emp_ids:
            self.x[e] = {}
            for d in self.days:
                self.x[e][d] = {}
                for s in self.all_options:
                    self.x[e][d][s] = model.NewBoolVar(f"x_{e}_{d}_{s}")

    # ─────────────────────────────────────────────────
    #  LAYER 1 — HARD CONSTRAINTS (RED)
    # ─────────────────────────────────────────────────
    def _add_layer1_hard(self, model):
        """
        Add all hard constraints (Layer 1).
        These ALWAYS apply and use model.Add() directly.

        Rules:
          1. One shift per employee per day — AddExactlyOne
          2. Staff Min/Max coverage — each shift meets min/max per day
          3. Head Nurse — morning on weekdays, OFF on weekends
          4. Approved leaves = OFF — leave days are forced OFF
          5. Nationality OFF count — Saudi=8, Non-Saudi=7 (exact)
          6. No dangerous sequences — N→M next day forbidden, E→M next day forbidden
          7. Max consecutive work — max 6 work days, max 5 consecutive nights
        """

        # Rule 1: Each employee: exactly one option per day
        for e in self.emp_ids:
            for d in self.days:
                model.AddExactlyOne(self.x[e][d][s] for s in self.all_options)

        # Rule 4: Leave days → must be OFF
        for eid, days_off in self.leave_days.items():
            if eid not in self.x:
                continue
            for d in days_off:
                if d in self.x[eid]:
                    model.Add(self.x[eid][d][OFF] == 1)

        # Rule 2: Staffing MIN moved to Layer 2 as a VERY-HIGH soft constraint
        # (weight 2500 — the strongest term). Generation NEVER fails on staff
        # shortage; the gap is reported in warnings and the quality report.

        # Rule 9-HARD: Mandatory pairing — identical assignment every day.
        # Pairs involving the head nurse are skipped (her fixed pattern is
        # incompatible with rotation) and reported.
        for (a, b) in self.mandatory_pairing:
            if a not in self.x or b not in self.x:
                continue
            if self.head_nurse_id in (a, b):
                self.skipped_hard_pairs.append((a, b))
                continue
            a_leave = self.leave_days.get(a, set())
            b_leave = self.leave_days.get(b, set())
            for d in self.days:
                if d in a_leave or d in b_leave:
                    continue
                for s in self.all_options:
                    model.Add(self.x[a][d][s] == self.x[b][d][s])

        # Rule 10-HARD: Mandatory separation — never the same working shift
        for (a, b) in self.mandatory_separation:
            if a not in self.x or b not in self.x:
                continue
            for d in self.days:
                for s in self.shift_codes:
                    model.Add(self.x[a][d][s] + self.x[b][d][s] <= 1)

        # Rule 3: Head Nurse: ALL weekends = OFF, weekdays = Morning
        if self.head_nurse_id and self.head_nurse_id in self.x:
            first_shift = self.shift_codes[0]
            hn_id = self.head_nurse_id
            weekend_set = set(self.weekend_days)
            hn_leave_days = self.leave_days.get(hn_id, set())
            for d in self.days:
                if d in hn_leave_days:
                    continue
                if d in weekend_set:
                    model.Add(self.x[hn_id][d][OFF] == 1)
                else:
                    model.Add(self.x[hn_id][d][first_shift] == 1)
                    for s in self.shift_codes:
                        if s != first_shift:
                            model.Add(self.x[hn_id][d][s] == 0)

        # Rule 20-HARD: Forced specific-day shifts (staff requests / SO) as
        # HARD constraints on the first solve attempt — an approved request is
        # a commitment, not a preference. generate() retries with the soft
        # version if this ever makes the model infeasible. Days where the
        # employee is on leave are skipped (leave already forces OFF).
        if getattr(self, "_forced_hard", False):
            for eid, day_map in self.forced_shifts.items():
                if eid not in self.x:
                    continue
                emp_leave = self.leave_days.get(eid, set())
                for d, allowed in day_map.items():
                    if d not in self.x[eid] or not allowed:
                        continue
                    if d in emp_leave and OFF not in allowed:
                        continue  # unsatisfiable vs leave — leave wins, soft warns
                    opts = [self.x[eid][d][s] for s in allowed if s in self.x[eid][d]]
                    if opts:
                        model.Add(sum(opts) >= 1)

        # Rule 5a: Shift Incharges: OFF quota follows the configured nationality
        # quota (saudi/nonsaudi_offs_per_month) like everyone else — falls back
        # to 8 only when no quota is configured ("Auto").
        # Upper bound (== target) moved to Layer 2 as soft constraint
        if self.incharge_ids:
            valid_incharges = [eid for eid in self.incharge_ids if eid in self.x]
            self._valid_incharges = valid_incharges
            self._incharge_off_targets = {}
            if valid_incharges:
                for e in valid_incharges:
                    is_saudi = self.emp_map.get(e, {}).get("nationality") == "سعودي"
                    quota = self.saudi_offs_fixed if is_saudi else self.nonsaudi_offs_fixed
                    if quota is None:
                        quota = 8
                    leave_count = len(self.leave_days.get(e, set()))
                    target = min(max(quota, leave_count), self.num_days)
                    target = self._range_off_target(e, target)
                    self._incharge_off_targets[e] = target
                    off_sum = sum(self.x[e][d][OFF] for d in self.days)
                    model.Add(off_sum >= target)  # minimum guarantee (hard)

        # Rule 5: Nationality OFF count — HARD minimum (>= target)
        # Exact match (== target) moved to Layer 2 as soft penalty
        self._nationality_offs_targets = {}
        if self.saudi_offs_fixed is not None or self.nonsaudi_offs_fixed is not None:
            excluded = set()
            if self.head_nurse_id:
                excluded.add(self.head_nurse_id)
            excluded.update(self.incharge_ids)

            for e in self.emp_ids:
                if e in excluded:
                    continue
                emp = self.emp_map.get(e, {})
                is_saudi = emp.get("nationality") == "سعودي"
                target = self.saudi_offs_fixed if is_saudi else self.nonsaudi_offs_fixed
                if target is None:
                    continue
                leave_count = len(self.leave_days.get(e, set()))
                t = min(max(target, leave_count), self.num_days)
                t = self._range_off_target(e, t)
                self._nationality_offs_targets[e] = t
                off_sum = sum(self.x[e][d][OFF] for d in self.days)
                model.Add(off_sum >= t)  # minimum guarantee (hard)

        # Rule 6a: N → next day not any morning shift (hard)
        # Covers ALL morning variants (M, M1, M2, D) — not just the first code.
        first_shift = self.shift_codes[0]
        morning_shifts = [s for s in self.shift_codes if s.startswith("M") or s == "D"]
        for e in self.emp_ids:
            for d in self.days[:-1]:  # all days except last
                next_d = d + 1
                if next_d in self.x[e]:
                    for m in morning_shifts:
                        model.Add(self.x[e][d]["N"] + self.x[e][next_d][m] <= 1)

        # Rule 6b: E → next day not any morning shift (hard), if E exists
        if "E" in self.shift_codes:
            for e in self.emp_ids:
                for d in self.days[:-1]:
                    next_d = d + 1
                    if next_d in self.x[e]:
                        for m in morning_shifts:
                            model.Add(self.x[e][d]["E"] + self.x[e][next_d][m] <= 1)

        # Rule 6c: N → next day not E (hard) — prevents NEN alternation
        if "E" in self.shift_codes:
            for e in self.emp_ids:
                for d in self.days[:-1]:
                    next_d = d + 1
                    if next_d in self.x[e]:
                        model.Add(self.x[e][d]["N"] + self.x[e][next_d]["E"] <= 1)

        # Rule 7a: Max consecutive nights — 4 in 12h mode (heavier shifts),
        # 6 otherwise
        night_code = "N"
        max_consec_nights = 4 if self.work_hours == 12 else 6
        for e in self.emp_ids:
            for i in range(len(self.days) - max_consec_nights):
                consecutive = [self.x[e][self.days[i + j]][night_code]
                               for j in range(max_consec_nights + 1)
                               if (i + j) < len(self.days)]
                if len(consecutive) == max_consec_nights + 1:
                    model.Add(sum(consecutive) <= max_consec_nights)

        # Rule 7b: Max consecutive work days — 4 in 12h mode, 6 otherwise
        max_consec_work = 4 if self.work_hours == 12 else 6
        consec_emps = [e for e in self.emp_ids if e != self.head_nurse_id]
        for e in consec_emps:
            for i in range(len(self.days) - max_consec_work):
                if (i + max_consec_work) < len(self.days):
                    off_in_window = [self.x[e][self.days[i + j]][OFF]
                                     for j in range(max_consec_work + 1)]
                    model.Add(sum(off_in_window) >= 1)

        self._consec_emps = consec_emps

        # Rule 7c (cross-month, ALWAYS ON): safety continuity from the
        # previous month's saved tail — applies regardless of the block
        # system and of any preference toggle, and only when this range
        # starts at day 1 (a mid-month range is not adjacent to last month).
        # Closes the measured 9-consecutive-nights boundary defect.
        if self.start_day == 1 and self.prev_tail:
            d1 = self.days[0]
            for e, (last_shift, run, trailing_offs) in self.prev_tail.items():
                if e not in self.x or d1 not in self.x[e]:
                    continue
                if trailing_offs > 0 or not last_shift:
                    continue  # rested at month end — day 1 is unconstrained
                # Dangerous sequences across the boundary (mirror Rules 6a-6c)
                if last_shift == "N":
                    for m in morning_shifts:
                        if m in self.x[e][d1]:
                            model.Add(self.x[e][d1][m] == 0)
                    if "E" in self.x[e][d1]:
                        model.Add(self.x[e][d1]["E"] == 0)
                elif last_shift == "E":
                    for m in morning_shifts:
                        if m in self.x[e][d1]:
                            model.Add(self.x[e][d1][m] == 0)
                # Max 6 consecutive WORK days including last month's tail
                if e in consec_emps:
                    more = max(0, max_consec_work - run)
                    window = self.days[:more + 1]
                    if len(window) == more + 1:
                        model.Add(sum(self.x[e][d][OFF] for d in window) >= 1)
                # Max 6 consecutive NIGHTS including last month's tail
                if last_shift == night_code:
                    more_n = max(0, max_consec_nights - run)
                    window_n = self.days[:more_n + 1]
                    if len(window_n) == more_n + 1:
                        model.Add(sum(self.x[e][d][night_code] for d in window_n)
                                  <= len(window_n) - 1)

        # ── BLOCK SYSTEM (Rules B1-B5, hard with smart exemptions) ──
        if self.block_system:
            self._add_block_rules(model)

    def _allowed_transitions(self):
        """
        Allowed shift-type change between consecutive blocks (across the rest gap).
        Forward-rotation policy: M→E, M→N, E→N, N→E, N→M.
        E→M (backward) and same-type repeats are forbidden.
        """
        if "E" not in self.shift_codes:
            # 12h mode: D↔N alternation
            return {"D": {"N"}, "N": {"D"}}
        morning = [s for s in self.shift_codes if s.startswith("M")]
        allowed = {}
        for m in morning:
            # morning → evening/night, and morning-variant swaps (6h mode)
            allowed[m] = {"E", "N"} | (set(morning) - {m})
        allowed["E"] = {"N"}
        allowed["N"] = {"E"} | set(morning)
        return allowed

    def _add_block_rules(self, model):
        """
        B1: within a work block every day keeps the same shift type (hard).
        B2: block length >= 4 (exempt: month tail, leave-adjacent windows).
        B4: across a 1-2 day rest, the next block's type follows the rotation map.
        B5: rest gaps <= 2 days (exempt: leave-adjacent, month start).
            The OFF quota is capped to fit this rule (rule_max_offs), never the reverse.
        Cross-month: day-1 continuation constrained by previous month's tail.
        Head nurse and full-month-leave employees are exempt.
        """
        # 12h shifts are physically heavier: blocks are 2-4 consecutive days
        # (same shift type). 8h/6h keep the 4-6 day blocks.
        if self.work_hours == 12:
            MIN_BLOCK, MAX_BLOCK = 2, 4
        else:
            MIN_BLOCK, MAX_BLOCK = 4, 6
        allowed = self._allowed_transitions()
        block_emps = [e for e in self.emp_ids
                      if e != self.head_nurse_id
                      and len(self.leave_days.get(e, set())) < self.num_days]
        self._block_emps = block_emps

        for e in block_emps:
            X = self.x[e]
            leaves = set(self.leave_days.get(e, set()))
            # Staff-requested OFFs (SO / forced OFF) flex the block rhythm
            # exactly like leave days: B2/B4/B5 windows around them are
            # exempt, so an approved request is never mathematically
            # impossible just because of block parity.
            if getattr(self, "_forced_hard", False):
                leaves |= {d for d, alw in self.forced_shifts.get(e, {}).items()
                           if alw and set(alw) == {OFF} and d in self.x[e]}

            def leave_near(a, b):
                return any(w in leaves for w in range(max(self.start_day, a),
                                                      min(self.end_day, b) + 1))

            # B1 — no shift-type change between adjacent working days
            for d in self.days[:-1]:
                for s in self.shift_codes:
                    for t in self.shift_codes:
                        if t != s:
                            model.AddBoolOr([X[d][s].Not(), X[d + 1][t].Not()])

            # B4 — rotation across 1- and 2-day rests
            for d in self.days:
                for gap in (1, 2):
                    nd = d + gap + 1
                    if nd > self.end_day:
                        continue
                    if leave_near(d, nd):
                        continue
                    off_lits = [X[d + g][OFF] for g in range(1, gap + 1)]
                    for s in self.shift_codes:
                        for t in self.shift_codes:
                            if t != s and t in allowed.get(s, set()):
                                continue  # legal transition
                            model.AddBoolOr(
                                [X[d][s].Not()]
                                + [o.Not() for o in off_lits]
                                + [X[nd][t].Not()]
                            )

            # B2 — a block that starts must run at least MIN_BLOCK days
            # (unless clipped by month end or near a leave)
            for d in self.days:
                if leave_near(d - 1, d + MIN_BLOCK - 1):
                    continue
                cond = [X[d][OFF].Not()]
                if d > self.start_day:
                    cond.append(X[d - 1][OFF])
                elif self.prev_tail.get(e, (None, 0, 0))[0] is not None and \
                        self.prev_tail[e][2] == 0:
                    continue  # day 1 continues an open block from last month
                for w in range(d + 1, min(d + MIN_BLOCK, self.end_day + 1)):
                    model.Add(X[w][OFF] == 0).OnlyEnforceIf(cond)

            # B5 — never 3 consecutive OFF days (rests are 1-2 days)
            for d in self.days:
                if d == self.start_day or d + 2 > self.end_day:
                    continue  # month-start leniency
                if leave_near(d - 1, d + 3):
                    continue
                model.AddBoolOr([X[d][OFF].Not(), X[d + 1][OFF].Not(),
                                 X[d + 2][OFF].Not()])

            # Cross-month continuation from the tail snapshot (only when this
            # range actually starts at day 1 — a mid-month range is not
            # adjacent to the previous month)
            tail = self.prev_tail.get(e)
            if (self.start_day == 1 and tail and tail[0] in self.shift_codes
                    and self.start_day in X):
                s_prev, run, trailing_offs = tail
                if trailing_offs == 0:
                    # Open block: day 1 is OFF or continues the SAME type,
                    # and the combined run must respect MAX_BLOCK.
                    for t in self.shift_codes:
                        if t != s_prev:
                            model.Add(X[self.start_day][t] == 0)
                    max_ext = max(0, MAX_BLOCK - run)
                    window = [self.start_day + k for k in range(max_ext + 1)
                              if self.start_day + k <= self.end_day]
                    if len(window) == max_ext + 1 and not leave_near(window[0], window[-1]):
                        model.AddBoolOr([X[w][OFF] for w in window])
                elif trailing_offs >= 1:
                    # Open rest gap: first work day must follow the rotation map.
                    remaining = max(0, 2 - trailing_offs)
                    for k in range(remaining + 1):
                        d0 = self.start_day + k
                        if d0 not in X or leave_near(self.start_day, d0):
                            break
                        prior_offs = [X[self.start_day + j][OFF] for j in range(k)]
                        for t in self.shift_codes:
                            if t != s_prev and t in allowed.get(s_prev, set()):
                                continue
                            model.AddBoolOr(
                                [o.Not() for o in prior_offs] + [X[d0][t].Not()]
                            )

    # ─────────────────────────────────────────────────
    #  LAYER 2 — IMPORTANT SOFT CONSTRAINTS (ORANGE)
    # ─────────────────────────────────────────────────
    def _add_layer2_important(self, model):
        """
        Add important soft constraints with high weight (800).
        These can break but solver tries very hard to avoid violations.

        Rules:
          8. Shift Incharge coverage — ≥1 incharge per shift per day (weight 800)
          9. Mandatory Pairing — paired employees same shift (weight 800)
          10. Mandatory Separation — separated employees different shifts (weight 800)
          11. Night fairness — max difference ≤4 nights (weight 600)
          12. Evening fairness — max difference ≤4 evenings (weight 600)
          13. Breastfeeding distribution — distribute evenly across shifts (weight 500)
          14. Shift continuity (4-6 days) — same shift type 4-6 days (weight 500)
          15. Shift dominance cap ≤70% — no >70% one shift type (weight 400)
        """
        penalties = []

        # Rule 2: Staff MIN per shift per day — strongest soft term (w=2500).
        # Each missing nurse below the minimum costs 2500; the solver fills
        # minimums first, but an under-staffed month still generates.
        STAFF_MIN_WEIGHT = CONSTRAINT_WEIGHTS["staff_min"]
        for d in self.days:
            for s in self.shift_codes:
                staff_on = [self.x[e][d][s] for e in self.emp_ids]
                shortage = model.NewIntVar(0, self.staff_min[s], f"short_{d}_{s}")
                model.Add(sum(staff_on) + shortage >= self.staff_min[s])
                penalties.append((shortage, STAFF_MIN_WEIGHT))

        # Rule 2b: Staff MAX per shift per day (SOFT — was hard, caused infeasibility)
        STAFF_MAX_WEIGHT = CONSTRAINT_WEIGHTS["staff_max"]
        for d in self.days:
            for s in self.shift_codes:
                staff_on = [self.x[e][d][s] for e in self.emp_ids]
                over_max = model.NewBoolVar(f"over_max_{d}_{s}")
                model.Add(sum(staff_on) > self.staff_max[s]).OnlyEnforceIf(over_max)
                model.Add(sum(staff_on) <= self.staff_max[s]).OnlyEnforceIf(over_max.Not())
                penalties.append((over_max, STAFF_MAX_WEIGHT))

        # Rule 5b: Nationality OFF exact match (SOFT — complement to hard >= in Layer 1)
        OFF_EXACT_WEIGHT = CONSTRAINT_WEIGHTS["off_exact"]
        for e, t in self._nationality_offs_targets.items():
            off_sum = sum(self.x[e][d][OFF] for d in self.days)
            excess = model.NewIntVar(0, self.num_days, f"off_excess_{e}")
            model.Add(excess == off_sum - t)
            penalties.append((excess, OFF_EXACT_WEIGHT))

        # Rule 5a-b: Incharge OFF exact match (SOFT)
        incharge_targets = getattr(self, "_incharge_off_targets", {})
        for e, t in incharge_targets.items():
            off_sum = sum(self.x[e][d][OFF] for d in self.days)
            ic_excess = model.NewIntVar(0, self.num_days, f"ic_off_excess_{e}")
            model.Add(ic_excess == off_sum - t)
            penalties.append((ic_excess, OFF_EXACT_WEIGHT))

        # Rule 8: Shift Incharge coverage — at least 1 per shift per day.
        # Applies with ANY number of incharges (best effort when fewer than
        # shifts — previously the rule disabled itself entirely in that case).
        valid_incharges = getattr(self, "_valid_incharges", [])
        if valid_incharges:
            INCHARGE_COVER_WEIGHT = CONSTRAINT_WEIGHTS["incharge_cover"]
            for d in self.days:
                for s in self.shift_codes:
                    # Try to have at least 1
                    has_one = model.NewBoolVar(f"inc_cover_{d}_{s}")
                    incharge_on = [self.x[e][d][s] for e in valid_incharges]
                    model.Add(sum(incharge_on) >= 1).OnlyEnforceIf(has_one)
                    model.Add(sum(incharge_on) == 0).OnlyEnforceIf(has_one.Not())
                    penalties.append((has_one.Not(), INCHARGE_COVER_WEIGHT))

        # Rule 9: PREFERENTIAL pairing — same shift (1 penalty per day).
        # Mandatory pairing is HARD in Layer 1 now; this soft version applies
        # to the "preferential" category (previously loaded but never used).
        PAIRING_WEIGHT = CONSTRAINT_WEIGHTS["pairing"]
        for (a, b) in self.pref_pairing:
            if a not in self.x or b not in self.x:
                continue
            a_leave = self.leave_days.get(a, set())
            b_leave = self.leave_days.get(b, set())
            for d in self.days:
                if d in a_leave or d in b_leave:
                    continue
                # One bool per option: is_same[s] = (a==1 AND b==1) for this option
                same_bools = []
                for s in self.all_options:
                    both_on = model.NewBoolVar(f"pair_both_{a}_{b}_{d}_{s}")
                    model.AddBoolAnd([self.x[a][d][s], self.x[b][d][s]]).OnlyEnforceIf(both_on)
                    model.AddBoolOr([self.x[a][d][s].Not(), self.x[b][d][s].Not()]).OnlyEnforceIf(both_on.Not())
                    same_bools.append(both_on)
                # If any option has both=1, they're on the same shift
                paired = model.NewBoolVar(f"paired_{a}_{b}_{d}")
                model.AddBoolOr(same_bools).OnlyEnforceIf(paired)
                model.AddBoolAnd([b.Not() for b in same_bools]).OnlyEnforceIf(paired.Not())
                penalties.append((paired.Not(), PAIRING_WEIGHT))

        # Rule 10: PREFERENTIAL separation — different shifts (1 penalty per day).
        # Mandatory separation is HARD in Layer 1 now.
        SEPARATION_WEIGHT = CONSTRAINT_WEIGHTS["separation"]
        for (a, b) in self.pref_separation:
            if a not in self.x or b not in self.x:
                continue
            for d in self.days:
                # Check if both are on the SAME working shift (not OFF)
                same_shift_bools = []
                for s in self.shift_codes:
                    both_on_s = model.NewBoolVar(f"sep_both_{a}_{b}_{d}_{s}")
                    model.AddBoolAnd([self.x[a][d][s], self.x[b][d][s]]).OnlyEnforceIf(both_on_s)
                    model.AddBoolOr([self.x[a][d][s].Not(), self.x[b][d][s].Not()]).OnlyEnforceIf(both_on_s.Not())
                    same_shift_bools.append(both_on_s)
                # Penalize if ANY shift has both employees
                any_same = model.NewBoolVar(f"sep_any_{a}_{b}_{d}")
                model.AddBoolOr(same_shift_bools).OnlyEnforceIf(any_same)
                model.AddBoolAnd([b.Not() for b in same_shift_bools]).OnlyEnforceIf(any_same.Not())
                penalties.append((any_same, SEPARATION_WEIGHT))

        # Rule 11: Night fairness — max difference ≤4
        NIGHT_FAIR_WEIGHT = CONSTRAINT_WEIGHTS["night_fairness"]
        fair_emps = [e for e in self.emp_ids if e != self.head_nurse_id]
        if len(fair_emps) >= 2:
            night_vars = {}
            for e in fair_emps:
                nv = model.NewIntVar(0, self.num_days, f"hn_nights_{e}")
                model.Add(nv == sum(self.x[e][d]["N"] for d in self.days))
                night_vars[e] = nv
            night_list = list(night_vars.values())
            if len(night_list) >= 2:
                max_nights = model.NewIntVar(0, self.num_days, "max_nights")
                min_nights = model.NewIntVar(0, self.num_days, "min_nights")
                model.AddMaxEquality(max_nights, night_list)
                model.AddMinEquality(min_nights, night_list)
                night_diff = model.NewIntVar(0, self.num_days, "night_diff")
                model.Add(night_diff == max_nights - min_nights)
                # Penalize if > 5 (tolerance raised from 4 to 5)
                over_tol = model.NewBoolVar(f"night_over5")
                model.Add(night_diff > 5).OnlyEnforceIf(over_tol)
                model.Add(night_diff <= 5).OnlyEnforceIf(over_tol.Not())
                penalties.append((over_tol, NIGHT_FAIR_WEIGHT))

        # Rule 12: Evening fairness — max difference ≤4
        EVENING_FAIR_WEIGHT = CONSTRAINT_WEIGHTS["evening_fairness"]
        if "E" in self.shift_codes and len(fair_emps) >= 2:
            eve_vars = {}
            for e in fair_emps:
                ev = model.NewIntVar(0, self.num_days, f"he_eves_{e}")
                model.Add(ev == sum(self.x[e][d]["E"] for d in self.days))
                eve_vars[e] = ev
            eve_list = list(eve_vars.values())
            if len(eve_list) >= 2:
                max_eves = model.NewIntVar(0, self.num_days, "max_eves")
                min_eves = model.NewIntVar(0, self.num_days, "min_eves")
                model.AddMaxEquality(max_eves, eve_list)
                model.AddMinEquality(min_eves, eve_list)
                eve_diff = model.NewIntVar(0, self.num_days, "eve_diff")
                model.Add(eve_diff == max_eves - min_eves)
                # Penalize if > 4
                over_four_e = model.NewBoolVar(f"eve_over4")
                model.Add(eve_diff > 4).OnlyEnforceIf(over_four_e)
                model.Add(eve_diff <= 4).OnlyEnforceIf(over_four_e.Not())
                penalties.append((over_four_e, EVENING_FAIR_WEIGHT))

        # Rule 13: Breastfeeding non-overlap — entitled staff should NOT share
        # the same shift as far as possible (their reduced hour must not hit
        # one shift all at once). ALL days, fair and soft — never blocks.
        BREASTFEED_WEIGHT = CONSTRAINT_WEIGHTS["breastfeeding"]
        if self.breastfeeding_ids:
            bf_list = [eid for eid in self.breastfeeding_ids
                       if eid in self.x and eid != self.head_nurse_id]
            if len(bf_list) >= 2:
                for d in self.days:
                    for s in self.shift_codes:
                        cnt = model.NewIntVar(0, len(bf_list), f"bf_{d}_{s}")
                        model.Add(cnt == sum(self.x[e][d][s] for e in bf_list))
                        # penalize every BF nurse beyond the first on a shift
                        over = model.NewIntVar(0, len(bf_list), f"bf_over_{d}_{s}")
                        model.Add(over >= cnt - 1)
                        model.Add(over >= 0)
                        penalties.append((over, BREASTFEED_WEIGHT))

        # Rule 14: Shift continuity — replaced by hard block rules (B1-B5)
        # when the block system is active; sampled fallback otherwise.
        if self.block_system:
            # B-soft-1: isolated single OFF between two work days is discouraged
            # (rests should be 2 days when possible, never scattered 1-day OFFs).
            # Gated on the "consecutive OFFs" preference toggle.
            SINGLE_OFF_WEIGHT = CONSTRAINT_WEIGHTS["single_off"] \
                if self.pref_consecutive_offs else 0
            BLOCK_START_WEIGHT = CONSTRAINT_WEIGHTS["block_start"]
            for e in getattr(self, "_block_emps", []):
                X = self.x[e]
                leaves = self.leave_days.get(e, set())
                for d in self.days[1:-1]:
                    if SINGLE_OFF_WEIGHT == 0:
                        break
                    if any(w in leaves for w in (d - 1, d, d + 1)):
                        continue
                    iso = model.NewBoolVar(f"iso_off_{e}_{d}")
                    model.AddBoolAnd([X[d - 1][OFF].Not(), X[d][OFF],
                                      X[d + 1][OFF].Not()]).OnlyEnforceIf(iso)
                    model.AddBoolOr([X[d - 1][OFF], X[d][OFF].Not(),
                                     X[d + 1][OFF]]).OnlyEnforceIf(iso.Not())
                    penalties.append((iso, SINGLE_OFF_WEIGHT))
                # B-soft-2: prefer fewer, longer blocks (target 6 days):
                # each block start after day 1 carries a small cost
                for d in self.days[1:]:
                    st = model.NewBoolVar(f"blk_start_{e}_{d}")
                    model.AddBoolAnd([X[d][OFF].Not(),
                                      X[d - 1][OFF]]).OnlyEnforceIf(st)
                    model.AddBoolOr([X[d][OFF],
                                     X[d - 1][OFF].Not()]).OnlyEnforceIf(st.Not())
                    penalties.append((st, BLOCK_START_WEIGHT))
        else:
            CONTINUITY_WEIGHT = CONSTRAINT_WEIGHTS["continuity"]
            # Sample every 3rd day-pair from active range
            sample_pairs = []
            d = self.start_day
            while d + 1 <= self.end_day:
                sample_pairs.append((d, d + 1))
                d += 3
            for e in self.emp_ids:
                if e == self.head_nurse_id:
                    continue
                for (d1, d2) in sample_pairs:
                    # same_s = both days have shift s
                    same_bools = []
                    for s in self.shift_codes:
                        same_s = model.NewBoolVar(f"same_{e}_{d1}_{s}")
                        model.AddBoolAnd([self.x[e][d1][s], self.x[e][d2][s]]).OnlyEnforceIf(same_s)
                        model.AddBoolOr([self.x[e][d1][s].Not(), self.x[e][d2][s].Not()]).OnlyEnforceIf(same_s.Not())
                        same_bools.append(same_s)
                    # no_change = same shift OR either day is OFF
                    no_change_options = same_bools + [self.x[e][d1][OFF], self.x[e][d2][OFF]]
                    changed = model.NewBoolVar(f"chg_{e}_{d1}")
                    model.AddBoolOr(no_change_options).OnlyEnforceIf(changed.Not())
                    model.AddBoolAnd([b.Not() for b in no_change_options]).OnlyEnforceIf(changed)
                    penalties.append((changed, CONTINUITY_WEIGHT))

        # Rule 14b: Max 13 night shifts per employee per month (soft, weight 500)
        MAX_NIGHTS_PER_MONTH = 13
        MAX_NIGHTS_WEIGHT = CONSTRAINT_WEIGHTS["max_nights_month"]
        for e in self.emp_ids:
            if e == self.head_nurse_id:
                continue
            total_n = model.NewIntVar(0, self.num_days, f"total_nights_{e}")
            model.Add(total_n == sum(self.x[e][d]["N"] for d in self.days))
            over_n = model.NewIntVar(0, self.num_days, f"over_nights_{e}")
            model.Add(over_n >= total_n - MAX_NIGHTS_PER_MONTH)
            model.Add(over_n >= 0)
            penalties.append((over_n, MAX_NIGHTS_WEIGHT))

        # Rule 14c: Max 2 shift types per work block (soft, weight 1500)
        # A work block = consecutive work days between OFFs.
        # We check windows of 3-7 days: if all are working, at most 2 shift types.
        # SKIPPED under the block system: B1 already forces ONE type per block
        # as a hard rule, making these window checks redundant model bloat.
        BLOCK_TYPE_WEIGHT = CONSTRAINT_WEIGHTS["block_types"]
        rule14c_emps = [] if self.block_system else \
            [e for e in self.emp_ids if e != self.head_nurse_id]
        for e in rule14c_emps:
            for win_size in range(3, min(8, len(self.days) + 1)):
                for idx in range(len(self.days) - win_size + 1):
                    days_in_win = self.days[idx:idx + win_size]
                    if len(days_in_win) < win_size:
                        break
                    # all_work = 1 iff no OFF in this window
                    off_in_win = [self.x[e][dd][OFF] for dd in days_in_win]
                    all_work = model.NewBoolVar(f"aw_{e}_{days_in_win[0]}_{win_size}")
                    model.Add(sum(off_in_win) == 0).OnlyEnforceIf(all_work)
                    model.Add(sum(off_in_win) >= 1).OnlyEnforceIf(all_work.Not())
                    # has_M, has_E, has_N for the window
                    has_type = {}
                    for s in self.shift_codes:
                        ht = model.NewBoolVar(f"ht_{e}_{days_in_win[0]}_{win_size}_{s}")
                        shifts_in = [self.x[e][dd][s] for dd in days_in_win]
                        model.Add(sum(shifts_in) >= 1).OnlyEnforceIf(ht)
                        model.Add(sum(shifts_in) == 0).OnlyEnforceIf(ht.Not())
                        has_type[s] = ht
                    # If all working: has_M + has_E + has_N <= 2
                    type_sum = sum(has_type[s] for s in self.shift_codes)
                    over_types = model.NewBoolVar(f"ot_{e}_{days_in_win[0]}_{win_size}")
                    model.Add(type_sum > 2).OnlyEnforceIf(over_types)
                    model.Add(type_sum <= 2).OnlyEnforceIf(over_types.Not())
                    # Penalize only when all days are working AND >2 types
                    both_bad = model.NewBoolVar(f"bb_{e}_{days_in_win[0]}_{win_size}")
                    model.AddBoolAnd([all_work, over_types]).OnlyEnforceIf(both_bad)
                    model.AddBoolOr([all_work.Not(), over_types.Not()]).OnlyEnforceIf(both_bad.Not())
                    penalties.append((both_bad, BLOCK_TYPE_WEIGHT))

        # Rule 22: Certificate coverage — each certificate held by staff should
        # have ≥1 holder on every shift, every day. Driven by the employee
        # DATABASE (multi-certificate junction table). SOFT ONLY (weight 600):
        # the solver balances all certificates as far as holder counts allow —
        # it can never make the schedule infeasible.
        CERT_WEIGHT = CONSTRAINT_WEIGHTS["cert_coverage"]
        for cert in self.required_certs:
            holders = [h for h in self.cert_holders.get(cert, [])
                       if h in self.x and h != self.head_nurse_id]
            if not holders:
                continue
            for d in self.days:
                for s in self.shift_codes:
                    on = [self.x[h][d][s] for h in holders]
                    has = model.NewBoolVar(f"cert_{cert}_{d}_{s}")
                    model.Add(sum(on) >= 1).OnlyEnforceIf(has)
                    model.Add(sum(on) == 0).OnlyEnforceIf(has.Not())
                    penalties.append((has.Not(), CERT_WEIGHT))

        # Rule 23: Trainer-trainee pairing (STRONG, weight 1800 — just below
        # staff_max). Every trainee, on every working day, should have at
        # least one trainer on the SAME shift. Soft so generation never blocks;
        # violations surface as warnings.
        TRAINEE_WEIGHT = CONSTRAINT_WEIGHTS["trainee_pairing"]
        valid_trainers = [t for t in self.trainer_ids if t in self.x]
        valid_trainees = [t for t in self.trainee_ids if t in self.x]
        if valid_trainees and valid_trainers:
            # trainer_present[d][s]: at least one trainer works shift s on day d
            trainer_present = {}
            for d in self.days:
                trainer_present[d] = {}
                for s in self.shift_codes:
                    tp = model.NewBoolVar(f"trainer_on_{d}_{s}")
                    on = [self.x[t][d][s] for t in valid_trainers]
                    model.Add(sum(on) >= 1).OnlyEnforceIf(tp)
                    model.Add(sum(on) == 0).OnlyEnforceIf(tp.Not())
                    trainer_present[d][s] = tp
            for tr in valid_trainees:
                tr_leave = self.leave_days.get(tr, set())
                for d in self.days:
                    if d in tr_leave:
                        continue
                    for s in self.shift_codes:
                        # violation: trainee works s while no trainer is on s
                        viol = model.NewBoolVar(f"trainee_alone_{tr}_{d}_{s}")
                        model.AddBoolAnd([self.x[tr][d][s],
                                          trainer_present[d][s].Not()]).OnlyEnforceIf(viol)
                        model.AddBoolOr([self.x[tr][d][s].Not(),
                                         trainer_present[d][s]]).OnlyEnforceIf(viol.Not())
                        penalties.append((viol, TRAINEE_WEIGHT))
        elif valid_trainees and not valid_trainers:
            self.warnings.append(
                f"⚠ {len(valid_trainees)} trainee(s) without any assigned trainer — "
                f"assign at least one trainer in Employee Data")

        # Rule 15: Shift dominance cap ≤70% (low weight — advisory only)
        DOMINANCE_WEIGHT = CONSTRAINT_WEIGHTS["dominance"]
        if len(fair_emps) >= 1:
            for e in fair_emps:
                leave_count = len(self.leave_days.get(e, set()))
                offs_target = self._nationality_offs_targets.get(e)
                off_estimate = offs_target if offs_target is not None else leave_count
                work_days = self.num_days - off_estimate
                if work_days <= 0:
                    continue
                cap = max(1, int(work_days * 0.70))
                for s in self.shift_codes:
                    shift_count = model.NewIntVar(0, self.num_days, f"dom_{e}_{s}")
                    model.Add(shift_count == sum(self.x[e][d][s] for d in self.days))
                    over_cap = model.NewBoolVar(f"dom_over_{e}_{s}")
                    model.Add(shift_count > cap).OnlyEnforceIf(over_cap)
                    model.Add(shift_count <= cap).OnlyEnforceIf(over_cap.Not())
                    penalties.append((over_cap, DOMINANCE_WEIGHT))

        return penalties

    # ─────────────────────────────────────────────────
    #  LAYER 3 — PREFERENCE SOFT CONSTRAINTS (GREEN)
    # ─────────────────────────────────────────────────
    def _add_layer3_preferences(self, model):
        """
        Add preference soft constraints with lower weight (≤100).
        These are skipped silently if violated.

        Rules:
          16. Full weekend OFF — at least 1 Fri+Sat OFF per employee (weight 100)
          17. Experience balance — balanced experience across shifts (weight 3)
          18. Arabic speaker balance — at least 1 per shift (weight 4)
          19. Personal shift preference — preferred shift per employee (weight 40)
          20. Forced shifts — specific employee on specific shift/day (weight 80)
          21. Forced weekend OFF — specific employee OFF on weekends (weight 80)
        """
        penalties = []

        # Rule 16: Full weekend OFF — at least 1 Fri+Sat OFF per employee.
        # Gated on the Preferences toggles (previously loaded but ignored):
        # pref_fair_weekend = master switch; the Saudi / non-Saudi toggles
        # restrict which group receives the preference.
        FULL_WE_WEIGHT = CONSTRAINT_WEIGHTS["weekend_off"]
        if self.weekend_pairs and self.pref_fair_weekend:
            for e in self.emp_ids:
                if e == self.head_nurse_id:
                    continue
                is_saudi = self.emp_map.get(e, {}).get("nationality") == "سعودي"
                if is_saudi and not self.pref_saudi_weekend:
                    continue
                if not is_saudi and not self.pref_nonsaudi_weekend:
                    continue
                pair_bools = []
                for (fri, sat) in self.weekend_pairs:
                    both = model.NewBoolVar(f"we_pair_{e}_{fri}")
                    model.AddBoolAnd([
                        self.x[e][fri][OFF], self.x[e][sat][OFF]
                    ]).OnlyEnforceIf(both)
                    model.AddBoolOr([
                        self.x[e][fri][OFF].Not(), self.x[e][sat][OFF].Not()
                    ]).OnlyEnforceIf(both.Not())
                    pair_bools.append(both)
                # Penalize if no full weekend
                no_full_we = model.NewBoolVar(f"no_full_we_{e}")
                model.Add(sum(pair_bools) == 0).OnlyEnforceIf(no_full_we)
                model.Add(sum(pair_bools) >= 1).OnlyEnforceIf(no_full_we.Not())
                penalties.append((no_full_we, FULL_WE_WEIGHT))

        # Rule 17: Experience balance across shifts (sampled)
        EXP_BALANCE_WEIGHT = CONSTRAINT_WEIGHTS["experience_balance"]
        if self.pref_exp_balance:
            # Sample every 7th day from the active range
            sample_days = self.days[::7]
            # Check if all employees have 0 experience
            has_exp = any(
                float(e.get("experience_years", 0) or 0) > 0
                for e in self.employees
            )
            if has_exp:
                for d in sample_days:
                    exp_per_shift = []
                    for s in self.shift_codes:
                        total_exp = model.NewIntVar(0, 10000, f"exp_{d}_{s}")
                        model.Add(total_exp == sum(
                            int(float(self.emp_map[e].get("experience_years", 0) or 0)) * self.x[e][d][s]
                            for e in self.emp_ids
                        ))
                        exp_per_shift.append(total_exp)
                    if len(exp_per_shift) >= 2:
                        max_exp = model.NewIntVar(0, 10000, f"max_exp_{d}")
                        min_exp = model.NewIntVar(0, 10000, f"min_exp_{d}")
                        model.AddMaxEquality(max_exp, exp_per_shift)
                        model.AddMinEquality(min_exp, exp_per_shift)
                        exp_diff = model.NewIntVar(0, 10000, f"exp_diff_{d}")
                        model.Add(exp_diff == max_exp - min_exp)
                        penalties.append((exp_diff, EXP_BALANCE_WEIGHT))

        # Rule 18: Arabic speaker PRESENCE — >=1 Arabic speaker on every shift,
        # every day (weight 800, all days — replaces the old sampled balance
        # that was effectively decorative at weight 4).
        ARABIC_PRESENCE_WEIGHT = CONSTRAINT_WEIGHTS["arabic_presence"]
        if self.pref_arabic_balance:
            arabic_ids = [e["id"] for e in self.employees
                          if e.get("arabic_speaker") == "نعم" and e["id"] in self.x]
            if arabic_ids:
                for d in self.days:
                    for s in self.shift_codes:
                        on = [self.x[e][d][s] for e in arabic_ids]
                        has_ar = model.NewBoolVar(f"ar_{d}_{s}")
                        model.Add(sum(on) >= 1).OnlyEnforceIf(has_ar)
                        model.Add(sum(on) == 0).OnlyEnforceIf(has_ar.Not())
                        penalties.append((has_ar.Not(), ARABIC_PRESENCE_WEIGHT))
            else:
                self.warnings.append(
                    "⚠ No Arabic-speaking staff — Arabic language coverage cannot be ensured")

        # Rule 19: Personal shift preference
        PREF_WEIGHT = CONSTRAINT_WEIGHTS["preferred_shift"]
        for e in self.emp_ids:
            emp_data = self.emp_map.get(e, {})
            pref = (emp_data.get("preferred_shift", "") or "").strip()
            if not pref or pref not in self.shift_codes:
                continue
            for d in self.days:
                not_pref = model.NewBoolVar(f"npref_{e}_{d}")
                model.Add(self.x[e][d][OFF] + self.x[e][d][pref] == 0).OnlyEnforceIf(not_pref)
                model.Add(self.x[e][d][OFF] + self.x[e][d][pref] >= 1).OnlyEnforceIf(not_pref.Not())
                penalties.append((not_pref, PREF_WEIGHT))

        # Rule 20: Forced specific-day shifts (soft fallback — skipped when the
        # hard version is active in Layer 1)
        FORCED_SHIFT_WEIGHT = CONSTRAINT_WEIGHTS["forced_shift"]
        forced_soft = {} if getattr(self, "_forced_hard", False) else self.forced_shifts
        for eid, day_map in forced_soft.items():
            if eid not in self.x:
                continue
            for d, allowed in day_map.items():
                if d not in self.x[eid] or not allowed:
                    continue
                viol = model.NewBoolVar(f"fshift_viol_{eid}_{d}")
                match_sum = sum(self.x[eid][d][s] for s in allowed if s in self.x[eid][d])
                model.Add(match_sum >= 1).OnlyEnforceIf(viol.Not())
                model.Add(match_sum == 0).OnlyEnforceIf(viol)
                penalties.append((viol, FORCED_SHIFT_WEIGHT))

        # Rule 21: Forced weekend OFF
        FORCED_WE_OFF_WEIGHT = CONSTRAINT_WEIGHTS["forced_weekend_off"]
        for eid in self.forced_weekend_emp_ids:
            if eid not in self.x:
                continue
            for d in self.weekend_days:
                if d in self.x[eid] and OFF in self.x[eid][d]:
                    viol = model.NewBoolVar(f"fwe_viol_{eid}_{d}")
                    model.Add(self.x[eid][d][OFF] == 1).OnlyEnforceIf(viol.Not())
                    model.Add(self.x[eid][d][OFF] == 0).OnlyEnforceIf(viol)
                    penalties.append((viol, FORCED_WE_OFF_WEIGHT))

        # Rule 24: Regeneration stability — tiny REWARD for keeping each cell
        # equal to the previously saved schedule of this month. Weight is a
        # tie-breaker (2) so it never overrides any real constraint, but among
        # equally good schedules the solver picks the one closest to what the
        # user already has — small constraint edits now produce small diffs.
        STABILITY_WEIGHT = CONSTRAINT_WEIGHTS["stability"]
        if self.prev_solution:
            for eid_str, days_dict in self.prev_solution.items():
                try:
                    e = int(eid_str)
                except (TypeError, ValueError):
                    continue
                if e not in self.x:
                    continue
                for d_str, prev_shift in days_dict.items():
                    try:
                        d = int(d_str)
                    except (TypeError, ValueError):
                        continue
                    if d not in self.x[e] or prev_shift not in self.x[e][d]:
                        continue
                    allowed = self.forced_shifts.get(e, {}).get(d)
                    if allowed and prev_shift not in allowed:
                        continue  # never reward keeping a cell the user re-forced
                    # negative weight in the minimized objective = reward
                    penalties.append((self.x[e][d][prev_shift], -STABILITY_WEIGHT))

        # [REMOVED] Shift Change rule — redundant with Rule 14 (continuity)
        # [REMOVED] Backward rotation N→E — covered by hard constraints (Rule 6)

        return penalties

    # ─────────────────────────────────────────────────
    #  OBJECTIVE
    # ─────────────────────────────────────────────────
    def _set_objective(self, model, penalties):
        """Minimize weighted sum of all penalty variables."""
        if not penalties:
            return
        obj_terms = []
        for var, weight in penalties:
            obj_terms.append(var * weight)
        model.Minimize(sum(obj_terms))

    # ─────────────────────────────────────────────────
    #  EXTRACT SOLUTION
    # ─────────────────────────────────────────────────
    def _extract_solution(self, solver):
        """Extract schedule from solver → {emp_id_str: {day_str: shift_code}}"""
        schedule = {}
        for e in self.emp_ids:
            schedule[str(e)] = {}
            for d in self.days:
                for s in self.all_options:
                    if solver.Value(self.x[e][d][s]) == 1:
                        schedule[str(e)][str(d)] = s
                        break
        self._generate_warnings(schedule)
        return schedule

    def _extract_last_shifts(self, schedule):
        """Extract the MONTH-END tail (last 7 calendar days) for next-month
        continuity. 7 days (> MAX_BLOCK) lets the next month derive the TRUE
        run length — the old 3-day snapshot allowed 9 consecutive nights
        across the boundary. Employees whose data does not cover the month
        end (partial range, no prior full month) are omitted: no fabricated
        tail is better than a wrong one."""
        last_day = calendar.monthrange(self.year, self.month)[1]
        tail_days = [str(d) for d in range(max(1, last_day - 6), last_day + 1)]
        last_shifts = {}
        for eid_str, days_dict in schedule.items():
            if all(t in days_dict for t in tail_days):
                last_shifts[eid_str] = {t: days_dict[t] for t in tail_days}
        return last_shifts

    def _range_off_target(self, e, t_full):
        """Adapt a MONTHLY OFF target to the generated day range. For a full
        month it is unchanged. For a partial range we subtract the OFFs the
        employee already has in the saved out-of-range days; without reliable
        saved data we prorate — applying the monthly quota verbatim to a
        short range made partial generation INFEASIBLE under block rules."""
        last_day = calendar.monthrange(self.year, self.month)[1]
        if self.start_day == 1 and self.end_day == last_day:
            return t_full
        leave_in_range = sum(1 for d in self.leave_days.get(e, set())
                             if self.start_day <= d <= self.end_day)
        prev = (self.prev_solution or {}).get(str(e))
        outside = [d for d in range(1, last_day + 1)
                   if d < self.start_day or d > self.end_day]
        if prev and all(str(d) in prev for d in outside):
            offs_out = sum(1 for d in outside if prev[str(d)] == OFF)
            t = t_full - offs_out
        else:
            t = round(t_full * self.num_days / last_day)
        return max(0, min(max(t, leave_in_range), self.num_days))

    def _merge_with_saved(self, schedule):
        """Partial-range generation must MERGE into the previously saved
        month instead of replacing it (INSERT OR REPLACE used to destroy the
        out-of-range days and poison next month's continuity tail)."""
        last_day = calendar.monthrange(self.year, self.month)[1]
        if self.start_day == 1 and self.end_day == last_day:
            return schedule
        merged = {eid: dict(days) for eid, days in (self.prev_solution or {}).items()}
        for eid, days in schedule.items():
            merged.setdefault(eid, {}).update(days)
        return merged

    def _generate_warnings(self, schedule):
        """Check solution quality — GROUPED warnings (one line per issue type)."""
        # Staffing shortage — one line per shift type
        for s in self.shift_codes:
            short_days = []
            for d in self.days:
                count = sum(1 for e in self.emp_ids
                            if schedule.get(str(e), {}).get(str(d)) == s)
                if count < self.staff_min[s]:
                    short_days.append(f"{d}({count}/{self.staff_min[s]})")
            if short_days:
                shown = ", ".join(short_days[:8])
                more = f" +{len(short_days) - 8} more days" if len(short_days) > 8 else ""
                self.warnings.append(
                    f"⚠ Coverage shortage on shift {s}: {len(short_days)} day(s) — days {shown}{more}")

        # Incharge coverage — one line per shift type
        if self.incharge_ids:
            valid = [eid for eid in self.incharge_ids if str(eid) in schedule]
            for s in self.shift_codes:
                missing = [str(d) for d in self.days
                           if not any(schedule.get(str(e), {}).get(str(d)) == s
                                      for e in valid)]
                if missing:
                    shown = ", ".join(missing[:10])
                    more = f" +{len(missing) - 10}" if len(missing) > 10 else ""
                    self.warnings.append(
                        f"⚠ No shift incharge on shift {s}: {len(missing)} day(s) — days {shown}{more}")

        # Summary stats
        shift_counts = {}
        night_counts = {}
        off_counts = {}
        for e in self.emp_ids:
            eid_str = str(e)
            shifts = sum(1 for d in self.days if schedule[eid_str].get(str(d), OFF) != OFF)
            nights = sum(1 for d in self.days if schedule[eid_str].get(str(d)) == "N")
            offs = sum(1 for d in self.days if schedule[eid_str].get(str(d), OFF) == OFF)
            shift_counts[e] = shifts
            night_counts[e] = nights
            off_counts[e] = offs

        max_shifts = max(shift_counts.values()) if shift_counts else 0
        min_shifts = min(shift_counts.values()) if shift_counts else 0
        if max_shifts - min_shifts > 3:
            self.warnings.append(
                f"⚠ Shift distribution gap: {min_shifts}–{max_shifts} shifts per employee"
            )

        max_nights = max(night_counts.values()) if night_counts else 0
        min_nights = min(night_counts.values()) if night_counts else 0
        if max_nights - min_nights > 3:
            self.warnings.append(
                f"⚠ Night shift gap: {min_nights}–{max_nights} nights per employee"
            )

        # Forced shifts — one summary line, WITH the reason when detectable
        forced_misses = []
        for eid, day_map in self.forced_shifts.items():
            emp_name = self.emp_map.get(eid, {}).get("name", f"ID {eid}")
            for d, allowed in day_map.items():
                actual = schedule.get(str(eid), {}).get(str(d))
                if actual is not None and actual not in allowed:
                    # Diagnose WHY: staffing shortage on that day is the usual culprit
                    reason = ""
                    short = []
                    for s in self.shift_codes:
                        cnt = sum(1 for es in schedule
                                  if schedule[es].get(str(d)) == s)
                        if cnt < self.staff_min.get(s, 0):
                            short.append(f"{s} {cnt}/{self.staff_min.get(s, 0)}")
                    if short:
                        reason = f" — likely cause: coverage shortage on day {d} ({', '.join(short)})"
                    forced_misses.append(
                        f"{emp_name} day {d} (requested {'/'.join(allowed)} — got {actual}){reason}")
        if forced_misses:
            shown = "; ".join(forced_misses[:6])
            more = f" +{len(forced_misses) - 6}" if len(forced_misses) > 6 else ""
            self.warnings.append(
                f"⚠ Forced shifts not applied ({len(forced_misses)}): {shown}{more}")

        # Forced weekend OFF
        for eid in self.forced_weekend_emp_ids:
            emp_name = self.emp_map.get(eid, {}).get("name", f"ID {eid}")
            missed = []
            for d in self.weekend_days:
                actual = schedule.get(str(eid), {}).get(str(d))
                if actual is not None and actual != OFF:
                    missed.append(str(d))
            if missed:
                self.warnings.append(
                    f"⚠ Forced weekend OFF partial for {emp_name}: "
                    f"working on day(s) {', '.join(missed)}"
                )
