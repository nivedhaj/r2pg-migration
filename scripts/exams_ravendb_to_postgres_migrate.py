"""
Extract Exams data from RavenDB and load it into one PostgreSQL exam table.

The RavenDB Exams document is an aggregate: exam header fields plus nested
ExamContents, Evaluation rows, LockHistory, AttendanceList, and RemarksList.
This script keeps that shape in one PostgreSQL row per exam. Searchable
top-level fields are stored as columns, and nested arrays are stored as JSONB
columns in the same exam table.

Before running: set all required configuration values in scripts/.env
(or pass them explicitly as command-line arguments).

Target table:
- exam
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
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence

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
    exams_collection: str
    page_size: int
    timeout_sec: int
    summary_json_path: Optional[str]
    write_summary_json: bool
    include_api_payload_validation: bool
    inspect_source_only: bool


@dataclass
class UpsertResult:
    id: str
    inserted: bool


def load_env_file(env_path: str) -> None:
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
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
        description="Migrate Exams data from RavenDB to one PostgreSQL exam table"
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

    parser.add_argument(
        "--exams-collection", default=os.getenv("EXAMS_COLLECTION", "Exams")
    )
    parser.add_argument("--page-size", type=int, default=os.getenv("PAGE_SIZE"))
    parser.add_argument("--timeout-sec", type=int, default=os.getenv("TIMEOUT_SEC"))
    parser.add_argument(
        "--summary-json-path",
        default=os.getenv("MIGRATION_SUMMARY_JSON"),
        help=(
            "Optional output path for post-run JSON artifact. "
            "Default when omitted: validation/exams-migration-summary-<timestamp>.json"
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
    parser.add_argument(
        "--inspect-source-only",
        action="store_true",
        help="Fetch RavenDB Exams and print source shape/counts without writing PostgreSQL.",
    )

    args = parser.parse_args()

    if not args.raven_url or not args.raven_db:
        parser.error(
            "Missing RavenDB config. Provide --raven-url/--raven-db or set RAVEN_URL/RAVEN_DB."
        )
    if not args.inspect_source_only:
        if not args.pg_password:
            parser.error(
                "Missing PostgreSQL password. Provide --pg-password or set PG_PASSWORD."
            )
        if (
            not args.pg_host
            or args.pg_port is None
            or not args.pg_db
            or not args.pg_user
        ):
            parser.error(
                "Missing PostgreSQL config. Provide --pg-host/--pg-port/--pg-db/--pg-user or set PG_HOST/PG_PORT/PG_DB/PG_USER."
            )
    if not args.exams_collection:
        parser.error(
            "Missing collection config. Provide --exams-collection or set EXAMS_COLLECTION."
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
            "RAVEN_CERT_FILE requires an https:// RavenDB URL because certificate authentication uses mutual TLS."
        )

    return Config(
        raven_url=args.raven_url.rstrip("/"),
        raven_db=args.raven_db,
        raven_cert_file=args.raven_cert_file,
        raven_cert_password=args.raven_cert_password,
        raven_insecure=args.raven_insecure,
        pg_host=args.pg_host or "",
        pg_port=args.pg_port or 0,
        pg_db=args.pg_db or "",
        pg_user=args.pg_user or "",
        pg_password=args.pg_password or "",
        exams_collection=args.exams_collection,
        page_size=args.page_size,
        timeout_sec=args.timeout_sec,
        summary_json_path=args.summary_json_path,
        write_summary_json=not args.no_summary_json,
        include_api_payload_validation=not args.no_api_payload_validation,
        inspect_source_only=args.inspect_source_only,
    )


def write_summary_json(path_text: str, payload: Dict[str, Any]) -> str:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path.resolve())


def to_camel_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "instId": row["inst_id"],
        "courseId": row["course_id"],
        "courseName": row["course_name"],
        "branch": row["branch"],
        "term": row["term"],
        "section": row["section"],
        "startDate": row["start_date"],
        "status": row["status"],
        "conductedOn": row["conducted_on"],
        "daysWorked": row["days_worked"],
        "totalMaxMarks": row["total_max_marks"],
        "lastLockedOn": row["last_locked_on"],
        "resultDate": row["result_date"],
    }


def iso_utc(value: Any) -> Optional[str]:
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


def decimal_to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def exam_status_code(value: Any) -> int:
    return {
        "Unknown": 0,
        "Active": 1,
        "Scheduled": 10,
        "Conducted": 20,
        "Locked": 90,
        "Disabled": 99,
    }.get(value, 0)


def parse_exam_status(value: Any) -> str:
    if value in ("Unknown", "Active", "Scheduled", "Conducted", "Locked", "Disabled"):
        return str(value)
    return "Active"


def build_exams_list_payload(
    cur: psycopg2.extensions.cursor, params: Dict[str, Any]
) -> Dict[str, Any]:
    top = int(params.get("recordsPerPage") or 256)
    current_page = int(params.get("currentPage") or 0)
    offset = current_page * top

    cur.execute("SELECT COUNT(*) FROM exam")
    total_records = int(cur.fetchone()[0])

    sql = """
        WITH course_lookup AS (
            SELECT DISTINCT ON (course_id)
                course_id,
                course_name,
                branch
            FROM (
                SELECT
                    c.id AS course_id,
                    c.name AS course_name,
                    c.branch
                FROM course c
                UNION ALL
                SELECT
                    (e.elem ->> 'CourseId')::uuid AS course_id,
                    e.elem ->> 'CourseName' AS course_name,
                    e.elem ->> 'Branch' AS branch
                FROM student s,
                     jsonb_array_elements(COALESCE(s.enrollments, '[]'::jsonb)) e(elem)
                WHERE NULLIF(e.elem ->> 'CourseId', '') IS NOT NULL
            ) combined
            WHERE course_id IS NOT NULL
            ORDER BY course_id, course_name NULLS LAST
        ),
        exam_projection AS (
            SELECT
                e.id::text AS id,
                e.name,
                e.inst_id::text AS inst_id,
                e.course_id::text AS course_id,
                cl.course_name,
                cl.branch,
                e.term,
                e.section,
                COALESCE(
                    (
                        SELECT (ec.elem ->> 'ScheduledOn')::timestamptz
                        FROM jsonb_array_elements(
                            COALESCE(e.exam_contents, '[]'::jsonb)
                        ) WITH ORDINALITY AS ec(elem, ord)
                        WHERE NULLIF(ec.elem ->> 'ScheduledOn', '') IS NOT NULL
                        ORDER BY ec.ord
                        LIMIT 1
                    ),
                    e.start_date
                ) AS start_date,
            e.status,
            (
                SELECT MAX((ec.elem ->> 'ConductedOn')::timestamptz)
                FROM jsonb_array_elements(
                    COALESCE(e.exam_contents, '[]'::jsonb)
                ) ec(elem)
                WHERE NULLIF(ec.elem ->> 'ConductedOn', '') IS NOT NULL
            ) AS conducted_on,
            e.days_worked,
            e.total_max_marks,
            (
                SELECT MAX((lh.elem ->> 'On')::timestamptz)
                FROM jsonb_array_elements(
                    COALESCE(e.lock_history, '[]'::jsonb)
                ) lh(elem)
                WHERE NULLIF(lh.elem ->> 'On', '') IS NOT NULL
            ) AS last_locked_on,
            e.result_date
        FROM exam e
        LEFT JOIN course_lookup cl
            ON cl.course_id = e.course_id
    )
    SELECT
        id,
        name,
        inst_id,
        course_id,
        course_name,
        branch,
        term,
        section,
        start_date,
        status,
        conducted_on,
        days_worked,
        total_max_marks,
        last_locked_on,
        result_date
    FROM exam_projection
    ORDER BY
        COALESCE(start_date, result_date) DESC NULLS LAST,
        name,
        id
    LIMIT %s OFFSET %s
    """
    cur.execute(sql, (top, offset))
    columns = [desc[0] for desc in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    data = []
    for row in rows:
        row["start_date"] = iso_utc(row["start_date"])
        row["status"] = exam_status_code(row["status"])
        row["conducted_on"] = iso_utc(row["conducted_on"])
        row["total_max_marks"] = decimal_to_float(row["total_max_marks"])
        row["last_locked_on"] = iso_utc(row["last_locked_on"])
        row["result_date"] = iso_utc(row["result_date"])
        data.append(to_camel_dict(row))

    total_pages = (total_records + top - 1) // top if top > 0 else 0
    return {
        "message": "Retrieved exams",
        "data": data,
        "meta": None,
        "createdOn": iso_utc(datetime.now(timezone.utc)),
        "requestUrl": None,
        "requestVerb": None,
        "pagedResults": False,
        "currentPage": current_page,
        "recordsPerPage": top,
        "totalRecords": total_records,
        "totalPages": total_pages,
    }


def build_api_payload_validation(
    cur: psycopg2.extensions.cursor,
) -> Dict[str, Any]:
    list_params = {
        "currentPage": 0,
        "recordsPerPage": 256,
    }
    return {
        "reference": {
            "note": "PostgreSQL-derived API-shaped payloads for exam read parity validation.",
        },
        "endpoints": {
            "examsList": {
                "request": list_params,
                "response": build_exams_list_payload(cur, list_params),
            }
        },
    }


def get_nested(doc: Dict[str, Any], *path: str) -> Any:
    cur: Any = doc
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def first_non_empty(*values: Any) -> Optional[Any]:
    for val in values:
        if val is None:
            continue
        if isinstance(val, str) and val.strip() == "":
            continue
        return val
    return None


def extract_uuid_from_any(value: Any) -> Optional[str]:
    if value is None:
        return None
    match = UUID_RE.search(str(value))
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
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def as_json(value: Any) -> Optional[Json]:
    if value is None:
        return None
    return Json(value)


def raven_query_collection(
    session: requests.Session, cfg: Config, collection_name: str
) -> List[Dict[str, Any]]:
    url = f"{cfg.raven_url}/databases/{cfg.raven_db}/queries"
    start = 0
    docs: List[Dict[str, Any]] = []
    escaped_collection = collection_name.replace("\\", "\\\\").replace('"', '\\"')

    while True:
        payload = {
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
            raise RuntimeError(
                f"RavenDB returned a non-document result for {collection_name}"
            )
        if not results:
            break
        docs.extend(results)
        start += len(results)

    return docs


def configure_raven_session(session: requests.Session, cfg: Config) -> None:
    if cfg.raven_cert_file:
        try:
            from requests_pkcs12 import Pkcs12Adapter
        except ImportError as exc:
            raise RuntimeError(
                "PKCS#12 RavenDB authentication requires requests-pkcs12. "
                "Install dependencies with: python -m pip install requests-pkcs12"
            ) from exc

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


def derive_exam_id(doc: Dict[str, Any]) -> Optional[str]:
    """Derive exam_id from Raven document identity or explicit source fields."""
    return first_non_empty(
        extract_uuid_from_any(get_nested(doc, "@metadata", "@id")),
        extract_uuid_from_any(doc.get("ExamId")),
        extract_uuid_from_any(doc.get("Id")),
    )


def ensure_target_schema(cur: psycopg2.extensions.cursor) -> None:
    """Create exam table and indexes with exact target schema."""
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'exam_status_enum') THEN
                CREATE TYPE exam_status_enum AS ENUM (
                    'Unknown',
                    'Active',
                    'Scheduled',
                    'Conducted',
                    'Locked',
                    'Disabled'
                );
            END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS exam (
            id UUID PRIMARY KEY,
            name VARCHAR(200),
            inst_id UUID,
            course_id UUID,
            term VARCHAR(64),
            section VARCHAR(32),
            exam_contents JSONB,
            lock_history JSONB,
            attendance_list JSONB,
            remarks_list JSONB,
            status exam_status_enum,
            days_worked INTEGER,
            total_max_marks NUMERIC(14, 2),
            merge_index INTEGER,
            start_date TIMESTAMPTZ,
            result_date TIMESTAMPTZ,
            owner_id UUID,
            parent_id UUID,
            created_on TIMESTAMPTZ,
            created_by UUID,
            modified_on TIMESTAMPTZ,
            modified_by UUID
        );
        """
    )


def assert_required_schema(cur: psycopg2.extensions.cursor) -> None:
    required_columns: Dict[str, Sequence[str]] = {
        "exam": (
            "id",
            "name",
            "inst_id",
            "course_id",
            "term",
            "section",
            "exam_contents",
            "lock_history",
            "attendance_list",
            "remarks_list",
            "status",
            "days_worked",
            "total_max_marks",
            "merge_index",
            "start_date",
            "result_date",
            "owner_id",
            "parent_id",
            "created_on",
            "created_by",
            "modified_on",
            "modified_by",
        )
    }
    required_types: Dict[str, Dict[str, Sequence[str]]] = {
        "exam": {
            "id": ("uuid",),
            "name": ("character varying",),
            "inst_id": ("uuid",),
            "course_id": ("uuid",),
            "term": ("character varying",),
            "section": ("character varying",),
            "exam_contents": ("jsonb",),
            "lock_history": ("jsonb",),
            "attendance_list": ("jsonb",),
            "remarks_list": ("jsonb",),
            "status": ("user-defined", "exam_status_enum"),
            "days_worked": ("integer",),
            "total_max_marks": ("numeric",),
            "merge_index": ("integer",),
            "start_date": ("timestamp with time zone",),
            "result_date": ("timestamp with time zone",),
            "owner_id": ("uuid",),
            "parent_id": ("uuid",),
            "created_on": ("timestamp with time zone",),
            "created_by": ("uuid",),
            "modified_on": ("timestamp with time zone",),
            "modified_by": ("uuid",),
        }
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
        if not existing:
            raise RuntimeError(f"Missing required table public.{table_name}.")
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


def upsert_exam(
    cur: psycopg2.extensions.cursor, doc: Dict[str, Any]
) -> Optional[UpsertResult]:
    exam_id = derive_exam_id(doc)
    if not exam_id:
        return None

    cur.execute("SELECT 1 FROM exam WHERE id = %s", (exam_id,))
    is_new = cur.fetchone() is None

    exam_contents_json = as_json(doc.get("ExamContents"))
    lock_history_json = as_json(doc.get("LockHistory"))
    attendance_list_json = as_json(doc.get("AttendanceList"))
    remarks_list_json = as_json(doc.get("RemarksList"))

    sql = """
        INSERT INTO exam (
            id,
            name,
            inst_id,
            course_id,
            term,
            section,
            exam_contents,
            lock_history,
            attendance_list,
            remarks_list,
            status,
            days_worked,
            total_max_marks,
            merge_index,
            start_date,
            result_date,
            owner_id,
            parent_id,
            created_on,
            created_by,
            modified_on,
            modified_by
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (id)
        DO UPDATE SET
            name = EXCLUDED.name,
            inst_id = EXCLUDED.inst_id,
            course_id = EXCLUDED.course_id,
            term = EXCLUDED.term,
            section = EXCLUDED.section,
            exam_contents = EXCLUDED.exam_contents,
            lock_history = EXCLUDED.lock_history,
            attendance_list = EXCLUDED.attendance_list,
            remarks_list = EXCLUDED.remarks_list,
            status = EXCLUDED.status,
            days_worked = EXCLUDED.days_worked,
            total_max_marks = EXCLUDED.total_max_marks,
            merge_index = EXCLUDED.merge_index,
            start_date = EXCLUDED.start_date,
            result_date = EXCLUDED.result_date,
            owner_id = EXCLUDED.owner_id,
            parent_id = EXCLUDED.parent_id,
            created_on = EXCLUDED.created_on,
            created_by = EXCLUDED.created_by,
            modified_on = EXCLUDED.modified_on,
            modified_by = EXCLUDED.modified_by
        RETURNING id;
        """
    params = (
        exam_id,
        as_text(doc.get("Name")),
        extract_uuid_from_any(doc.get("InstId")),
        extract_uuid_from_any(doc.get("CourseId")),
        as_text(doc.get("Term")),
        as_text(doc.get("Section")),
        exam_contents_json,
        lock_history_json,
        attendance_list_json,
        remarks_list_json,
        parse_exam_status(doc.get("Status")),
        parse_int(doc.get("DaysWorked")),
        parse_decimal(doc.get("TotalMaxMarks")),
        parse_int(doc.get("MergeIndex")),
        parse_ts(doc.get("StartDate")),
        parse_ts(doc.get("ResultDate")),
        extract_uuid_from_any(doc.get("OwnerId")),
        extract_uuid_from_any(doc.get("ParentId")),
        parse_ts(doc.get("CreatedOn")),
        extract_uuid_from_any(doc.get("CreatedBy")),
        parse_ts(doc.get("ModifiedOn")),
        extract_uuid_from_any(doc.get("ModifiedBy")),
    )
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None
    return UpsertResult(str(row[0]), is_new)


def build_source_profile(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    profile = {
        "exam_documents": len(docs),
        "with_id": 0,
        "subjects_in_exam_contents": 0,
        "evaluations_inside_exam_contents": 0,
        "attendance_rows": 0,
        "remark_rows": 0,
        "lock_history_rows": 0,
        "first_exam": None,
    }
    for doc in docs:
        if derive_exam_id(doc):
            profile["with_id"] += 1
        contents = (
            doc.get("ExamContents") if isinstance(doc.get("ExamContents"), list) else []
        )
        profile["subjects_in_exam_contents"] += len(contents)
        for content in contents:
            if isinstance(content, dict) and isinstance(content.get("Evaluation"), list):
                profile["evaluations_inside_exam_contents"] += len(content["Evaluation"])
        if isinstance(doc.get("AttendanceList"), list):
            profile["attendance_rows"] += len(doc["AttendanceList"])
        if isinstance(doc.get("RemarksList"), list):
            profile["remark_rows"] += len(doc["RemarksList"])
        if isinstance(doc.get("LockHistory"), list):
            profile["lock_history_rows"] += len(doc["LockHistory"])

    if docs:
        first = docs[0]
        profile["first_exam"] = {
            "id": derive_exam_id(first),
            "name": first.get("Name"),
            "status": first.get("Status"),
            "subjects_in_exam_contents": len(first.get("ExamContents") or []),
            "attendance_rows": len(first.get("AttendanceList") or []),
            "remark_rows": len(first.get("RemarksList") or []),
            "lock_history_rows": len(first.get("LockHistory") or []),
        }
    return profile


def main() -> int:
    cfg = parse_args()
    requests_session = requests.Session()
    conn = None
    try:
        configure_raven_session(requests_session, cfg)
        print(
            f"RavenDB target: url={cfg.raven_url}, db={cfg.raven_db}, collection={cfg.exams_collection}"
        )
        print("[1/3] Fetching RavenDB exam documents...")
        exam_docs = raven_query_collection(requests_session, cfg, cfg.exams_collection)
        print(f"Fetched exams={len(exam_docs)}")

        if cfg.inspect_source_only:
            print(json.dumps(build_source_profile(exam_docs), indent=2))
            return 0

        print("[2/3] Connecting PostgreSQL...")
        print(
            f"PostgreSQL target: host={cfg.pg_host}, port={cfg.pg_port}, db={cfg.pg_db}, user={cfg.pg_user}"
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

        exams_processed = 0
        exams_inserted = 0
        skipped_exams_missing_id = 0

        with conn:
            with conn.cursor() as cur:
                ensure_target_schema(cur)
                assert_required_schema(cur)

                print("[3/3] Upserting exams...")
                for doc in exam_docs:
                    result = upsert_exam(cur, doc)
                    if result is None:
                        skipped_exams_missing_id += 1
                        continue
                    exams_processed += 1
                    exams_inserted += int(result.inserted)

        api_payload_validation: Optional[Dict[str, Any]] = None
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM exam")
            exam_count = int(cur.fetchone()[0])
            if cfg.include_api_payload_validation:
                api_payload_validation = build_api_payload_validation(cur)

        summary = {
            "generated_at_utc": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "source": {
                "raven_url": cfg.raven_url,
                "raven_db": cfg.raven_db,
                "exams_collection": cfg.exams_collection,
            },
            "target": {
                "pg_host": cfg.pg_host,
                "pg_port": cfg.pg_port,
                "pg_db": cfg.pg_db,
                "pg_user": cfg.pg_user,
            },
            "run_stats": {
                "exams_processed": exams_processed,
                "new_exams_inserted": exams_inserted,
                "skipped_exams_missing_id": skipped_exams_missing_id,
            },
            "post_load_counts": {
                "exam": exam_count,
            },
        }
        if api_payload_validation is not None:
            summary["api_payload_validation"] = api_payload_validation

        print("Migration completed.")
        print(f"exams_processed: {exams_processed}")
        print(f"new_exams_inserted: {exams_inserted}")

        if cfg.write_summary_json:
            output_path = cfg.summary_json_path
            if not output_path:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                output_path = f"validation/exams-migration-summary-{timestamp}.json"
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
