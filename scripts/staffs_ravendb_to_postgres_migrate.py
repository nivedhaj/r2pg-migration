"""
Extract Staffs data from RavenDB and load it into PostgreSQL.

The RavenDB Staff document contains top-level fields plus nested arrays/objects
such as EmploymentHistory, Salaries, Contacts, and ClassTeacher. This script
stores searchable top-level fields as columns and keeps nested structures as
JSONB on the same staff row.

Before running: set all required configuration values in scripts/.env
(or pass them explicitly as command-line arguments).

Target tables:
- staff
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
    staffs_collection: str
    page_size: int
    timeout_sec: int
    summary_json_path: Optional[str]
    write_summary_json: bool
    include_api_payload_validation: bool
    inspect_source_only: bool


@dataclass
class UpsertResult:
    staff_id: str
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
        description="Migrate Staffs data from RavenDB to PostgreSQL"
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
        "--staffs-collection", default=os.getenv("STAFFS_COLLECTION", "Staffs")
    )
    parser.add_argument("--page-size", type=int, default=os.getenv("PAGE_SIZE"))
    parser.add_argument("--timeout-sec", type=int, default=os.getenv("TIMEOUT_SEC"))
    parser.add_argument(
        "--summary-json-path",
        default=os.getenv("MIGRATION_SUMMARY_JSON"),
        help=(
            "Optional output path for post-run JSON artifact. "
            "Default when omitted: validation/staffs-migration-summary-<timestamp>.json"
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
        help="Fetch RavenDB Staffs and print source shape/counts without writing PostgreSQL.",
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
    if not args.staffs_collection:
        parser.error(
            "Missing collection config. Provide --staffs-collection or set STAFFS_COLLECTION."
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
        staffs_collection=args.staffs_collection,
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


def as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def as_json(value: Any) -> Optional[Json]:
    if value is None:
        return None
    return Json(value)


def as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def iso_utc(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="microseconds")
            .rstrip("0")
            .rstrip(".")
            + "Z"
        )
    return str(value)


def staff_status_code(value: Any) -> int:
    return {
        "Unknown": -1,
        "Active": 1,
        "Disabled": 99,
    }.get(value, -1)


def gender_code(value: Any) -> int:
    return {
        "Female": 0,
        "Male": 1,
        "NoInfo": 90,
    }.get(value, 90)


def staff_type_code(value: Any) -> int:
    return {
        "Teaching": 0,
        "NonTeaching": 1,
        "Management": 2,
    }.get(value, 0)


def contact_type_code(value: Any) -> int:
    parsed = parse_int(value)
    if parsed is not None:
        return parsed
    text = (str(value).strip().lower() if value is not None else "")
    mapping = {
        "email": 10,
        "mobile": 20,
        "phone": 30,
        "whatsapp": 40,
    }
    return mapping.get(text, 0)


def contact_location_code(value: Any) -> int:
    parsed = parse_int(value)
    if parsed is not None:
        return parsed
    text = (str(value).strip().lower() if value is not None else "")
    mapping = {
        "personal": 10,
        "work": 20,
        "home": 30,
    }
    return mapping.get(text, 0)


def normalize_contacts(items: Any) -> List[Dict[str, Any]]:
    contacts: List[Dict[str, Any]] = []
    for item in as_list(items):
        if not isinstance(item, dict):
            continue
        contacts.append(
            {
                "contactType": contact_type_code(
                    first_non_empty(item.get("contactType"), item.get("ContactType"))
                ),
                "location": contact_location_code(
                    first_non_empty(item.get("location"), item.get("Location"))
                ),
                "info": first_non_empty(item.get("info"), item.get("Info")),
                "archivedOn": first_non_empty(
                    item.get("archivedOn"), item.get("ArchivedOn")
                ),
                "status": staff_status_code(
                    first_non_empty(item.get("status"), item.get("Status"))
                ),
                "notes": first_non_empty(item.get("notes"), item.get("Notes")),
                "primary": item.get("primary")
                if "primary" in item
                else item.get("Primary"),
            }
        )
    return contacts


def normalize_employment_history(items: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for item in as_list(items):
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "instId": first_non_empty(item.get("instId"), item.get("InstId")),
                "designation": first_non_empty(
                    item.get("designation"), item.get("Designation")
                ),
                "refId": first_non_empty(item.get("refId"), item.get("RefId")),
                "from": first_non_empty(item.get("from"), item.get("From")),
                "courseSubjectList": as_list(
                    first_non_empty(
                        item.get("courseSubjectList"), item.get("CourseSubjectList")
                    )
                ),
                "status": staff_status_code(
                    first_non_empty(item.get("status"), item.get("Status"))
                ),
            }
        )
    return records


def normalize_class_teacher(item: Any) -> Dict[str, Any]:
    source = as_dict(item)
    return {
        "courseId": first_non_empty(source.get("courseId"), source.get("CourseId")),
        "courseName": first_non_empty(
            source.get("courseName"), source.get("CourseName")
        ),
        "branch": first_non_empty(source.get("branch"), source.get("Branch")),
        "term": first_non_empty(source.get("term"), source.get("Term")),
        "section": first_non_empty(source.get("section"), source.get("Section")),
    }


def as_string_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x) for x in value if x is not None]
    return [str(value)]


def parse_staff_type(value: Any) -> Optional[str]:
    if value in ("Teaching", "NonTeaching", "Management"):
        return str(value)
    return None


def parse_gender_enum(value: Any) -> Optional[str]:
    if value in ("Female", "Male", "NoInfo"):
        return str(value)
    return "NoInfo"


def parse_staff_status(value: Any) -> str:
    if value in ("Unknown", "Active", "Disabled"):
        return str(value)
    return "Active"


def to_camel_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    contacts = normalize_contacts(row.get("contacts"))
    employment_history = normalize_employment_history(row.get("employment_history"))

    return {
        "instId": row.get("inst_id"),
        "doj": iso_utc(row.get("doj")),
        "designations": as_list(row.get("designations")),
        "status": row.get("status"),
        "employmentHistory": employment_history,
        "courseSubjectList": as_list(row.get("course_subject_list")),
        "alias": row.get("alias"),
        "classTeacher": normalize_class_teacher(row.get("class_teacher")),
        "refId": row.get("ref_id"),
        "userId": row.get("user_id"),
        "salaries": as_list(row.get("salaries")),
        "payslips": as_list(row.get("payslips")),
        "firstName": row.get("first_name"),
        "middleName": row.get("middle_name"),
        "lastName": row.get("last_name"),
        "name": row.get("name"),
        "title": row.get("title"),
        "gender": row.get("gender"),
        "staffType": row.get("staff_type"),
        "dob": iso_utc(row.get("dob")),
        "email": row.get("email"),
        "mobile": row.get("mobile"),
        "virtualId": row.get("virtual_id"),
        "contacts": contacts,
        "addresses": as_list(row.get("addresses")),
        "tags": as_list(row.get("tags")),
        "attributes": as_dict(row.get("attributes")),
        "id": row.get("id"),
        "ownerId": row.get("owner_id"),
        "parentId": row.get("parent_id") or "",
        "createdOn": iso_utc(row.get("created_on")),
        "createdBy": row.get("created_by"),
        "modifiedOn": iso_utc(row.get("modified_on")),
        "modifiedBy": row.get("modified_by"),
    }


def build_staffs_list_payload(
    cur: psycopg2.extensions.cursor, params: Dict[str, Any]
) -> Dict[str, Any]:
    top = int(params.get("recordsPerPage") or 256)
    current_page = int(params.get("currentPage") or 0)
    offset = current_page * top

    cur.execute("SELECT COUNT(*) FROM staff")
    total_records = int(cur.fetchone()[0])

    cur.execute(
        """
        SELECT
            id::text AS id,
            inst_id::text AS inst_id,
            doj,
            COALESCE(designations, '[]'::jsonb) AS designations,
            status,
            COALESCE(employment_history, '[]'::jsonb) AS employment_history,
            COALESCE(course_subject_list, '{}'::text[]) AS course_subject_list,
            alias,
            COALESCE(class_teacher, '{}'::jsonb) AS class_teacher,
            ref_id,
            user_id::text AS user_id,
            COALESCE(salaries, '[]'::jsonb) AS salaries,
            COALESCE(payslips, '[]'::jsonb) AS payslips,
            first_name,
            middle_name,
            last_name,
            name,
            title,
            gender,
            staff_type,
            dob,
            email,
            mobile,
            virtual_id,
            COALESCE(contacts, '[]'::jsonb) AS contacts,
            COALESCE(addresses, '[]'::jsonb) AS addresses,
            COALESCE(tags, '{}'::text[]) AS tags,
            COALESCE(attributes, '{}'::jsonb) AS attributes,
            owner_id::text AS owner_id,
            parent_id::text AS parent_id,
            created_on,
            created_by::text AS created_by,
            modified_on,
            modified_by::text AS modified_by
        FROM staff
        ORDER BY created_on DESC NULLS LAST, name NULLS LAST, id
        LIMIT %s OFFSET %s
        """,
        (top, offset),
    )

    columns = [desc[0] for desc in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    data = [to_camel_dict(row) for row in rows]
    total_pages = (total_records + top - 1) // top if top > 0 else 0

    return {
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
            "note": "PostgreSQL-derived API-shaped payloads for staff read parity validation.",
        },
        "endpoints": {
            "staffsList": {
                "request": list_params,
                "response": build_staffs_list_payload(cur, list_params),
            }
        },
    }


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


def derive_staff_id(doc: Dict[str, Any]) -> Optional[str]:
    return first_non_empty(
        extract_uuid_from_any(get_nested(doc, "@metadata", "@id")),
        extract_uuid_from_any(doc.get("StaffId")),
        extract_uuid_from_any(doc.get("Id")),
    )


def ensure_target_schema(cur: psycopg2.extensions.cursor) -> None:
    """Create staff table and indexes with exact target schema."""
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'staff_type_enum') THEN
                CREATE TYPE staff_type_enum AS ENUM (
                    'Teaching',
                    'NonTeaching',
                    'Management'
                );
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'staff_gender_enum') THEN
                CREATE TYPE staff_gender_enum AS ENUM (
                    'Female',
                    'Male',
                    'NoInfo'
                );
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'staff_status_enum') THEN
                CREATE TYPE staff_status_enum AS ENUM (
                    'Unknown',
                    'Active',
                    'Disabled'
                );
            END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS staff (
            id UUID PRIMARY KEY,
            inst_id UUID,
            doj TIMESTAMPTZ,
            designations JSONB,
            status staff_status_enum,
            employment_history JSONB,
            course_subject_list TEXT[],
            alias VARCHAR(200),
            class_teacher JSONB,
            ref_id VARCHAR(100),
            user_id UUID,
            salaries JSONB,
            payslips JSONB,
            first_name VARCHAR(100),
            middle_name VARCHAR(100),
            last_name VARCHAR(100),
            name VARCHAR(200),
            title VARCHAR(100),
            gender staff_gender_enum,
            staff_type staff_type_enum,
            dob TIMESTAMPTZ,
            email VARCHAR(200),
            mobile VARCHAR(32),
            virtual_id VARCHAR(200),
            contacts JSONB,
            addresses JSONB,
            tags TEXT[],
            attributes JSONB,
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
        "staff": (
            "id",
            "inst_id",
            "doj",
            "designations",
            "status",
            "employment_history",
            "course_subject_list",
            "alias",
            "class_teacher",
            "ref_id",
            "user_id",
            "salaries",
            "payslips",
            "first_name",
            "middle_name",
            "last_name",
            "name",
            "title",
            "gender",
            "staff_type",
            "dob",
            "email",
            "mobile",
            "virtual_id",
            "contacts",
            "addresses",
            "tags",
            "attributes",
            "owner_id",
            "parent_id",
            "created_on",
            "created_by",
            "modified_on",
            "modified_by",
        )
    }
    required_types: Dict[str, Dict[str, Sequence[str]]] = {
        "staff": {
            "id": ("uuid",),
            "inst_id": ("uuid",),
            "doj": ("timestamp with time zone",),
            "designations": ("jsonb",),
            "status": ("user-defined", "staff_status_enum"),
            "employment_history": ("jsonb",),
            "course_subject_list": ("array", "text[]"),
            "class_teacher": ("jsonb",),
            "user_id": ("uuid",),
            "salaries": ("jsonb",),
            "payslips": ("jsonb",),
            "gender": ("user-defined", "staff_gender_enum"),
            "staff_type": ("user-defined", "staff_type_enum"),
            "dob": ("timestamp with time zone",),
            "contacts": ("jsonb",),
            "addresses": ("jsonb",),
            "tags": ("array", "text[]"),
            "attributes": ("jsonb",),
            "owner_id": ("uuid",),
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


def upsert_staff(
    cur: psycopg2.extensions.cursor, doc: Dict[str, Any]
) -> Optional[UpsertResult]:
    staff_id = derive_staff_id(doc)
    if not staff_id:
        return None

    cur.execute("SELECT 1 FROM staff WHERE id = %s", (staff_id,))
    is_new = cur.fetchone() is None

    cur.execute(
        """
        INSERT INTO staff (
            id,
            inst_id,
            doj,
            designations,
            status,
            employment_history,
            course_subject_list,
            alias,
            class_teacher,
            ref_id,
            user_id,
            salaries,
            payslips,
            first_name,
            middle_name,
            last_name,
            name,
            title,
            gender,
            staff_type,
            dob,
            email,
            mobile,
            virtual_id,
            contacts,
            addresses,
            tags,
            attributes,
            owner_id,
            parent_id,
            created_on,
            created_by,
            modified_on,
            modified_by
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (id)
        DO UPDATE SET
            inst_id = EXCLUDED.inst_id,
            doj = EXCLUDED.doj,
            designations = EXCLUDED.designations,
            status = EXCLUDED.status,
            employment_history = EXCLUDED.employment_history,
            course_subject_list = EXCLUDED.course_subject_list,
            alias = EXCLUDED.alias,
            class_teacher = EXCLUDED.class_teacher,
            ref_id = EXCLUDED.ref_id,
            user_id = EXCLUDED.user_id,
            salaries = EXCLUDED.salaries,
            payslips = EXCLUDED.payslips,
            first_name = EXCLUDED.first_name,
            middle_name = EXCLUDED.middle_name,
            last_name = EXCLUDED.last_name,
            name = EXCLUDED.name,
            title = EXCLUDED.title,
            gender = EXCLUDED.gender,
            staff_type = EXCLUDED.staff_type,
            dob = EXCLUDED.dob,
            email = EXCLUDED.email,
            mobile = EXCLUDED.mobile,
            virtual_id = EXCLUDED.virtual_id,
            contacts = EXCLUDED.contacts,
            addresses = EXCLUDED.addresses,
            tags = EXCLUDED.tags,
            attributes = EXCLUDED.attributes,
            owner_id = EXCLUDED.owner_id,
            parent_id = EXCLUDED.parent_id,
            created_on = EXCLUDED.created_on,
            created_by = EXCLUDED.created_by,
            modified_on = EXCLUDED.modified_on,
            modified_by = EXCLUDED.modified_by
        RETURNING id;
        """,
        (
            staff_id,
            extract_uuid_from_any(doc.get("InstId")),
            parse_ts(doc.get("DOJ")),
            as_json(as_list(doc.get("Designations"))),
            parse_staff_status(doc.get("Status")),
            as_json(as_list(doc.get("EmploymentHistory"))),
            as_string_list(doc.get("CourseSubjectList")),
            as_text(doc.get("Alias")),
            as_json(as_dict(doc.get("ClassTeacher"))),
            as_text(doc.get("RefId")),
            extract_uuid_from_any(doc.get("UserId")),
            as_json(as_list(doc.get("Salaries"))),
            as_json(as_list(doc.get("Payslips"))),
            as_text(doc.get("FirstName")),
            as_text(doc.get("MiddleName")),
            as_text(doc.get("LastName")),
            as_text(doc.get("Name")),
            as_text(doc.get("Title")),
            parse_gender_enum(doc.get("Gender")),
            parse_staff_type(doc.get("StaffType")),
            parse_ts(doc.get("DOB")),
            as_text(doc.get("Email")),
            as_text(doc.get("Mobile")),
            as_text(doc.get("VirtualId")),
            as_json(as_list(doc.get("Contacts"))),
            as_json(as_list(doc.get("Addresses"))),
            as_string_list(doc.get("Tags")),
            as_json(as_dict(doc.get("Attributes"))),
            extract_uuid_from_any(doc.get("OwnerId")),
            extract_uuid_from_any(doc.get("ParentId")),
            parse_ts(doc.get("CreatedOn")),
            extract_uuid_from_any(doc.get("CreatedBy")),
            parse_ts(doc.get("ModifiedOn")),
            extract_uuid_from_any(doc.get("ModifiedBy")),
        ),
    )

    row = cur.fetchone()
    if not row:
        return None
    return UpsertResult(str(row[0]), is_new)


def build_source_profile(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    profile = {
        "staff_documents": len(docs),
        "with_staff_id": 0,
        "employment_history_rows": 0,
        "course_subject_rows": 0,
        "contacts_rows": 0,
        "salaries_rows": 0,
        "payslips_rows": 0,
        "first_staff": None,
    }

    for doc in docs:
        if derive_staff_id(doc):
            profile["with_staff_id"] += 1
        profile["employment_history_rows"] += len(as_list(doc.get("EmploymentHistory")))
        profile["course_subject_rows"] += len(as_list(doc.get("CourseSubjectList")))
        profile["contacts_rows"] += len(as_list(doc.get("Contacts")))
        profile["salaries_rows"] += len(as_list(doc.get("Salaries")))
        profile["payslips_rows"] += len(as_list(doc.get("Payslips")))

    if docs:
        first = docs[0]
        profile["first_staff"] = {
            "id": derive_staff_id(first),
            "name": first.get("Name"),
            "status": first.get("Status"),
            "owner_id": first.get("OwnerId"),
            "contacts_rows": len(as_list(first.get("Contacts"))),
            "employment_history_rows": len(as_list(first.get("EmploymentHistory"))),
        }

    return profile


def main() -> int:
    cfg = parse_args()
    requests_session = requests.Session()
    conn = None

    try:
        configure_raven_session(requests_session, cfg)
        print(
            f"RavenDB target: url={cfg.raven_url}, db={cfg.raven_db}, collection={cfg.staffs_collection}"
        )
        print("[1/3] Fetching RavenDB staff documents...")
        staff_docs = raven_query_collection(requests_session, cfg, cfg.staffs_collection)
        print(f"Fetched staffs={len(staff_docs)}")

        if cfg.inspect_source_only:
            print(json.dumps(build_source_profile(staff_docs), indent=2))
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

        staffs_processed = 0
        staffs_inserted = 0
        skipped_staffs_missing_id = 0

        with conn:
            with conn.cursor() as cur:
                ensure_target_schema(cur)
                assert_required_schema(cur)

                print("[3/3] Upserting staffs...")
                for doc in staff_docs:
                    result = upsert_staff(cur, doc)
                    if result is None:
                        skipped_staffs_missing_id += 1
                        continue
                    staffs_processed += 1
                    staffs_inserted += int(result.inserted)

        api_payload_validation: Optional[Dict[str, Any]] = None
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM staff")
            staff_count = int(cur.fetchone()[0])
            if cfg.include_api_payload_validation:
                api_payload_validation = build_api_payload_validation(cur)

        summary = {
            "generated_at_utc": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "source": {
                "raven_url": cfg.raven_url,
                "raven_db": cfg.raven_db,
                "staffs_collection": cfg.staffs_collection,
            },
            "target": {
                "pg_host": cfg.pg_host,
                "pg_port": cfg.pg_port,
                "pg_db": cfg.pg_db,
                "pg_user": cfg.pg_user,
            },
            "run_stats": {
                "staffs_processed": staffs_processed,
                "new_staffs_inserted": staffs_inserted,
                "skipped_staffs_missing_id": skipped_staffs_missing_id,
            },
            "post_load_counts": {
                "staff": staff_count,
            },
        }
        if api_payload_validation is not None:
            summary["api_payload_validation"] = api_payload_validation

        print("Migration completed.")
        print(f"staffs_processed: {staffs_processed}")
        print(f"new_staffs_inserted: {staffs_inserted}")

        if cfg.write_summary_json:
            output_path = cfg.summary_json_path
            if not output_path:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                output_path = f"validation/staffs-migration-summary-{timestamp}.json"
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
