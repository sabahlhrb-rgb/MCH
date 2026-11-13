"""
database.py — إدارة قاعدة البيانات لنظام جدولة الموظفين
قسم واحد فقط — البيانات تُحفظ بجانب ملف الأداة
الكود (Code) لا يُخزّن — يُحسب تلقائياً من ترتيب الموظفين

Improvements:
  - Context manager support (__enter__ / __exit__)
  - Connection timeout (10s) to prevent UI freezing
  - PRAGMA foreign_keys = ON for referential integrity
  - Specific exception handling (sqlite3.OperationalError, etc.)
  - backup_database() for safe data backup before risky operations
  - Type hints on all public methods
"""

import sqlite3
import os
import json
import shutil
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def get_app_data_dir():
    """مسار حفظ البيانات — بجانب ملف الأداة"""
    import sys
    if getattr(sys, 'frozen', False):
        base = Path(os.path.dirname(sys.executable))
    else:
        base = Path(os.path.dirname(os.path.abspath(__file__)))
    data_dir = base / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir


def get_db_path():
    """مسار قاعدة البيانات"""
    return get_app_data_dir() / "department.db"


def backup_database(tag: str = "backup") -> Optional[Path]:
    """
    Create a timestamped backup of the database file.
    Returns the backup path on success, None on failure.

    Args:
        tag: Label for the backup file (e.g., "pre_migration", "manual")
    """
    db_path = get_db_path()
    if not db_path.exists():
        return None
    try:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = get_app_data_dir() / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup_path = backup_dir / f"department_{tag}_{timestamp}.db"
        shutil.copy2(str(db_path), str(backup_path))
        logger.info(f"Database backup created: {backup_path}")

        # Keep only last 10 backups to save disk space
        backups = sorted(backup_dir.glob("department_*.db"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-10]:
            try:
                old.unlink()
            except OSError:
                pass

        return backup_path
    except (OSError, shutil.Error) as e:
        logger.warning(f"Backup failed: {e}")
        return None


# ─── Connection timeout for all database connections ───
DB_TIMEOUT = 10  # seconds — prevents UI freeze on locked DB


def init_database() -> None:
    """إنشاء قاعدة البيانات وجداولها — مع ترحيل تلقائي من النسخ القديمة"""
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path), timeout=DB_TIMEOUT)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()

        # معلومات القسم
        cur.execute("""
            CREATE TABLE IF NOT EXISTS department_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                head_nurse TEXT NOT NULL DEFAULT '',
                nursing_director TEXT NOT NULL DEFAULT ''
            )
        """)

        cur.execute("SELECT COUNT(*) FROM department_info")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO department_info (name, head_nurse, nursing_director) VALUES ('', '', '')")

        # === ترحيل: إذا الجدول القديم فيه عمود code، نحذفه ===
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='employees'")
        table_exists = cur.fetchone()

        if table_exists:
            # فحص الأعمدة الموجودة
            cur.execute("PRAGMA table_info(employees)")
            columns = [col[1] for col in cur.fetchall()]

            if "code" in columns:
                # ترحيل: نقل البيانات للجدول الجديد بدون code
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS employees_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        nationality TEXT NOT NULL DEFAULT 'غير سعودي',
                        arabic_speaker TEXT NOT NULL DEFAULT 'نعم',
                        experience_years REAL NOT NULL DEFAULT 0,
                        age INTEGER NOT NULL DEFAULT 25,
                        advance_course TEXT NOT NULL DEFAULT '',
                        is_active INTEGER NOT NULL DEFAULT 1
                    )
                """)
                cur.execute("""
                    INSERT INTO employees_new (id, name, nationality, arabic_speaker, experience_years, age, is_active)
                    SELECT id, name, nationality, arabic_speaker, experience_years, age, is_active
                    FROM employees
                """)
                cur.execute("DROP TABLE employees")
                cur.execute("ALTER TABLE employees_new RENAME TO employees")
            else:
                # ترحيل: إضافة عمود advance_course إذا غير موجود
                if "advance_course" not in columns:
                    cur.execute("ALTER TABLE employees ADD COLUMN advance_course TEXT NOT NULL DEFAULT ''")
                if "preferred_shift" not in columns:
                    cur.execute("ALTER TABLE employees ADD COLUMN preferred_shift TEXT NOT NULL DEFAULT ''")
        else:
            # إنشاء الجدول لأول مرة
            cur.execute("""
                CREATE TABLE IF NOT EXISTS employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    nationality TEXT NOT NULL DEFAULT 'غير سعودي',
                    arabic_speaker TEXT NOT NULL DEFAULT 'نعم',
                    experience_years REAL NOT NULL DEFAULT 0,
                    age INTEGER NOT NULL DEFAULT 25,
                    advance_course TEXT NOT NULL DEFAULT '',
                    preferred_shift TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1
                )
            """)

        # === ترحيل: أعمدة جديدة (جنسية تفصيلية، دور تدريبي، ساعة رضاعة) ===
        cur.execute("PRAGMA table_info(employees)")
        emp_columns = [col[1] for col in cur.fetchall()]
        if "nationality_code" not in emp_columns:
            cur.execute("ALTER TABLE employees ADD COLUMN nationality_code TEXT NOT NULL DEFAULT ''")
        if "training_role" not in emp_columns:
            cur.execute("ALTER TABLE employees ADD COLUMN training_role TEXT NOT NULL DEFAULT ''")
            # legacy: TRAINER stored in advance_course by early test data
            cur.execute("UPDATE employees SET training_role='trainer' "
                        "WHERE UPPER(advance_course)='TRAINER'")
        if "breastfeeding" not in emp_columns:
            cur.execute("ALTER TABLE employees ADD COLUMN breastfeeding INTEGER NOT NULL DEFAULT 0")
            # migrate the old constraints-page list into the employee flag
            # (shift_settings may not exist yet on a brand-new database)
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shift_settings'")
            if cur.fetchone():
                cur.execute("SELECT setting_value FROM shift_settings "
                            "WHERE setting_key='breastfeeding_ids'")
                row = cur.fetchone()
                if row and row[0]:
                    try:
                        for bf_id in json.loads(row[0]):
                            cur.execute("UPDATE employees SET breastfeeding=1 WHERE id=?", (int(bf_id),))
                    except (ValueError, TypeError):
                        pass

        # === الشهادات المتعددة: جدول ربط (موظفة ↔ شهادات) ===
        cur.execute("""
            CREATE TABLE IF NOT EXISTS employee_certificates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                certificate TEXT NOT NULL,
                UNIQUE(employee_id, certificate)
            )
        """)
        # ترحيل: الشهادة المفردة القديمة في advance_course → جدول الشهادات
        cur.execute("SELECT id, advance_course FROM employees "
                    "WHERE advance_course != '' AND UPPER(advance_course) NOT IN ('TRAINER', 'NONE')")
        for emp_id, cert in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO employee_certificates (employee_id, certificate) "
                        "VALUES (?, ?)", (emp_id, cert.strip()))
        # Backfill: السعودية → Saudi، وغير السعودية → Other (تُراجَع يدوياً لاحقاً).
        # Idempotent — يلتقط أيضاً أي صفوف مستوردة لاحقاً بدون كود جنسية.
        cur.execute("UPDATE employees SET nationality_code='Saudi' "
                    "WHERE nationality_code='' AND nationality='سعودي'")
        cur.execute("UPDATE employees SET nationality_code='Other' "
                    "WHERE nationality_code='' AND nationality!='سعودي'")
        # فرض الاتساق: كود الجنسية هو مصدر الحقيقة الوحيد للتصنيفين.
        # Other (وكل غير العربيات) = غير متحدثة عربية — دائماً.
        from constants import NATIONALITIES, derive_nationality_flags
        for code in NATIONALITIES:
            nat, arabic = derive_nationality_flags(code)
            cur.execute(
                "UPDATE employees SET nationality=?, arabic_speaker=? "
                "WHERE nationality_code=? AND (nationality!=? OR arabic_speaker!=?)",
                (nat, arabic, code, nat, arabic))

        # الجداول الشهرية المحفوظة
        cur.execute("""
            CREATE TABLE IF NOT EXISTS monthly_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                schedule_data TEXT NOT NULL,
                last_shifts TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(year, month)
            )
        """)

        # القيود
        cur.execute("""
            CREATE TABLE IF NOT EXISTS constraints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                constraint_type TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'mandatory',
                params TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)

        # إعدادات المناوبات
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shift_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_key TEXT NOT NULL UNIQUE,
                setting_value TEXT NOT NULL
            )
        """)

        conn.commit()
    finally:
        conn.close()


class DepartmentDB:
    """
    واجهة التعامل مع قاعدة البيانات

    Supports context manager protocol for safe resource cleanup:
        with DepartmentDB() as db:
            db.get_all_employees()
    """

    def __init__(self) -> None:
        init_database()
        self.db_path = get_db_path()
        self.conn = sqlite3.connect(str(self.db_path), timeout=DB_TIMEOUT)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.row_factory = sqlite3.Row

    # ── Context Manager ──
    def __enter__(self) -> "DepartmentDB":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        """Close database connection safely."""
        if self.conn:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    # ===== معلومات القسم =====
    def get_department_info(self) -> Dict[str, str]:
        """Retrieve department information (name, head nurse, nursing director)."""
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM department_info LIMIT 1")
            row = cur.fetchone()
            return dict(row) if row else {"name": "", "head_nurse": "", "nursing_director": ""}
        except sqlite3.OperationalError as e:
            logger.error(f"Failed to get department info: {e}")
            return {"name": "", "head_nurse": "", "nursing_director": ""}

    def save_department_info(self, name: str, head_nurse: str, nursing_director: str) -> None:
        """Save or update department information."""
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT id FROM department_info ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if row is None:
                cur.execute("""
                    INSERT INTO department_info (name, head_nurse, nursing_director)
                    VALUES (?, ?, ?)
                """, (name, head_nurse, nursing_director))
            else:
                cur.execute("""
                    UPDATE department_info SET name=?, head_nurse=?, nursing_director=?
                    WHERE id=?
                """, (name, head_nurse, nursing_director, row[0]))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to save department info: {e}")
            raise

    # ===== الموظفين =====
    def get_all_employees(self) -> List[Dict[str, Any]]:
        """جلب الموظفين النشطين — مرتبين: رئيسة القسم ← المناوبات بالخبرة ← البقية بالخبرة.
        الكود يُحسب تلقائياً 1, 2, 3, ... حسب هذا الترتيب."""
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM employees WHERE is_active=1 ORDER BY id")
            rows = [dict(row) for row in cur.fetchall()]

            # ── Build display order: Head Nurse → Incharges → rest by experience ──
            emp_map = {r["id"]: r for r in rows}

            hn_id_str = self.get_setting("head_nurse_id", "")
            hn_id = int(hn_id_str) if hn_id_str and hn_id_str.isdigit() else None

            si_raw = self.get_setting("shift_incharge_ids", "[]")
            try:
                import json as _json
                si_ids = _json.loads(si_raw or "[]")
            except (ValueError, TypeError):
                si_ids = []

            ordered = []
            added = set()

            # 1) Head Nurse first
            if hn_id and hn_id in emp_map:
                ordered.append(emp_map[hn_id])
                added.add(hn_id)

            # 2) Shift Incharges sorted by experience descending
            incharge_emps = [emp_map[sid] for sid in si_ids
                             if sid in emp_map and sid not in added]
            incharge_emps.sort(
                key=lambda e: float(e.get("experience_years", 0) or 0),
                reverse=True
            )
            for e in incharge_emps:
                ordered.append(e)
                added.add(e["id"])

            # 3) Rest sorted by experience descending
            rest = [r for r in rows if r["id"] not in added]
            rest.sort(
                key=lambda e: float(e.get("experience_years", 0) or 0),
                reverse=True
            )
            ordered.extend(rest)

            # Assign sequential codes
            for i, row in enumerate(ordered, start=1):
                row["code"] = i

            # Attach certificates (multi) from the junction table
            cert_map: Dict[int, List[str]] = {}
            cur.execute("SELECT employee_id, certificate FROM employee_certificates")
            for emp_id, cert in cur.fetchall():
                cert_map.setdefault(emp_id, []).append(cert)
            for row in ordered:
                row["certificates"] = sorted(cert_map.get(row["id"], []))

            return ordered
        except sqlite3.OperationalError as e:
            logger.error(f"Failed to fetch employees: {e}")
            return []

    def add_employee(self, name: str, nationality: str = "", arabic_speaker: str = "",
                     experience_years: float = 0, age: int = 25,
                     advance_course: str = "", preferred_shift: str = "",
                     nationality_code: str = "", training_role: str = "",
                     breastfeeding: int = 0, certificates: Optional[List[str]] = None,
                     **kwargs) -> Optional[int]:
        """إضافة موظف جديد — الكود تلقائي. Returns new employee ID or None on failure.
        عند تمرير nationality_code تُشتق حقول (سعودي/غير سعودي) و(متحدثة عربية) تلقائياً."""
        if nationality_code:
            from constants import derive_nationality_flags
            nationality, arabic_speaker = derive_nationality_flags(nationality_code)
        nationality = nationality or "غير سعودي"
        arabic_speaker = arabic_speaker or "نعم"
        try:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO employees (name, nationality, arabic_speaker,
                                       experience_years, age, advance_course, preferred_shift,
                                       nationality_code, training_role, breastfeeding)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, nationality, arabic_speaker, experience_years, age,
                  advance_course, preferred_shift, nationality_code,
                  training_role, 1 if breastfeeding else 0))
            new_id = cur.lastrowid
            if certificates:
                for cert in certificates:
                    cur.execute("INSERT OR IGNORE INTO employee_certificates "
                                "(employee_id, certificate) VALUES (?, ?)",
                                (new_id, str(cert).strip()))
            self.conn.commit()
            return new_id
        except sqlite3.IntegrityError as e:
            logger.error(f"Integrity error adding employee '{name}': {e}")
            return None
        except sqlite3.Error as e:
            logger.error(f"Failed to add employee '{name}': {e}")
            return None

    # أسماء الأعمدة المسموح بتحديثها — حماية من SQL injection
    ALLOWED_EMPLOYEE_COLUMNS = {
        "name", "nationality", "arabic_speaker", "nationality_code",
        "experience_years", "age", "advance_course", "preferred_shift", "is_active",
        "training_role", "breastfeeding"
    }

    def set_employee_certificates(self, emp_id: int, certificates: List[str]) -> None:
        """استبدال شهادات موظفة بالقائمة المعطاة (مصدر الحقيقة: جدول الربط)."""
        try:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM employee_certificates WHERE employee_id=?", (emp_id,))
            for cert in certificates or []:
                c = str(cert).strip()
                if c and c.upper() != "NONE":
                    cur.execute("INSERT OR IGNORE INTO employee_certificates "
                                "(employee_id, certificate) VALUES (?, ?)", (emp_id, c))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to set certificates for employee {emp_id}: {e}")
            raise

    def update_employee(self, emp_id: int, **kwargs) -> None:
        """تحديث بيانات موظف — يتجاهل code تلقائياً. Only whitelisted columns are updated.
        عند تحديث nationality_code تُشتق حقول التصنيف تلقائياً."""
        if not kwargs:
            return
        kwargs.pop("code", None)
        if not kwargs:
            return
        # تصفية: فقط الأعمدة المسموح بها
        safe_kwargs = {k: v for k, v in kwargs.items()
                       if k in self.ALLOWED_EMPLOYEE_COLUMNS}
        if not safe_kwargs:
            return
        if safe_kwargs.get("nationality_code"):
            from constants import derive_nationality_flags
            nat, arabic = derive_nationality_flags(safe_kwargs["nationality_code"])
            safe_kwargs["nationality"] = nat
            safe_kwargs["arabic_speaker"] = arabic
        set_clause = ", ".join(f"{k}=?" for k in safe_kwargs)
        values = list(safe_kwargs.values()) + [emp_id]
        try:
            cur = self.conn.cursor()
            cur.execute(f"UPDATE employees SET {set_clause} WHERE id=?", values)
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to update employee {emp_id}: {e}")
            raise

    def delete_employee(self, emp_id: int) -> None:
        """حذف نهائي كامل — يحذف الموظف وكل ارتباطاته من قاعدة البيانات"""
        try:
            cur = self.conn.cursor()

            # 1) حذف الموظف من جدول employees + شهاداتها
            cur.execute("DELETE FROM employees WHERE id=?", (emp_id,))
            cur.execute("DELETE FROM employee_certificates WHERE employee_id=?", (emp_id,))

            # 2) تنظيف الإعدادات: رئيسة القسم
            hn = self.get_setting("head_nurse_id", "")
            if hn and hn.isdigit() and int(hn) == emp_id:
                self.set_setting("head_nurse_id", "")

            # 3) تنظيف الإعدادات: المناوبات
            si_raw = self.get_setting("shift_incharge_ids", "[]")
            try:
                si_ids = json.loads(si_raw or "[]")
                if emp_id in si_ids:
                    si_ids.remove(emp_id)
                    self.set_setting("shift_incharge_ids", json.dumps(si_ids))
            except (json.JSONDecodeError, TypeError):
                pass

            # 4) تنظيف الإعدادات: الرضاعة الطبيعية
            bf_raw = self.get_setting("breastfeeding_ids", "[]")
            try:
                bf_ids = json.loads(bf_raw or "[]")
                if emp_id in bf_ids:
                    bf_ids.remove(emp_id)
                    self.set_setting("breastfeeding_ids", json.dumps(bf_ids))
            except (json.JSONDecodeError, TypeError):
                pass

            # 5) حذف القيود المرتبطة (إقران / فصل / إجازات / فترات إجبارية)
            all_constraints = self.get_constraints()
            for c in all_constraints:
                p = c.get("params", {})
                related = False
                # قيود فردية (إجازة)
                if p.get("employee_id") == emp_id:
                    related = True
                # قيود ثنائية (إقران / فصل)
                emp_ids_list = p.get("employee_ids", []) or []
                if emp_id in emp_ids_list:
                    related = True
                if related:
                    cur.execute("DELETE FROM constraints WHERE id=?", (c["id"],))

            # 6) تنظيف الجداول المحفوظة — إزالة بيانات الموظف منها
            cur.execute("SELECT id, schedule_data, last_shifts FROM monthly_schedules")
            for row in cur.fetchall():
                try:
                    sched = json.loads(row[1]) if row[1] else {}
                    lshifts = json.loads(row[2]) if row[2] else {}
                    eid_str = str(emp_id)
                    changed = False
                    if eid_str in sched:
                        del sched[eid_str]
                        changed = True
                    if eid_str in lshifts:
                        del lshifts[eid_str]
                        changed = True
                    if changed:
                        cur.execute(
                            "UPDATE monthly_schedules SET schedule_data=?, last_shifts=? WHERE id=?",
                            (json.dumps(sched, ensure_ascii=False),
                             json.dumps(lshifts, ensure_ascii=False), row[0])
                        )
                except (json.JSONDecodeError, TypeError):
                    pass

            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to delete employee {emp_id}: {e}")
            raise

    def get_next_code(self) -> int:
        """الكود التالي"""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM employees WHERE is_active=1")
        return cur.fetchone()[0] + 1

    def import_employees_bulk(self, employees_list: List[Dict[str, Any]]) -> int:
        """استيراد مجموعة موظفين. Returns count of successfully imported employees."""
        cur = self.conn.cursor()
        success_count = 0
        for emp in employees_list:
            try:
                cur.execute("""
                    INSERT INTO employees
                    (name, nationality, arabic_speaker, experience_years, age, advance_course)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    emp["name"],
                    emp.get("nationality", "غير سعودي"),
                    emp.get("arabic_speaker", "نعم"),
                    emp.get("experience_years", 0),
                    emp.get("age", 25),
                    emp.get("advance_course", "")
                ))
                success_count += 1
            except sqlite3.IntegrityError as e:
                logger.warning(f"Duplicate or integrity error importing {emp.get('name', '?')}: {e}")
            except sqlite3.Error as e:
                logger.error(f"Error importing {emp.get('name', '?')}: {e}")
        self.conn.commit()
        return success_count

    # ===== الإعدادات =====
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieve a setting value by key, or return default if not found."""
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT setting_value FROM shift_settings WHERE setting_key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else default
        except sqlite3.OperationalError as e:
            logger.error(f"Failed to get setting '{key}': {e}")
            return default

    def set_setting(self, key: str, value: Any) -> None:
        """Save or update a setting value."""
        try:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO shift_settings (setting_key, setting_value)
                VALUES (?, ?)
            """, (key, str(value)))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to set setting '{key}': {e}")
            raise

    # ===== القيود =====
    def add_constraint(self, constraint_type: str, category: str, params: Dict) -> Optional[int]:
        """Add a new constraint. Returns constraint ID or None on failure."""
        try:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO constraints (constraint_type, category, params)
                VALUES (?, ?, ?)
            """, (constraint_type, category, json.dumps(params, ensure_ascii=False)))
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.Error as e:
            logger.error(f"Failed to add constraint {constraint_type}: {e}")
            return None

    def get_constraints(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve active constraints, optionally filtered by category."""
        try:
            cur = self.conn.cursor()
            if category:
                cur.execute("SELECT * FROM constraints WHERE category=? AND is_active=1", (category,))
            else:
                cur.execute("SELECT * FROM constraints WHERE is_active=1")
            rows = cur.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                try:
                    d["params"] = json.loads(d["params"])
                except (json.JSONDecodeError, TypeError):
                    d["params"] = {}
                    logger.warning(f"Invalid JSON in constraint {d.get('id')}")
                result.append(d)
            return result
        except sqlite3.OperationalError as e:
            logger.error(f"Failed to get constraints: {e}")
            return []

    PAIR_CONSTRAINT_TYPES = ("pairing_mandatory", "separation_mandatory",
                             "pairing_preferential", "separation_preferential")

    def migrate_pair_constraint_ids(self) -> int:
        """
        Backfill stable employee IDs into legacy pairing/separation constraints
        that only carry "CODE. Name" labels (codes shift when staff change,
        which used to silently orphan these constraints).
        Returns the number of constraints migrated.
        """
        try:
            emps = self.get_all_employees()
            label_map = {f"{e['code']}. {e['name']}": e["id"] for e in emps}
            migrated = 0
            cur = self.conn.cursor()
            for c in self.get_constraints():
                if c["constraint_type"] not in self.PAIR_CONSTRAINT_TYPES:
                    continue
                p = c["params"]
                if p.get("employee_a_id") and p.get("employee_b_id"):
                    continue
                ida = label_map.get(p.get("employee_a", ""))
                idb = label_map.get(p.get("employee_b", ""))
                if ida and idb and ida != idb:
                    p["employee_a_id"] = ida
                    p["employee_b_id"] = idb
                    cur.execute("UPDATE constraints SET params=? WHERE id=?",
                                (json.dumps(p, ensure_ascii=False), c["id"]))
                    migrated += 1
            if migrated:
                self.conn.commit()
                logger.info(f"Migrated {migrated} pair constraints to ID storage")
            return migrated
        except sqlite3.Error as e:
            logger.error(f"Pair-constraint migration failed: {e}")
            return 0

    def update_constraint_params(self, constraint_id: int, params: Dict) -> None:
        """Replace the params JSON of an existing constraint."""
        try:
            cur = self.conn.cursor()
            cur.execute("UPDATE constraints SET params=? WHERE id=?",
                        (json.dumps(params, ensure_ascii=False), constraint_id))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to update constraint {constraint_id}: {e}")
            raise

    def delete_constraint(self, constraint_id: int) -> None:
        """Delete a constraint by ID."""
        try:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM constraints WHERE id=?", (constraint_id,))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to delete constraint {constraint_id}: {e}")
            raise

    # ===== الجداول الشهرية =====
    def save_schedule(self, year: int, month: int,
                      schedule_data: Dict, last_shifts: Dict) -> None:
        """Save or update the monthly schedule and last-shifts snapshot."""
        try:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO monthly_schedules (year, month, schedule_data, last_shifts)
                VALUES (?, ?, ?, ?)
            """, (year, month,
                  json.dumps(schedule_data, ensure_ascii=False),
                  json.dumps(last_shifts, ensure_ascii=False)))
            self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to save schedule {year}-{month}: {e}")
            raise

    def get_schedule(self, year: int, month: int) -> Optional[Dict[str, Any]]:
        """Retrieve a saved monthly schedule, or None if not found."""
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM monthly_schedules WHERE year=? AND month=?", (year, month))
            row = cur.fetchone()
            if row:
                d = dict(row)
                d["schedule_data"] = json.loads(d["schedule_data"])
                d["last_shifts"] = json.loads(d["last_shifts"]) if d["last_shifts"] else {}
                return d
            return None
        except sqlite3.OperationalError as e:
            logger.error(f"Failed to get schedule {year}-{month}: {e}")
            return None

    def get_previous_month_last_shifts(self, year: int, month: int) -> Dict:
        """Retrieve last-shifts data from the previous month for continuity."""
        prev_month = month - 1
        prev_year = year
        if prev_month < 1:
            prev_month = 12
            prev_year -= 1
        schedule = self.get_schedule(prev_year, prev_month)
        if schedule and schedule.get("last_shifts"):
            return schedule["last_shifts"]
        return {}
