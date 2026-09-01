#!/usr/bin/env python3
"""
Extract Student-related data from RavenDB, transform it to PostgreSQL schema,
and load into local PostgreSQL.

Before running: set all required configuration values in .env
(or pass them explicitly as command-line arguments).

Target tables:
- organization
- institute
- student

"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extras import Json
import requests

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass
class Config:
    raven_url: str
    raven_db: str
    raven_cert_file: Optional[str]
    raven_cert_password: Optional[str]
    raven_insecure: bool
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str
    org_collection: str
    institute_collection: str
    student_collection: str
    page_size: int
    timeout_sec: int
    summary_json_path: Optional[str]
    write_summary_json: bool
    include_api_payload_validation: bool


@dataclass
class UpsertResult:
    record_id: str
    inserted: bool


def load_env_file(env_path: str) -> None:
    """Load KEY=VALUE entries from .env into os.environ when missing."""
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and os.getenv(key) is None:
                os.environ[key] = value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_args() -> Config:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_env = os.path.join(script_dir, "..", ".env")
    if os.path.exists(root_env):
        load_env_file(root_env)

    parser = argparse.ArgumentParser(
        description="Migrate Student-related data from RavenDB to PostgreSQL"
    )
    parser.add_argument("--raven-url", default=os.getenv("RAVEN_URL"))
    parser.add_argument("--raven-db", default=os.getenv("RAVEN_DB"))
    parser.add_argument("--raven-cert-file", default=os.getenv("RAVEN_CERT_FILE"))
    parser.add_argument(
        "--raven-cert-password", default=os.getenv("RAVEN_CERT_PASSWORD")
    )
    parser.add_argument(
        "--raven-insecure",
        action="store_true",
        default=env_bool("RAVEN_INSECURE", False),
        help="Disable TLS certificate verification for RavenDB HTTPS (not for production).",
    )

    parser.add_argument("--pg-host", default=os.getenv("PG_HOST"))
    parser.add_argument("--pg-port", type=int, default=os.getenv("PG_PORT"))
    parser.add_argument("--pg-db", default=os.getenv("PG_DB"))
    parser.add_argument("--pg-user", default=os.getenv("PG_USER"))
    parser.add_argument("--pg-password", default=os.getenv("PG_PASSWORD"))

    parser.add_argument("--org-collection", default=os.getenv("ORG_COLLECTION"))
    parser.add_argument(
        "--institute-collection", default=os.getenv("INSTITUTE_COLLECTION")
    )
    parser.add_argument("--student-collection", default=os.getenv("STUDENT_COLLECTION"))

    parser.add_argument("--page-size", type=int, default=os.getenv("PAGE_SIZE"))
    parser.add_argument("--timeout-sec", type=int, default=os.getenv("TIMEOUT_SEC"))
    parser.add_argument(
        "--summary-json-path",
        default=os.getenv("MIGRATION_SUMMARY_JSON"),
        help=(
            "Optional output path for post-run JSON artifact. "
            "Default when omitted: validation/migration-summary-<timestamp>.json"
        ),
    )
    parser.add_argument(
        "--no-summary-json",
        action="store_true",
        help="Disable writing post-run summary JSON artifact.",
    )
    parser.add_argument(
        "--no-api-payload-validation",
        action="store_true",
        help="Disable API-shaped PostgreSQL payload generation in summary JSON.",
    )

    args = parser.parse_args()

    if not args.raven_url or not args.raven_db:
        parser.error("Missing RavenDB config. Provide --raven-url/--raven-db or set RAVEN_URL/RAVEN_DB.")

    if not args.pg_password:
        parser.error("Missing PostgreSQL password. Provide --pg-password or set PG_PASSWORD.")

    if not args.pg_host or args.pg_port is None or not args.pg_db or not args.pg_user:
        parser.error(
            "Missing PostgreSQL config. Provide --pg-host/--pg-port/--pg-db/--pg-user or set PG_HOST/PG_PORT/PG_DB/PG_USER."
        )

    if not args.org_collection or not args.institute_collection or not args.student_collection:
        parser.error(
            "Missing collection config. Provide --org-collection/--institute-collection/--student-collection "
            "or set ORG_COLLECTION/INSTITUTE_COLLECTION/STUDENT_COLLECTION."
        )

    if args.page_size is None:
        parser.error("Missing page size. Provide --page-size or set PAGE_SIZE.")

    if args.timeout_sec is None:
        parser.error("Missing timeout. Provide --timeout-sec or set TIMEOUT_SEC.")

    if args.page_size <= 0:
        parser.error("Invalid page size. --page-size must be greater than 0.")

    if args.timeout_sec <= 0:
        parser.error("Invalid timeout. --timeout-sec must be greater than 0.")

    if args.raven_cert_file:
        if not os.path.isfile(args.raven_cert_file):
            script_dir_cert = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.raven_cert_file)
            if os.path.isfile(script_dir_cert):
                args.raven_cert_file = script_dir_cert
            else:
                parser.error(f"Raven cert file not found: {args.raven_cert_file}")
    if args.raven_cert_file and not args.raven_url.lower().startswith("https://"):
        parser.error(
            "RAVEN_CERT_FILE requires an https:// RavenDB URL because certificate "
            "authentication uses mutual TLS."
        )

    return Config(
        raven_url=args.raven_url.rstrip("/"),
        raven_db=args.raven_db,
        raven_cert_file=args.raven_cert_file,
        raven_cert_password=args.raven_cert_password,
        raven_insecure=args.raven_insecure,
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        pg_db=args.pg_db,
        pg_user=args.pg_user,
        pg_password=args.pg_password,
        org_collection=args.org_collection,
        institute_collection=args.institute_collection,
        student_collection=args.student_collection,
        page_size=args.page_size,
        timeout_sec=args.timeout_sec,
        summary_json_path=args.summary_json_path,
        write_summary_json=not args.no_summary_json,
        include_api_payload_validation=not args.no_api_payload_validation,
    )


def write_summary_json(path_text: str, payload: Dict[str, Any]) -> str:
    """Write migration summary JSON artifact and return absolute path."""
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path.resolve())


def _page_meta(total_records: int, skip: int, top: int) -> Dict[str, int]:
    total_pages = (total_records + top - 1) // top if top > 0 else 0
    return {
        "totalRecords": total_records,
        "totalPages": total_pages,
        "currentPage": (skip // top) + 1 if top > 0 else 0,
        "recordsPerPage": top,
        "skip": skip,
        "top": top,
    }


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def format_iso_datetime_or_date(value: Any) -> Optional[str]:
    """Format date/datetime values from psycopg rows safely in UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (
            value.astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="microseconds")
            .rstrip("0")
            .rstrip(".")
            + "Z"
        )
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def iso_utc(value: Any) -> Optional[str]:
    """Format any datetime to UTC ISO string with trailing Z."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (
            value.astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="microseconds")
            .rstrip("0")
            .rstrip(".")
            + "Z"
        )
    return str(value)


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, (str, bytes)):
        text = str(value).strip()
        return [text] if text else []
    return [str(value)]


def status_code(value: Any) -> int:
    return {"Unknown": -1, "Active": 1, "Disabled": 99}.get(value, -1)


def gender_code(value: Any) -> int:
    return {"Female": 0, "Male": 1, "NoInfo": 90}.get(value, 90)


def parse_student_gender(value: Any) -> str:
    if value in ("Female", "Male", "NoInfo"):
        return str(value)
    return "NoInfo"


def parse_student_status(value: Any) -> str:
    if value in ("Unknown", "Active", "Disabled"):
        return str(value)
    return "Active"


def org_status_code(value: Any) -> int:
    return {
        "Unknown": -1,
        "ActivationPending": 0,
        "Active": 1,
        "Locked": 90,
        "Disabled": 99,
    }.get(value, -1)


def inst_status_code(value: Any) -> int:
    return {
        "Unknown": -1,
        "ActivationPending": 0,
        "Active": 1,
        "Locked": 90,
        "Disabled": 99,
    }.get(value, -1)


def edu_level_code(value: Any) -> int:
    return {
        "Unknown": -1,
        "PreNursery": 2,
        "Nursery": 5,
        "School": 10,
        "UnderGraduate": 20,
        "Graduate": 30,
        "PostGraduate": 40,
    }.get(value, -1)


def parse_organization_status(value: Any) -> str:
    if value in ("Unknown", "ActivationPending", "Active", "Locked", "Disabled"):
        return str(value)
    return "Active"


def parse_institute_status(value: Any) -> str:
    if value in ("Unknown", "ActivationPending", "Active", "Locked", "Disabled"):
        return str(value)
    return "Active"


def parse_edu_level(value: Any) -> Optional[str]:
    if value in (
        "Unknown",
        "PreNursery",
        "Nursery",
        "School",
        "UnderGraduate",
        "Graduate",
        "PostGraduate",
    ):
        return str(value)
    return None


def as_string_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x) for x in value if x is not None]
    return [str(value)]


ENROLLMENT_LATERAL_SQL = """
    JOIN LATERAL (
        SELECT
            e->>'CourseName' AS course_name,
            e->>'TermName' AS term_name,
            e->>'SectionName' AS section_name,
            e->>'RollNo' AS roll_no
        FROM jsonb_array_elements(
            COALESCE(s.enrollments, '[]'::jsonb)
        ) AS e
    ) se ON TRUE
"""


def _build_manage_where(params: Dict[str, Any]) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    values: List[Any] = []

    if params.get("courseName"):
        clauses.append("se.course_name = %s")
        values.append(params["courseName"])
    if params.get("section"):
        clauses.append("se.section_name = %s")
        values.append(params["section"])
    if params.get("term"):
        clauses.append("se.term_name = %s")
        values.append(params["term"])
    if params.get("activeOnly"):
        clauses.append("s.status = 'Active'")
    if params.get("search"):
        clauses.append(
            "(s.student_id ILIKE %s OR s.name ILIKE %s OR "
            "s.father_name ILIKE %s OR s.mother_name ILIKE %s OR "
            "s.email_csv ILIKE %s OR s.mobile_csv ILIKE %s)"
        )
        search_value = f"%{params['search']}%"
        values.extend([search_value] * 6)

    return ("WHERE " + " AND ".join(clauses)) if clauses else "", values


def build_student_manage_payload(
    cur: psycopg2.extensions.cursor, params: Dict[str, Any]
) -> Dict[str, Any]:
    sort_map = {
        "name": "s.name",
        "studentId": "s.student_id",
        "courseName": "se.course_name",
        "sectionName": "se.section_name",
        "rollNo": "se.roll_no",
    }

    sort_by = str(params.get("sortBy") or "name")
    sort_dir = str(params.get("sortDir") or "asc").lower()
    if sort_by not in sort_map:
        sort_by = "name"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"

    top = int(params.get("top") or 20)
    skip = int(params.get("skip") or 0)
    where_sql, where_values = _build_manage_where(params)

    count_sql = f"""
        SELECT COUNT(DISTINCT s.id)
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        {where_sql}
    """
    cur.execute(count_sql, where_values)
    total_records = int(cur.fetchone()[0])

    data_sql = f"""
        SELECT
            s.id,
            s.student_id,
            s.name,
            s.gender,
            s.status,
            se.course_name,
            se.term_name,
            se.section_name,
            se.roll_no,
            s.email_csv,
            s.mobile_csv
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        {where_sql}
        ORDER BY {sort_map[sort_by]} {sort_dir}, s.id
        LIMIT %s OFFSET %s
    """
    cur.execute(data_sql, where_values + [top, skip])
    rows = cur.fetchall()

    return {
        "request": params,
        "response": {
            "Data": [
                {
                    "Id": str(r[0]) if r[0] is not None else None,
                    "StudentId": r[1],
                    "Name": r[2],
                    "Gender": gender_code(r[3]),
                    "Status": status_code(r[4]),
                    "CourseName": r[5],
                    "TermName": r[6],
                    "SectionName": r[7],
                    "RollNo": r[8],
                    "Email": _split_csv(r[9]),
                    "Mobile": _split_csv(r[10]),
                }
                for r in rows
            ],
            "Meta": _page_meta(total_records, skip, top),
        },
    }


def build_coursewise_breakup_payload(
    cur: psycopg2.extensions.cursor, params: Dict[str, Any]
) -> Dict[str, Any]:
    where_sql = ""
    where_values: List[Any] = []
    if params.get("courseName"):
        where_sql = "WHERE se.course_name = %s"
        where_values.append(params["courseName"])

    sql = f"""
        SELECT
            se.course_name,
            se.section_name,
            COUNT(*) FILTER (WHERE s.gender = 'Male') AS boys,
            COUNT(*) FILTER (WHERE s.gender = 'Female') AS girls,
            COUNT(*) AS total
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        {where_sql}
        GROUP BY se.course_name, se.section_name
        ORDER BY se.section_name
    """
    cur.execute(sql, where_values)
    rows = cur.fetchall()
    return {
        "request": params,
        "response": {
            "Data": [
                {
                    "CourseName": r[0],
                    "SectionName": r[1],
                    "Boys": int(r[2] or 0),
                    "Girls": int(r[3] or 0),
                    "Total": int(r[4] or 0),
                }
                for r in rows
            ],
            "Count": len(rows),
        },
    }


def build_sectionwise_breakup_payload(
    cur: psycopg2.extensions.cursor, params: Dict[str, Any]
) -> Dict[str, Any]:
    top = int(params.get("top") or 20)
    skip = int(params.get("skip") or 0)
    sort_dir = str(params.get("sortDir") or "asc").lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"

    clauses = ["1=1"]
    values: List[Any] = []
    if params.get("courseName"):
        clauses.append("se.course_name = %s")
        values.append(params["courseName"])
    if params.get("section"):
        clauses.append("se.section_name = %s")
        values.append(params["section"])
    if params.get("term"):
        clauses.append("se.term_name = %s")
        values.append(params["term"])
    if params.get("search"):
        clauses.append("(s.student_id ILIKE %s OR s.name ILIKE %s)")
        search_value = f"%{params['search']}%"
        values.extend([search_value, search_value])

    where_sql = "WHERE " + " AND ".join(clauses)

    count_sql = f"""
        SELECT COUNT(*)
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        {where_sql}
    """
    cur.execute(count_sql, values)
    total_records = int(cur.fetchone()[0])

    data_sql = f"""
        SELECT se.roll_no, s.student_id, s.name, s.gender, s.email_csv, s.mobile_csv
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        {where_sql}
        ORDER BY se.roll_no {sort_dir}, s.name {sort_dir}
        LIMIT %s OFFSET %s
    """
    cur.execute(data_sql, values + [top, skip])
    rows = cur.fetchall()

    return {
        "request": params,
        "response": {
            "Data": [
                {
                    "RollNo": r[0],
                    "StudentId": r[1],
                    "Name": r[2],
                    "Gender": gender_code(r[3]),
                    "Email": _split_csv(r[4]),
                    "Mobile": _split_csv(r[5]),
                }
                for r in rows
            ],
            "Meta": _page_meta(total_records, skip, top),
        },
    }


def build_parent_details_payload(
    cur: psycopg2.extensions.cursor, params: Dict[str, Any]
) -> Dict[str, Any]:
    clauses = ["1=1"]
    values: List[Any] = []
    if params.get("courseName"):
        clauses.append("se.course_name = %s")
        values.append(params["courseName"])
    if params.get("section"):
        clauses.append("se.section_name = %s")
        values.append(params["section"])

    sql = f"""
        SELECT s.name, s.father_name
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        WHERE {' AND '.join(clauses)}
        ORDER BY s.name
    """
    cur.execute(sql, values)
    rows = cur.fetchall()

    return {
        "request": params,
        "response": {
            "Data": [
                {
                    "StudentName": r[0],
                    "FatherName": "" if r[1] is None else str(r[1]).strip(),
                }
                for r in rows
            ],
            "Count": len(rows),
        },
    }


def build_new_admissions_payload(
    cur: psycopg2.extensions.cursor, params: Dict[str, Any]
) -> Dict[str, Any]:
    course_names = params.get("courseNames")
    if isinstance(course_names, str):
        course_names = [x.strip() for x in course_names.split(",") if x.strip()]
    if not isinstance(course_names, list):
        course_names = []

    where_sql = ""
    values: List[Any] = []
    if course_names:
        where_sql = "WHERE se.course_name = ANY(%s)"
        values.append(course_names)

    sql = f"""
        SELECT s.student_id, s.name, s.gender, CONCAT(se.course_name, '-', se.section_name) AS section
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        {where_sql}
        ORDER BY s.student_id
    """
    cur.execute(sql, values)
    rows = cur.fetchall()

    return {
        "request": params,
        "response": {
            "Data": [
                {
                    "StudentId": r[0],
                    "Name": r[1],
                    "Gender": gender_code(r[2]),
                    "Section": r[3],
                }
                for r in rows
            ],
            "Count": len(rows),
        },
    }


def build_student_details_payload(
    cur: psycopg2.extensions.cursor, params: Dict[str, Any]
) -> Dict[str, Any]:
    top = int(params.get("top") or 20)
    skip = int(params.get("skip") or 0)
    where_sql = ""
    values: List[Any] = []
    if params.get("courseName"):
        where_sql = "WHERE se.course_name = %s"
        values.append(params["courseName"])

    count_sql = f"""
        SELECT COUNT(*)
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        {where_sql}
    """
    cur.execute(count_sql, values)
    total_records = int(cur.fetchone()[0])

    data_sql = f"""
        SELECT
            se.roll_no,
            s.student_id,
            s.first_name,
            s.last_name,
            se.course_name,
            se.section_name,
            s.dob,
            s.gender,
            s.mobile_csv,
            s.email_csv
        FROM student s
        {ENROLLMENT_LATERAL_SQL}
        {where_sql}
        ORDER BY se.roll_no, s.first_name, s.student_id
        LIMIT %s OFFSET %s
    """
    cur.execute(data_sql, values + [top, skip])
    rows = cur.fetchall()

    return {
        "request": params,
        "response": {
            "Data": [
                {
                    "RollNo": r[0],
                    "StudentId": r[1],
                    "FirstName": r[2],
                    "LastName": r[3],
                    "CourseName": r[4],
                    "SectionName": r[5],
                    "Dob": format_iso_datetime_or_date(r[6]),
                    "Gender": gender_code(r[7]),
                    "Mobile": _split_csv(r[8]),
                    "Email": _split_csv(r[9]),
                }
                for r in rows
            ],
            "Meta": _page_meta(total_records, skip, top),
        },
    }


def build_dashboard_payload(cur: psycopg2.extensions.cursor) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT
            COUNT(*) AS total_students,
            COUNT(*) FILTER (WHERE status = 'Active') AS active_students,
            COUNT(*) FILTER (WHERE status <> 'Active' OR status IS NULL) AS inactive_students
        FROM student
        """
    )
    totals = cur.fetchone()

    cur.execute(
        """
        SELECT gender, COUNT(*) AS total
        FROM student
        WHERE status = 'Active'
        GROUP BY gender
        ORDER BY total DESC
        """
    )
    rows = cur.fetchall()

    return {
        "response": {
            "Data": {
                "Totals": {
                    "TotalStudents": int(totals[0]),
                    "ActiveStudents": int(totals[1]),
                    "InactiveStudents": int(totals[2]),
                },
                "GenderDistribution": [
                    {"Gender": gender_code(row[0]), "Total": int(row[1])} for row in rows
                ],
            }
        }
    }


def build_api_payload_validation(
    cur: psycopg2.extensions.cursor,
) -> Dict[str, Any]:
    manage_params = {
        "courseName": "I",
        "section": "A",
        "term": "Annual",
        "search": None,
        "sortBy": "name",
        "sortDir": "asc",
        "skip": 0,
        "top": 20,
        "activeOnly": False,
    }
    sectionwise_params = {
        "courseName": "I",
        "section": "A",
        "term": "Annual",
        "search": None,
        "sortDir": "asc",
        "skip": 0,
        "top": 20,
    }
    parent_params = {"courseName": "I", "section": "A"}
    new_admissions_params = {"courseNames": ["I"]}
    details_params = {"courseName": "I", "skip": 0, "top": 20}
    coursewise_params = {"courseName": "I"}

    return {
        "reference": {
            "note": "Validate this API-shaped JSON against DevTools/OpenAPI payloads.",
            "baseline_filters": {
                "courseName": "I",
                "section": "A",
                "term": "Annual",
            },
        },
        "endpoints": {
            "studentManage": build_student_manage_payload(cur, manage_params),
            "coursewiseBreakup": build_coursewise_breakup_payload(cur, coursewise_params),
            "sectionwiseBreakup": build_sectionwise_breakup_payload(cur, sectionwise_params),
            "studentParentDetails": build_parent_details_payload(cur, parent_params),
            "newAdmissions": build_new_admissions_payload(cur, new_admissions_params),
            "studentDetails": build_student_details_payload(cur, details_params),
            "dashboardSummary": build_dashboard_payload(cur),
        },
    }


def get_nested(doc: Dict[str, Any], *path: str) -> Any:
    """Safely read nested dict keys, returning None on any missing step."""
    cur: Any = doc
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def first_non_empty(*values: Any) -> Optional[Any]:
    """Return the first non-empty value (skips None and blank strings)."""
    for val in values:
        if val is None:
            continue
        if isinstance(val, str) and val.strip() == "":
            continue
        return val
    return None


def extract_uuid_from_any(value: Any) -> Optional[str]:
    """Extract the first UUID from any value and normalize to lowercase."""
    if value is None:
        return None
    text = str(value)
    match = UUID_RE.search(text)
    return match.group(0).lower() if match else None


def parse_ts(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    if "." in normalized:
        base, frac = normalized.split(".", 1)
        tz_pos = max(frac.find("+"), frac.find("-"))
        if tz_pos >= 0:
            frac_part = frac[:tz_pos]
            tz_part = frac[tz_pos:]
        else:
            frac_part = frac
            tz_part = ""
        digits = "".join(ch for ch in frac_part if ch.isdigit())
        if len(digits) > 6:
            digits = digits[:6]
        normalized = f"{base}.{digits}{tz_part}" if digits else f"{base}{tz_part}"
    if "+" not in normalized[10:] and "-" not in normalized[10:]:
        normalized = f"{normalized}+00:00"

    try:
        datetime.fromisoformat(normalized)
        return normalized
    except ValueError:
        return None


def parse_int(value: Any) -> Optional[int]:
    """Parse an integer-like value; return None when invalid or blank."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def as_json(value: Any) -> Optional[Json]:
    """Wrap values for JSONB writes while preserving SQL NULL semantics."""
    if value is None:
        return None
    return Json(value)


def raven_query_collection(
    session: requests.Session, cfg: Config, collection_name: str
) -> List[Dict[str, Any]]:
    """Read an entire Raven collection using deterministic offset pagination."""
    url = f"{cfg.raven_url}/databases/{cfg.raven_db}/queries"
    start = 0
    docs: List[Dict[str, Any]] = []

    escaped_collection = collection_name.replace("\\", "\\\\").replace('"', '\\"')

    while True:
        payload = {
            # Stable ordering makes offset paging deterministic; freeze source
            # writes for the migration window to guarantee a consistent snapshot.
            "Query": f'from "{escaped_collection}" order by id()',
            "Start": start,
            "PageSize": cfg.page_size,
        }
        resp = session.post(url, json=payload, timeout=cfg.timeout_sec)
        resp.raise_for_status()

        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError(f"Unexpected RavenDB response for {collection_name}")
        results = body.get("Results", [])
        if not isinstance(results, list):
            raise RuntimeError(f"Unexpected RavenDB response for {collection_name}")

        if any(not isinstance(result, dict) for result in results):
            raise RuntimeError(f"RavenDB returned a non-document result for {collection_name}")

        if not results:
            break

        docs.extend(results)
        start += len(results)

    return docs


def configure_raven_session(session: requests.Session, cfg: Config) -> None:
    """Configure RavenDB TLS verification and optional PKCS#12 client auth."""
    if cfg.raven_cert_file:
        try:
            from requests_pkcs12 import Pkcs12Adapter
        except ImportError as exc:
            raise RuntimeError(
                "PKCS#12 RavenDB authentication requires requests-pkcs12. "
                "Install dependencies with: "
                "python -m pip install -r scripts/python-connectivity/requirements.txt"
            ) from exc

        # A RavenDB client certificate is normally distributed as a password-
        # protected .pfx/.p12 bundle. The bundle contains the private key, so
        # no separate private-key path is needed or accepted by this script.
        session.mount(
            "https://",
            Pkcs12Adapter(
                pkcs12_filename=cfg.raven_cert_file,
                pkcs12_password=cfg.raven_cert_password or "",
            ),
        )

    if cfg.raven_insecure:
        session.verify = False
        print("Warning: RavenDB TLS verification is disabled (--raven-insecure).")


def ensure_target_schema(cur: psycopg2.extensions.cursor) -> None:
    """Create student-domain target tables and indexes with exact target schema."""
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'organization_status_enum') THEN
                CREATE TYPE organization_status_enum AS ENUM (
                    'Unknown',
                    'ActivationPending',
                    'Active',
                    'Locked',
                    'Disabled'
                );
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'institute_status_enum') THEN
                CREATE TYPE institute_status_enum AS ENUM (
                    'Unknown',
                    'ActivationPending',
                    'Active',
                    'Locked',
                    'Disabled'
                );
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'edu_level_enum') THEN
                CREATE TYPE edu_level_enum AS ENUM (
                    'Unknown',
                    'PreNursery',
                    'Nursery',
                    'School',
                    'UnderGraduate',
                    'Graduate',
                    'PostGraduate'
                );
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'student_gender_enum') THEN
                CREATE TYPE student_gender_enum AS ENUM (
                    'Female',
                    'Male',
                    'NoInfo'
                );
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'student_status_enum') THEN
                CREATE TYPE student_status_enum AS ENUM (
                    'Unknown',
                    'Active',
                    'Disabled'
                );
            END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS organization (
            id UUID PRIMARY KEY,
            name VARCHAR(200),
            short_name VARCHAR(32),
            status organization_status_enum,
            created_on TIMESTAMPTZ,
            modified_on TIMESTAMPTZ,
            website TEXT,
            address JSONB,
            sms_sender_id VARCHAR(16),
            email_sender_id VARCHAR(320),
            logo_url TEXT,
            is_group BOOLEAN,
            is_root BOOLEAN,
            modules JSONB,
            policy_name VARCHAR(100),
            enable_sms BOOLEAN,
            enable_email BOOLEAN,
            enable_notification BOOLEAN,
            edu_level edu_level_enum,
            read_only BOOLEAN,
            owner_id UUID,
            parent_id UUID,
            created_by UUID,
            modified_by UUID
        );

        CREATE TABLE IF NOT EXISTS institute (
            id UUID PRIMARY KEY,
            name VARCHAR(200),
            short_name VARCHAR(6),
            status institute_status_enum,
            created_on TIMESTAMPTZ,
            modified_on TIMESTAMPTZ,
            academic_year_from TIMESTAMPTZ,
            academic_year_to TIMESTAMPTZ,
            institute_code VARCHAR(32),
            registration_number VARCHAR(64),
            website TEXT,
            address JSONB,
            sms_sender_id VARCHAR(16),
            email_sender_id VARCHAR(320),
            logo_url TEXT,
            is_group BOOLEAN,
            is_root BOOLEAN,
            is_org BOOLEAN,
            modules JSONB,
            policy_name VARCHAR(100),
            course_order TEXT[],
            enable_sms BOOLEAN,
            enable_email BOOLEAN,
            enable_notification BOOLEAN,
            parental_access_enabled BOOLEAN,
            staff_access_enabled BOOLEAN,
            student_access_enabled BOOLEAN,
            edu_level edu_level_enum,
            read_only BOOLEAN,
            owner_id UUID,
            parent_id UUID,
            created_by UUID,
            modified_by UUID
        );

        CREATE TABLE IF NOT EXISTS student (
            id UUID PRIMARY KEY,
            student_id VARCHAR(32),
            name VARCHAR(200),
            first_name VARCHAR(100),
            middle_name VARCHAR(100),
            last_name VARCHAR(100),
            title VARCHAR(16),
            gender student_gender_enum,
            dob TIMESTAMPTZ,
            email VARCHAR(320),
            mobile VARCHAR(20),
            email_csv TEXT,
            mobile_csv TEXT,
            virtual_id VARCHAR(320),
            category VARCHAR(32),
            attendance JSONB,
            status student_status_enum,
            user_id UUID,
            inst_id UUID REFERENCES institute(id),
            father_name VARCHAR(200),
            mother_name VARCHAR(200),
            father JSONB,
            mother JSONB,
            guardian JSONB,
            aadhar_number CHAR(12),
            udid VARCHAR(32),
            domicile JSONB,
            fees_receivable JSONB,
            iep JSONB,
            documents JSONB,
            photo_url TEXT,
            contacts JSONB,
            addresses JSONB,
            tags TEXT[],
            attributes JSONB,
            occupations JSONB,
            pan CHAR(10),
            owner_id UUID,
            parent_id UUID,
            enrollments JSONB,
            created_on TIMESTAMPTZ,
            created_by UUID,
            modified_on TIMESTAMPTZ,
            modified_by UUID
        );
        """
    )


def assert_required_schema(cur: psycopg2.extensions.cursor) -> None:
    required_columns: Dict[str, Sequence[str]] = {
        "organization": (
            "id",
            "name",
            "short_name",
            "status",
            "created_on",
            "modified_on",
            "website",
            "address",
            "sms_sender_id",
            "email_sender_id",
            "logo_url",
            "is_group",
            "is_root",
            "modules",
            "policy_name",
            "enable_sms",
            "enable_email",
            "enable_notification",
            "edu_level",
            "read_only",
            "owner_id",
            "parent_id",
            "created_by",
            "modified_by",
        ),
        "institute": (
            "id",
            "name",
            "short_name",
            "status",
            "created_on",
            "modified_on",
            "academic_year_from",
            "academic_year_to",
            "institute_code",
            "registration_number",
            "website",
            "address",
            "sms_sender_id",
            "email_sender_id",
            "logo_url",
            "is_group",
            "is_root",
            "is_org",
            "modules",
            "policy_name",
            "course_order",
            "enable_sms",
            "enable_email",
            "enable_notification",
            "parental_access_enabled",
            "staff_access_enabled",
            "student_access_enabled",
            "edu_level",
            "read_only",
            "owner_id",
            "parent_id",
            "created_by",
            "modified_by",
        ),
        "student": (
            "id",
            "student_id",
            "name",
            "first_name",
            "middle_name",
            "last_name",
            "title",
            "gender",
            "dob",
            "email",
            "mobile",
            "email_csv",
            "mobile_csv",
            "virtual_id",
            "category",
            "attendance",
            "status",
            "inst_id",
            "father_name",
            "mother_name",
            "father",
            "mother",
            "guardian",
            "aadhar_number",
            "udid",
            "domicile",
            "fees_receivable",
            "iep",
            "documents",
            "photo_url",
            "contacts",
            "addresses",
            "tags",
            "attributes",
            "occupations",
            "pan",
            "owner_id",
            "parent_id",
            "enrollments",
            "created_on",
            "created_by",
            "modified_on",
            "modified_by",
        ),
    }
    required_types: Dict[str, Dict[str, Sequence[str]]] = {
        "organization": {
            "id": ("uuid",),
            "created_on": ("timestamp with time zone",),
            "modified_on": ("timestamp with time zone",),
            "status": ("user-defined", "organization_status_enum"),
            "address": ("jsonb",),
            "logo_url": ("text", "character varying"),
            "is_group": ("boolean",),
            "is_root": ("boolean",),
            "modules": ("jsonb",),
            "policy_name": ("character varying",),
            "enable_sms": ("boolean",),
            "enable_email": ("boolean",),
            "enable_notification": ("boolean",),
            "edu_level": ("user-defined", "edu_level_enum"),
            "read_only": ("boolean",),
            "owner_id": ("uuid",),
            "parent_id": ("uuid",),
            "created_by": ("uuid",),
            "modified_by": ("uuid",),
        },
        "institute": {
            "id": ("uuid",),
            "created_on": ("timestamp with time zone",),
            "modified_on": ("timestamp with time zone",),
            "status": ("user-defined", "institute_status_enum"),
            "academic_year_from": ("timestamp with time zone",),
            "academic_year_to": ("timestamp with time zone",),
            "institute_code": ("character varying",),
            "registration_number": ("character varying",),
            "address": ("jsonb",),
            "logo_url": ("text", "character varying"),
            "is_group": ("boolean",),
            "is_root": ("boolean",),
            "is_org": ("boolean",),
            "modules": ("jsonb",),
            "policy_name": ("character varying",),
            "course_order": ("array", "text[]"),
            "enable_sms": ("boolean",),
            "enable_email": ("boolean",),
            "enable_notification": ("boolean",),
            "parental_access_enabled": ("boolean",),
            "staff_access_enabled": ("boolean",),
            "student_access_enabled": ("boolean",),
            "edu_level": ("user-defined", "edu_level_enum"),
            "read_only": ("boolean",),
            "owner_id": ("uuid",),
            "parent_id": ("uuid",),
            "created_by": ("uuid",),
            "modified_by": ("uuid",),
        },
        "student": {
            "id": ("uuid",),
            "student_id": ("character varying",),
            "name": ("character varying",),
            "gender": ("user-defined", "student_gender_enum"),
            "status": ("user-defined", "student_status_enum"),
            "dob": ("timestamp with time zone",),
            "attendance": ("jsonb",),
            "inst_id": ("uuid",),
            "father": ("jsonb",),
            "mother": ("jsonb",),
            "guardian": ("jsonb",),
            "domicile": ("jsonb",),
            "fees_receivable": ("jsonb",),
            "iep": ("jsonb",),
            "documents": ("jsonb",),
            "photo_url": ("text", "character varying"),
            "contacts": ("jsonb",),
            "addresses": ("jsonb",),
            "tags": ("array", "text[]"),
            "attributes": ("jsonb",),
            "occupations": ("jsonb",),
            "owner_id": ("uuid",),
            "parent_id": ("uuid",),
            "enrollments": ("jsonb",),
            "created_on": ("timestamp with time zone",),
            "created_by": ("uuid",),
            "modified_on": ("timestamp with time zone",),
            "modified_by": ("uuid",),
        },
    }

    for table_name, columns in required_columns.items():
        cur.execute(
            """
            SELECT column_name, data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table_name,),
        )
        rows = cur.fetchall()
        existing = {row[0] for row in rows}
        type_by_column = {row[0]: str(row[1]).lower() for row in rows}
        udt_by_column = {row[0]: str(row[2]).lower() for row in rows}
        missing = [col for col in columns if col not in existing]
        if missing:
            raise RuntimeError(
                f"Table public.{table_name} is missing required columns: {', '.join(missing)}"
            )

        mismatches = []
        for column_name, expected_types in required_types.get(table_name, {}).items():
            actual_type = type_by_column.get(column_name)
            actual_udt = udt_by_column.get(column_name)
            if actual_type is None:
                continue
            if actual_type not in expected_types and actual_udt not in expected_types:
                mismatches.append(
                    f"{column_name} expected {', '.join(expected_types)} but found {actual_type} ({actual_udt})"
                )
        if mismatches:
            raise RuntimeError(
                f"Table public.{table_name} has datatype mismatches: {'; '.join(mismatches)}"
            )


def upsert_organization(
    cur: psycopg2.extensions.cursor, doc: Dict[str, Any]
) -> Optional[UpsertResult]:
    """Upsert one organization document keyed by id."""
    meta_id = get_nested(doc, "@metadata", "@id")

    org_id = first_non_empty(
        extract_uuid_from_any(doc.get("SourceOrgId")),
        extract_uuid_from_any(doc.get("OrgId")),
        extract_uuid_from_any(doc.get("Id")),
        extract_uuid_from_any(meta_id),
    )

    if not org_id:
        return None

    cur.execute("SELECT 1 FROM organization WHERE id = %s", (org_id,))
    is_new = cur.fetchone() is None

    cur.execute(
        """
        INSERT INTO organization (
            id,
            name,
            short_name,
            status,
            website,
            address,
            sms_sender_id,
            email_sender_id,
            logo_url,
            is_group,
            is_root,
            modules,
            policy_name,
            enable_sms,
            enable_email,
            enable_notification,
            edu_level,
            read_only,
            owner_id,
            parent_id,
            created_on,
            created_by,
            modified_on,
            modified_by
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (id)
        DO UPDATE SET
            name = EXCLUDED.name,
            short_name = EXCLUDED.short_name,
            status = EXCLUDED.status,
            website = EXCLUDED.website,
            address = EXCLUDED.address,
            sms_sender_id = EXCLUDED.sms_sender_id,
            email_sender_id = EXCLUDED.email_sender_id,
            logo_url = EXCLUDED.logo_url,
            is_group = EXCLUDED.is_group,
            is_root = EXCLUDED.is_root,
            modules = EXCLUDED.modules,
            policy_name = EXCLUDED.policy_name,
            enable_sms = EXCLUDED.enable_sms,
            enable_email = EXCLUDED.enable_email,
            enable_notification = EXCLUDED.enable_notification,
            edu_level = EXCLUDED.edu_level,
            read_only = EXCLUDED.read_only,
            owner_id = EXCLUDED.owner_id,
            parent_id = EXCLUDED.parent_id,
            created_on = EXCLUDED.created_on,
            created_by = EXCLUDED.created_by,
            modified_on = EXCLUDED.modified_on,
            modified_by = EXCLUDED.modified_by
        RETURNING id;
        """,
        (
            org_id,
            doc.get("Name"),
            doc.get("ShortName"),
            parse_organization_status(doc.get("Status")),
            doc.get("Website"),
            as_json(doc.get("Address")),
            doc.get("SMSSenderId"),
            doc.get("EmailSenderId"),
            doc.get("LogoUrl"),
            doc.get("IsGroup"),
            doc.get("IsRoot"),
            as_json(doc.get("Modules")),
            doc.get("PolicyName"),
            doc.get("EnableSMS"),
            doc.get("EnableEmail"),
            doc.get("EnableNotification"),
            parse_edu_level(doc.get("EduLevel")),
            doc.get("ReadOnly"),
            extract_uuid_from_any(doc.get("OwnerId")),
            extract_uuid_from_any(doc.get("ParentId")),
            parse_ts(doc.get("CreatedOn")),
            extract_uuid_from_any(doc.get("CreatedBy")),
            parse_ts(doc.get("ModifiedOn")),
            extract_uuid_from_any(doc.get("ModifiedBy")),
        ),
    )
    row = cur.fetchone()
    return UpsertResult(str(row[0]), is_new) if row else None


def upsert_institute(
    cur: psycopg2.extensions.cursor, doc: Dict[str, Any]
) -> Optional[UpsertResult]:
    """Upsert one institute document keyed by id."""
    meta_id = get_nested(doc, "@metadata", "@id")

    inst_id = first_non_empty(
        extract_uuid_from_any(doc.get("SourceInstId")),
        extract_uuid_from_any(doc.get("InstId")),
        extract_uuid_from_any(doc.get("Id")),
        extract_uuid_from_any(meta_id),
    )

    if not inst_id:
        return None

    cur.execute("SELECT 1 FROM institute WHERE id = %s", (inst_id,))
    is_new = cur.fetchone() is None

    short_name = doc.get("ShortName")
    if short_name is not None:
        short_name = str(short_name).strip()[:6]
        if not short_name:
            short_name = None

    cur.execute(
        """
        INSERT INTO institute (
            id,
            name,
            short_name,
            status,
            academic_year_from,
            academic_year_to,
            institute_code,
            registration_number,
            website,
            address,
            sms_sender_id,
            email_sender_id,
            logo_url,
            is_group,
            is_root,
            is_org,
            modules,
            policy_name,
            course_order,
            enable_sms,
            enable_email,
            enable_notification,
            parental_access_enabled,
            staff_access_enabled,
            student_access_enabled,
            edu_level,
            read_only,
            owner_id,
            parent_id,
            created_on,
            created_by,
            modified_on,
            modified_by
        )
        VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        ON CONFLICT (id)
        DO UPDATE SET
            name = EXCLUDED.name,
            short_name = EXCLUDED.short_name,
            status = EXCLUDED.status,
            academic_year_from = EXCLUDED.academic_year_from,
            academic_year_to = EXCLUDED.academic_year_to,
            institute_code = EXCLUDED.institute_code,
            registration_number = EXCLUDED.registration_number,
            website = EXCLUDED.website,
            address = EXCLUDED.address,
            sms_sender_id = EXCLUDED.sms_sender_id,
            email_sender_id = EXCLUDED.email_sender_id,
            logo_url = EXCLUDED.logo_url,
            is_group = EXCLUDED.is_group,
            is_root = EXCLUDED.is_root,
            is_org = EXCLUDED.is_org,
            modules = EXCLUDED.modules,
            policy_name = EXCLUDED.policy_name,
            course_order = EXCLUDED.course_order,
            enable_sms = EXCLUDED.enable_sms,
            enable_email = EXCLUDED.enable_email,
            enable_notification = EXCLUDED.enable_notification,
            parental_access_enabled = EXCLUDED.parental_access_enabled,
            staff_access_enabled = EXCLUDED.staff_access_enabled,
            student_access_enabled = EXCLUDED.student_access_enabled,
            edu_level = EXCLUDED.edu_level,
            read_only = EXCLUDED.read_only,
            owner_id = EXCLUDED.owner_id,
            parent_id = EXCLUDED.parent_id,
            created_on = EXCLUDED.created_on,
            created_by = EXCLUDED.created_by,
            modified_on = EXCLUDED.modified_on,
            modified_by = EXCLUDED.modified_by
        RETURNING id;
        """,
        (
            inst_id,
            doc.get("Name"),
            short_name,
            parse_institute_status(doc.get("Status")),
            parse_ts(doc.get("AcademicYearFrom")),
            parse_ts(doc.get("AcademicYearTo")),
            as_text(doc.get("InstituteCode")),
            as_text(doc.get("RegistrationNumber")),
            doc.get("Website"),
            as_json(doc.get("Address")),
            doc.get("SMSSenderId"),
            doc.get("EmailSenderId"),
            doc.get("LogoUrl"),
            doc.get("IsGroup"),
            doc.get("IsRoot"),
            doc.get("IsOrg"),
            as_json(doc.get("Modules")),
            doc.get("PolicyName"),
            as_string_list(doc.get("CourseOrder")),
            doc.get("EnableSMS"),
            doc.get("EnableEmail"),
            doc.get("EnableNotification"),
            doc.get("ParentalAccessEnabled"),
            doc.get("StaffAccessEnabled"),
            doc.get("StudentAccessEnabled"),
            parse_edu_level(doc.get("EduLevel")),
            doc.get("ReadOnly"),
            extract_uuid_from_any(doc.get("OwnerId")),
            extract_uuid_from_any(doc.get("ParentId")),
            parse_ts(doc.get("CreatedOn")),
            extract_uuid_from_any(doc.get("CreatedBy")),
            parse_ts(doc.get("ModifiedOn")),
            extract_uuid_from_any(doc.get("ModifiedBy")),
        ),
    )
    row = cur.fetchone()
    return UpsertResult(str(row[0]), is_new) if row else None


def derive_student_pk(doc: Dict[str, Any]) -> Optional[str]:
    """Derive unique student primary key from Raven document identity."""
    meta_id = get_nested(doc, "@metadata", "@id")

    # Preferred PK: Raven document UUID.
    meta_uuid = extract_uuid_from_any(meta_id)
    if meta_uuid:
        return meta_uuid

    # Fallback to explicit UUID-like identifiers if metadata does not carry one.
    return first_non_empty(
        extract_uuid_from_any(doc.get("Id")),
        extract_uuid_from_any(doc.get("SourceStudentId")),
        extract_uuid_from_any(doc.get("StudentId")),
    )


def derive_student_id(doc: Dict[str, Any]) -> Optional[str]:
    """Derive business student ID such as 23P001."""
    direct = first_non_empty(
        doc.get("SourceStudentId"),
        doc.get("StudentId"),
    )
    if direct:
        return str(direct)

    # Use an enrollment reference only when the source actually provides one.
    # Never substitute the Raven document UUID, Id, or admission number.
    enrollments = doc.get("Enrollments")
    if isinstance(enrollments, list):
        for enrollment in enrollments:
            if not isinstance(enrollment, dict):
                continue
            sid = first_non_empty(enrollment.get("StudentId"))
            if sid:
                return str(sid)

    return None


def derive_source_inst_id_from_student(doc: Dict[str, Any]) -> Optional[str]:
    """Find the first usable institute UUID from student enrollments."""
    enrollments = doc.get("Enrollments")
    if not isinstance(enrollments, list):
        return None

    for enrollment in enrollments:
        if not isinstance(enrollment, dict):
            continue
        source_inst_id = extract_uuid_from_any(enrollment.get("InstId"))
        if source_inst_id:
            return source_inst_id
    return None


def as_text(value: Any) -> Optional[str]:
    """Convert values to string while preserving None."""
    if value is None:
        return None
    return str(value)


def upsert_student(
    cur: psycopg2.extensions.cursor, doc: Dict[str, Any]
) -> Optional[UpsertResult]:
    """Upsert one student row keyed by student.id (Raven document UUID)."""
    student_pk = derive_student_pk(doc)
    if not student_pk:
        return None

    cur.execute("SELECT 1 FROM student WHERE id = %s", (student_pk,))
    is_new = cur.fetchone() is None

    student_id = derive_student_id(doc)

    source_inst_id = derive_source_inst_id_from_student(doc)

    inst_id = None
    if source_inst_id:
        cur.execute(
            "SELECT id FROM institute WHERE id = %s",
            (source_inst_id,),
        )
        found = cur.fetchone()
        if found:
            inst_id = found[0]

    father = doc.get("Father") if isinstance(doc.get("Father"), dict) else {}
    mother = doc.get("Mother") if isinstance(doc.get("Mother"), dict) else {}

    enrollments_json = as_json(doc.get("Enrollments"))

    cur.execute(
        """
        INSERT INTO student (
            id,
            student_id,
            name,
            first_name,
            middle_name,
            last_name,
            title,
            gender,
            dob,
            email,
            mobile,
            email_csv,
            mobile_csv,
            virtual_id,
            category,
            attendance,
            status,
            user_id,
            inst_id,
            father_name,
            mother_name,
            father,
            mother,
            guardian,
            aadhar_number,
            udid,
            domicile,
            fees_receivable,
            iep,
            documents,
            photo_url,
            contacts,
            addresses,
            tags,
            attributes,
            occupations,
            pan,
            owner_id,
            parent_id,
            enrollments,
            created_on,
            created_by,
            modified_on,
            modified_by
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        ON CONFLICT (id)
        DO UPDATE SET
            student_id = EXCLUDED.student_id,
            name = EXCLUDED.name,
            first_name = EXCLUDED.first_name,
            middle_name = EXCLUDED.middle_name,
            last_name = EXCLUDED.last_name,
            title = EXCLUDED.title,
            gender = EXCLUDED.gender,
            dob = EXCLUDED.dob,
            email = EXCLUDED.email,
            mobile = EXCLUDED.mobile,
            email_csv = EXCLUDED.email_csv,
            mobile_csv = EXCLUDED.mobile_csv,
            virtual_id = EXCLUDED.virtual_id,
            category = EXCLUDED.category,
            attendance = EXCLUDED.attendance,
            status = EXCLUDED.status,
            user_id = EXCLUDED.user_id,
            inst_id = EXCLUDED.inst_id,
            father_name = EXCLUDED.father_name,
            mother_name = EXCLUDED.mother_name,
            father = EXCLUDED.father,
            mother = EXCLUDED.mother,
            guardian = EXCLUDED.guardian,
            aadhar_number = EXCLUDED.aadhar_number,
            udid = EXCLUDED.udid,
            domicile = EXCLUDED.domicile,
            fees_receivable = EXCLUDED.fees_receivable,
            iep = EXCLUDED.iep,
            documents = EXCLUDED.documents,
            photo_url = EXCLUDED.photo_url,
            contacts = EXCLUDED.contacts,
            addresses = EXCLUDED.addresses,
            tags = EXCLUDED.tags,
            attributes = EXCLUDED.attributes,
            occupations = EXCLUDED.occupations,
            pan = EXCLUDED.pan,
            owner_id = EXCLUDED.owner_id,
            parent_id = EXCLUDED.parent_id,
            enrollments = EXCLUDED.enrollments,
            created_on = EXCLUDED.created_on,
            created_by = EXCLUDED.created_by,
            modified_on = EXCLUDED.modified_on,
            modified_by = EXCLUDED.modified_by
        RETURNING id;
        """,
        (
            student_pk,
            student_id,
            doc.get("Name"),
            doc.get("FirstName"),
            doc.get("MiddleName"),
            doc.get("LastName"),
            doc.get("Title"),
            parse_student_gender(doc.get("Gender")),
            parse_ts(doc.get("DOB")),
            doc.get("Email"),
            doc.get("Mobile"),
            doc.get("EmailCSV"),
            doc.get("MobileCSV"),
            doc.get("VirtualId"),
            doc.get("Category"),
            as_json(doc.get("Attendance")),
            parse_student_status(doc.get("Status")),
            extract_uuid_from_any(doc.get("UserId")),
            inst_id,
            father.get("Name"),
            mother.get("Name"),
            as_json(doc.get("Father")),
            as_json(doc.get("Mother")),
            as_json(doc.get("Guardian")),
            doc.get("AadharNumber"),
            doc.get("UDID"),
            as_json(doc.get("Domicile")),
            as_json(doc.get("FeesReceivable")),
            as_json(doc.get("IEP")),
            as_json(doc.get("Documents")),
            doc.get("PhotoUrl"),
            as_json(doc.get("Contacts")),
            as_json(doc.get("Addresses")),
            as_list(doc.get("Tags")),
            as_json(doc.get("Attributes")),
            as_json(doc.get("Occupations")),
            doc.get("PAN"),
            extract_uuid_from_any(doc.get("OwnerId")),
            extract_uuid_from_any(doc.get("ParentId")),
            enrollments_json,
            parse_ts(doc.get("CreatedOn")),
            extract_uuid_from_any(doc.get("CreatedBy")),
            parse_ts(doc.get("ModifiedOn")),
            extract_uuid_from_any(doc.get("ModifiedBy")),
        ),
    )

    row = cur.fetchone()
    return UpsertResult(str(row[0]), is_new) if row else None


def main() -> int:
    """Run the end-to-end migration for orgs, institutes, students, and enrollments."""
    cfg = parse_args()

    requests_session = requests.Session()
    conn = None
    try:
        configure_raven_session(requests_session, cfg)

        print(
            f"RavenDB target: url={cfg.raven_url}, db={cfg.raven_db}, "
            f"collections=({cfg.org_collection}, {cfg.institute_collection}, {cfg.student_collection})"
        )
        print("[1/5] Fetching RavenDB documents...")
        org_docs = raven_query_collection(requests_session, cfg, cfg.org_collection)
        inst_docs = raven_query_collection(requests_session, cfg, cfg.institute_collection)
        stu_docs = raven_query_collection(requests_session, cfg, cfg.student_collection)
        print(
            f"Fetched orgs={len(org_docs)}, institutes={len(inst_docs)}, students={len(stu_docs)}"
        )

        print("[2/5] Connecting PostgreSQL...")
        print(
            f"PostgreSQL target: host={cfg.pg_host}, port={cfg.pg_port}, "
            f"db={cfg.pg_db}, user={cfg.pg_user}"
        )
        conn = psycopg2.connect(
            host=cfg.pg_host,
            port=cfg.pg_port,
            dbname=cfg.pg_db,
            user=cfg.pg_user,
            password=cfg.pg_password,
        )
        conn.autocommit = True
        with conn.cursor() as tz_cur:
            tz_cur.execute("SET TIME ZONE 'UTC';")
        conn.autocommit = False

        loaded_orgs = 0
        loaded_insts = 0
        loaded_students = 0
        new_orgs = 0
        new_insts = 0
        new_students = 0
        deleted_students = 0

        with conn:
            with conn.cursor() as cur:
                print("[3/6] Ensuring target schema...")
                ensure_target_schema(cur)
                assert_required_schema(cur)

                print("[4/6] Upserting organizations...")
                for d in org_docs:
                    result = upsert_organization(cur, d)
                    if result is not None:
                        loaded_orgs += 1
                        new_orgs += int(result.inserted)

                print("[5/6] Upserting institutes...")
                for d in inst_docs:
                    result = upsert_institute(cur, d)
                    if result is not None:
                        loaded_insts += 1
                        new_insts += int(result.inserted)

                print("[6/6] Upserting students and enrollments...")
                for d in stu_docs:
                    result = upsert_student(cur, d)
                    if result is None:
                        continue
                    loaded_students += 1
                    new_students += int(result.inserted)

        # Post-load counts used by validation and operational sign-off.
        api_payload_validation: Optional[Dict[str, Any]] = None
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM organization")
            total_organizations = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM institute")
            total_institutes = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM student")
            total_students = int(cur.fetchone()[0])

            if cfg.include_api_payload_validation:
                api_payload_validation = build_api_payload_validation(cur)

        summary = {
            "generated_at_utc": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "source": {
                "raven_url": cfg.raven_url,
                "raven_db": cfg.raven_db,
                "org_collection": cfg.org_collection,
                "institute_collection": cfg.institute_collection,
                "student_collection": cfg.student_collection,
            },
            "target": {
                "pg_host": cfg.pg_host,
                "pg_port": cfg.pg_port,
                "pg_db": cfg.pg_db,
                "pg_user": cfg.pg_user,
            },
            "run_stats": {
                "organizations_processed": loaded_orgs,
                "new_organizations_inserted": new_orgs,
                "institutes_processed": loaded_insts,
                "new_institutes_inserted": new_insts,
                "students_processed": loaded_students,
                "new_students_inserted": new_students,
            },
            "post_load_counts": {
                "organization": total_organizations,
                "institute": total_institutes,
                "student": total_students,
            },
        }
        if api_payload_validation is not None:
            summary["api_payload_validation"] = api_payload_validation

        print("Migration completed.")
        print(f"organizations_processed: {loaded_orgs}")
        print(f"new_organizations_inserted: {new_orgs}")
        print(f"institutes_processed: {loaded_insts}")
        print(f"new_institutes_inserted: {new_insts}")
        print(f"students_processed: {loaded_students}")
        print(f"new_students_inserted: {new_students}")

        if cfg.write_summary_json:
            output_path = cfg.summary_json_path
            if not output_path:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                output_path = f"validation/migration-summary-{timestamp}.json"
            written = write_summary_json(output_path, summary)
            print(f"Summary JSON written: {written}")

        return 0

    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
        requests_session.close()


if __name__ == "__main__":
    raise SystemExit(main())
