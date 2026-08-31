#!/usr/bin/env python3
"""
Extract Fees and FeeTx data from RavenDB, transform it to PostgreSQL schema,
and load into PostgreSQL.

Before running: set all required configuration values in .env
(or pass them explicitly as command-line arguments).

Target tables:
- fee
- fee_transaction
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
    fees_collection: str
    fee_txs_collection: str
    page_size: int
    timeout_sec: int
    summary_json_path: Optional[str]
    write_summary_json: bool
    include_api_payload_validation: bool = True


@dataclass
class UpsertResult:
    record_id: str
    inserted: bool


def load_env_file(env_path: str) -> None:
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
        description="Migrate Fees and FeeTx data from RavenDB to PostgreSQL"
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
        "--fees-collection", default=os.getenv("FEES_COLLECTION", "Fees")
    )
    parser.add_argument(
        "--fee-txs-collection", default=os.getenv("FEE_TXS_COLLECTION", "FeeTxes")
    )

    parser.add_argument("--page-size", type=int, default=os.getenv("PAGE_SIZE"))
    parser.add_argument("--timeout-sec", type=int, default=os.getenv("TIMEOUT_SEC"))
    parser.add_argument(
        "--summary-json-path",
        default=os.getenv("MIGRATION_SUMMARY_JSON"),
        help=(
            "Optional output path for post-run JSON artifact. "
            "Default when omitted: validation/fees-migration-summary-<timestamp>.json"
        ),
    )
    parser.add_argument(
        "--no-summary-json",
        action="store_true",
        help="Disable writing post-run summary JSON artifact.",
    )
    parser.add_argument(
        "--include-api-payload-validation",
        action="store_true",
        default=True,
        help="Include API-shaped payload validation in summary output.",
    )

    args = parser.parse_args()

    if not args.raven_url or not args.raven_db:
        parser.error(
            "Missing RavenDB config. Provide --raven-url/--raven-db or set RAVEN_URL/RAVEN_DB."
        )

    if not args.pg_password:
        parser.error(
            "Missing PostgreSQL password. Provide --pg-password or set PG_PASSWORD."
        )

    if not args.pg_host or args.pg_port is None or not args.pg_db or not args.pg_user:
        parser.error(
            "Missing PostgreSQL config. Provide --pg-host/--pg-port/--pg-db/--pg-user or set PG_HOST/PG_PORT/PG_DB/PG_USER."
        )

    if not args.fees_collection or not args.fee_txs_collection:
        parser.error(
            "Missing collection config. Provide --fees-collection/--fee-txs-collection or set FEES_COLLECTION/FEE_TXS_COLLECTION."
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
        fees_collection=args.fees_collection,
        fee_txs_collection=args.fee_txs_collection,
        page_size=args.page_size,
        timeout_sec=args.timeout_sec,
        summary_json_path=args.summary_json_path,
        write_summary_json=not args.no_summary_json,
        include_api_payload_validation=args.include_api_payload_validation,
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
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def fee_status_code(value: Any) -> int:
    return {
        "Unknown": 0,
        "Active": 1,
        "Disabled": 99,
    }.get(value, 0)


def fee_tx_status_code(value: Any) -> int:
    return {
        "Active": 1,
        "Disabled": 99,
    }.get(value, 1)


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


def decimal_to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def as_json(value: Any) -> Optional[Json]:
    if value is None:
        return None
    return Json(value)


def as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def as_string_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x) for x in value if x is not None]
    return [str(value)]


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


def parse_fee_status(value: Any) -> str:
    if value in ("Unknown", "Active", "Disabled"):
        return str(value)
    return "Active"


def parse_fee_tx_status(value: Any) -> str:
    if value in ("Active", "Disabled"):
        return str(value)
    return "Active"


def parse_payment_mode(value: Any, default: Optional[str] = "Cash") -> Optional[str]:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    lookup = {
        "cash": "Cash",
        "cheque": "Cheque",
        "online": "Online",
        "banktransfer": "BankTransfer",
        "bank_transfer": "BankTransfer",
        "upi": "UPI",
        "card": "Card",
        "dd": "DD",
        "draft": "Draft",
        "other": "Other",
    }
    return lookup.get(text.lower(), text)


def to_fee_camel_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "nameLower": row.get("name_lower"),
        "displayText": row.get("display_text"),
        "amount": decimal_to_float(row.get("amount")),
        "tags": as_list(row.get("tags")),
        "collectStudentWise": row.get("collect_student_wise"),
        "studentList": as_list(row.get("student_list")),
        "courseList": as_list(row.get("course_list")),
        "installments": as_list(row.get("installments")),
        "fines": as_list(row.get("fines")),
        "isTxDone": row.get("is_tx_done"),
        "status": row.get("status"),
        "ownerId": row.get("owner_id"),
        "parentId": row.get("parent_id"),
        "createdOn": iso_utc(row.get("created_on")),
        "createdBy": row.get("created_by"),
        "modifiedOn": iso_utc(row.get("modified_on")),
        "modifiedBy": row.get("modified_by"),
    }


def to_fee_tx_camel_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "txNo": row.get("tx_no"),
        "txDate": iso_utc(row.get("tx_date")),
        "studentId": row.get("student_id"),
        "installmentsPaid": as_list(row.get("installments_paid")),
        "finesPaid": as_list(row.get("fines_paid")),
        "discounts": as_list(row.get("discounts")),
        "feeAdjustment": row.get("fee_adjustment"),
        "paymentMode": row.get("payment_mode"),
        "isFinePaid": row.get("is_fine_paid"),
        "isDiscountGiven": row.get("is_discount_given"),
        "hasFeeAdjustment": row.get("has_fee_adjustment"),
        "isOpeningBalanceAdjusted": row.get("is_opening_balance_adjusted"),
        "refNo": row.get("ref_no"),
        "amount": decimal_to_float(row.get("amount")),
        "status": row.get("status"),
        "paidBy": row.get("paid_by"),
        "chequeNo": row.get("cheque_no"),
        "bankName": row.get("bank_name"),
        "chequeDate": iso_utc(row.get("cheque_date")),
        "onlineTxnRefNo": row.get("online_txn_ref_no"),
        "ownerId": row.get("owner_id"),
        "parentId": row.get("parent_id"),
        "createdOn": iso_utc(row.get("created_on")),
        "createdBy": row.get("created_by"),
        "modifiedOn": iso_utc(row.get("modified_on")),
        "modifiedBy": row.get("modified_by"),
    }


def build_api_payload_validation(cur: psycopg2.extensions.cursor) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT
            id::text AS id,
            name,
            name_lower,
            display_text,
            amount,
            tags,
            collect_student_wise,
            student_list,
            course_list,
            installments,
            fines,
            is_tx_done,
            status,
            owner_id::text AS owner_id,
            parent_id::text AS parent_id,
            created_on,
            created_by::text AS created_by,
            modified_on,
            modified_by::text AS modified_by
        FROM fee
        ORDER BY name, id
        LIMIT 20;
        """
    )
    fee_columns = [desc[0] for desc in cur.description]
    fee_rows = [dict(zip(fee_columns, row)) for row in cur.fetchall()]
    fees_data = [to_fee_camel_dict(row) for row in fee_rows]

    cur.execute(
        """
        SELECT
            id::text AS id,
            tx_no,
            tx_date,
            student_id::text AS student_id,
            installments_paid,
            fines_paid,
            discounts,
            fee_adjustment,
            payment_mode,
            is_fine_paid,
            is_discount_given,
            has_fee_adjustment,
            is_opening_balance_adjusted,
            ref_no,
            amount,
            status,
            paid_by,
            cheque_no,
            bank_name,
            cheque_date,
            online_txn_ref_no,
            owner_id::text AS owner_id,
            parent_id::text AS parent_id,
            created_on,
            created_by::text AS created_by,
            modified_on,
            modified_by::text AS modified_by
        FROM fee_transaction
        ORDER BY tx_date DESC NULLS LAST, id
        LIMIT 20;
        """
    )
    tx_columns = [desc[0] for desc in cur.description]
    tx_rows = [dict(zip(tx_columns, row)) for row in cur.fetchall()]
    tx_data = [to_fee_tx_camel_dict(row) for row in tx_rows]

    return {
        "reference": {
            "note": "PostgreSQL-derived API-shaped payloads for fee and transaction read parity validation."
        },
        "endpoints": {
            "feesList": {
                "response": {
                    "data": fees_data,
                    "count": len(fees_data),
                }
            },
            "feeTransactionsList": {
                "response": {
                    "data": tx_data,
                    "count": len(tx_data),
                }
            },
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
                "Install dependencies with: "
                "python -m pip install -r scripts/python-connectivity/requirements.txt"
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


def derive_raven_doc_uuid(doc: Dict[str, Any]) -> Optional[str]:
    meta_id = get_nested(doc, "@metadata", "@id")
    return first_non_empty(
        extract_uuid_from_any(meta_id),
        extract_uuid_from_any(doc.get("Id")),
        extract_uuid_from_any(doc.get("FeeId")),
        extract_uuid_from_any(doc.get("TxId")),
    )


def ensure_target_schema(cur: psycopg2.extensions.cursor) -> None:
    """Create fee target tables when missing and validate required columns."""
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'fee_status_enum') THEN
                CREATE TYPE fee_status_enum AS ENUM (
                    'Unknown',
                    'Active',
                    'Disabled'
                );
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'fee_tx_status_enum') THEN
                CREATE TYPE fee_tx_status_enum AS ENUM (
                    'Active',
                    'Disabled'
                );
            END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS fee (
            id UUID PRIMARY KEY,
            name VARCHAR(200),
            name_lower VARCHAR(200),
            display_text VARCHAR(200),
            amount NUMERIC(14, 2),
            tags TEXT[],
            collect_student_wise BOOLEAN,
            student_list TEXT[],
            course_list TEXT[],
            installments JSONB,
            fines JSONB,
            is_tx_done BOOLEAN,
            status fee_status_enum,
            owner_id UUID,
            parent_id UUID,
            created_on TIMESTAMPTZ,
            created_by UUID,
            modified_on TIMESTAMPTZ,
            modified_by UUID
        );

        CREATE TABLE IF NOT EXISTS fee_transaction (
            id UUID PRIMARY KEY,
            tx_no VARCHAR(100),
            tx_date TIMESTAMPTZ,
            student_id UUID,
            installments_paid JSONB,
            fines_paid JSONB,
            discounts JSONB,
            fee_adjustment JSONB,
            payment_mode VARCHAR(64),
            is_fine_paid BOOLEAN,
            is_discount_given BOOLEAN,
            has_fee_adjustment BOOLEAN,
            is_opening_balance_adjusted BOOLEAN,
            ref_no VARCHAR(100),
            amount NUMERIC(14, 2),
            status fee_tx_status_enum,
            paid_by VARCHAR(100),
            cheque_no VARCHAR(100),
            bank_name VARCHAR(200),
            cheque_date TIMESTAMPTZ,
            online_txn_ref_no VARCHAR(100),
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
        "fee": (
            "id",
            "name",
            "name_lower",
            "display_text",
            "amount",
            "tags",
            "collect_student_wise",
            "student_list",
            "course_list",
            "installments",
            "fines",
            "is_tx_done",
            "status",
            "owner_id",
            "parent_id",
            "created_on",
            "created_by",
            "modified_on",
            "modified_by",
        ),
        "fee_transaction": (
            "id",
            "tx_no",
            "tx_date",
            "student_id",
            "installments_paid",
            "fines_paid",
            "discounts",
            "fee_adjustment",
            "payment_mode",
            "is_fine_paid",
            "is_discount_given",
            "has_fee_adjustment",
            "is_opening_balance_adjusted",
            "ref_no",
            "amount",
            "status",
            "paid_by",
            "cheque_no",
            "bank_name",
            "cheque_date",
            "online_txn_ref_no",
            "owner_id",
            "parent_id",
            "created_on",
            "created_by",
            "modified_on",
            "modified_by",
        ),
    }
    required_types: Dict[str, Dict[str, Sequence[str]]] = {
        "fee": {
            "id": ("uuid",),
            "amount": ("numeric",),
            "tags": ("array", "text[]"),
            "collect_student_wise": ("boolean",),
            "student_list": ("array", "text[]"),
            "course_list": ("array", "text[]"),
            "installments": ("jsonb",),
            "fines": ("jsonb",),
            "is_tx_done": ("boolean",),
            "status": ("user-defined", "fee_status_enum"),
            "owner_id": ("uuid",),
            "parent_id": ("uuid",),
            "created_on": ("timestamp with time zone",),
            "created_by": ("uuid",),
            "modified_on": ("timestamp with time zone",),
            "modified_by": ("uuid",),
        },
        "fee_transaction": {
            "id": ("uuid",),
            "tx_date": ("timestamp with time zone",),
            "student_id": ("uuid",),
            "installments_paid": ("jsonb",),
            "fines_paid": ("jsonb",),
            "discounts": ("jsonb",),
            "fee_adjustment": ("jsonb",),
            "payment_mode": ("character varying",),
            "is_fine_paid": ("boolean",),
            "is_discount_given": ("boolean",),
            "has_fee_adjustment": ("boolean",),
            "is_opening_balance_adjusted": ("boolean",),
            "amount": ("numeric",),
            "status": ("user-defined", "fee_tx_status_enum"),
            "cheque_date": ("timestamp with time zone",),
            "owner_id": ("uuid",),
            "parent_id": ("uuid",),
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
        if not existing:
            raise RuntimeError(
                f"Missing required table public.{table_name}. "
                "Create target fee tables before running this ETL."
            )

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


def upsert_fee(
    cur: psycopg2.extensions.cursor, doc: Dict[str, Any]
) -> Optional[UpsertResult]:
    fee_id = derive_raven_doc_uuid(doc)
    if not fee_id:
        return None

    cur.execute("SELECT 1 FROM fee WHERE id = %s", (fee_id,))
    is_new = cur.fetchone() is None

    cur.execute(
        """
        INSERT INTO fee (
            id,
            name,
            name_lower,
            display_text,
            amount,
            tags,
            collect_student_wise,
            student_list,
            course_list,
            installments,
            fines,
            is_tx_done,
            status,
            owner_id,
            parent_id,
            created_on,
            created_by,
            modified_on,
            modified_by
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (id)
        DO UPDATE SET
            name = EXCLUDED.name,
            name_lower = EXCLUDED.name_lower,
            display_text = EXCLUDED.display_text,
            amount = EXCLUDED.amount,
            tags = EXCLUDED.tags,
            collect_student_wise = EXCLUDED.collect_student_wise,
            student_list = EXCLUDED.student_list,
            course_list = EXCLUDED.course_list,
            installments = EXCLUDED.installments,
            fines = EXCLUDED.fines,
            is_tx_done = EXCLUDED.is_tx_done,
            status = EXCLUDED.status,
            owner_id = EXCLUDED.owner_id,
            parent_id = EXCLUDED.parent_id,
            created_on = EXCLUDED.created_on,
            created_by = EXCLUDED.created_by,
            modified_on = EXCLUDED.modified_on,
            modified_by = EXCLUDED.modified_by
        RETURNING id;
        """,
        (
            fee_id,
            first_non_empty(doc.get("Name"), doc.get("DisplayText")),
            doc.get("NameLower"),
            doc.get("DisplayText"),
            parse_decimal(doc.get("Amount")),
            as_string_list(doc.get("Tags")),
            bool(doc.get("CollectStudentWise")) if doc.get("CollectStudentWise") is not None else None,
            as_string_list(doc.get("StudentList")),
            as_string_list(doc.get("CourseList")),
            as_json(doc.get("Installments")),
            as_json(doc.get("Fines")),
            bool(doc.get("IsTxDone")) if doc.get("IsTxDone") is not None else None,
            parse_fee_status(doc.get("Status")),
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


def upsert_fee_transaction(
    cur: psycopg2.extensions.cursor, doc: Dict[str, Any]
) -> Optional[UpsertResult]:
    tx_id = derive_raven_doc_uuid(doc)
    if not tx_id:
        return None

    cur.execute("SELECT 1 FROM fee_transaction WHERE id = %s", (tx_id,))
    is_new = cur.fetchone() is None

    cur.execute(
        """
        INSERT INTO fee_transaction (
            id,
            tx_no,
            tx_date,
            student_id,
            installments_paid,
            fines_paid,
            discounts,
            fee_adjustment,
            payment_mode,
            is_fine_paid,
            is_discount_given,
            has_fee_adjustment,
            is_opening_balance_adjusted,
            ref_no,
            amount,
            status,
            paid_by,
            cheque_no,
            bank_name,
            cheque_date,
            online_txn_ref_no,
            owner_id,
            parent_id,
            created_on,
            created_by,
            modified_on,
            modified_by
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (id)
        DO UPDATE SET
            tx_no = EXCLUDED.tx_no,
            tx_date = EXCLUDED.tx_date,
            student_id = EXCLUDED.student_id,
            installments_paid = EXCLUDED.installments_paid,
            fines_paid = EXCLUDED.fines_paid,
            discounts = EXCLUDED.discounts,
            fee_adjustment = EXCLUDED.fee_adjustment,
            payment_mode = EXCLUDED.payment_mode,
            is_fine_paid = EXCLUDED.is_fine_paid,
            is_discount_given = EXCLUDED.is_discount_given,
            has_fee_adjustment = EXCLUDED.has_fee_adjustment,
            is_opening_balance_adjusted = EXCLUDED.is_opening_balance_adjusted,
            ref_no = EXCLUDED.ref_no,
            amount = EXCLUDED.amount,
            status = EXCLUDED.status,
            paid_by = EXCLUDED.paid_by,
            cheque_no = EXCLUDED.cheque_no,
            bank_name = EXCLUDED.bank_name,
            cheque_date = EXCLUDED.cheque_date,
            online_txn_ref_no = EXCLUDED.online_txn_ref_no,
            owner_id = EXCLUDED.owner_id,
            parent_id = EXCLUDED.parent_id,
            created_on = EXCLUDED.created_on,
            created_by = EXCLUDED.created_by,
            modified_on = EXCLUDED.modified_on,
            modified_by = EXCLUDED.modified_by
        RETURNING id;
        """,
        (
            tx_id,
            doc.get("TxNo"),
            parse_ts(doc.get("TxDate")),
            extract_uuid_from_any(doc.get("StudentId")),
            as_json(doc.get("InstallmentsPaid")),
            as_json(doc.get("FinesPaid")),
            as_json(doc.get("Discounts")),
            as_json(doc.get("FeeAdjustment")),
            parse_payment_mode(doc.get("PaymentMode")),
            bool(doc.get("IsFinePaid")) if doc.get("IsFinePaid") is not None else None,
            bool(doc.get("IsDiscountGiven")) if doc.get("IsDiscountGiven") is not None else None,
            bool(doc.get("HasFeeAdjustment")) if doc.get("HasFeeAdjustment") is not None else None,
            bool(doc.get("IsOpeningBalanceAdjusted")) if doc.get("IsOpeningBalanceAdjusted") is not None else None,
            doc.get("RefNo"),
            parse_decimal(doc.get("Amount")),
            parse_fee_tx_status(doc.get("Status")),
            doc.get("PaidBy"),
            doc.get("ChequeNo"),
            doc.get("BankName"),
            parse_ts(doc.get("ChequeDate")),
            doc.get("OnlineTxnRefNo"),
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


def main() -> int:
    cfg = parse_args()

    requests_session = requests.Session()
    conn = None
    try:
        configure_raven_session(requests_session, cfg)

        print(
            f"RavenDB target: url={cfg.raven_url}, db={cfg.raven_db}, "
            f"collections=({cfg.fees_collection}, {cfg.fee_txs_collection})"
        )
        print("[1/4] Fetching RavenDB documents...")
        fee_docs = raven_query_collection(requests_session, cfg, cfg.fees_collection)
        tx_docs = raven_query_collection(requests_session, cfg, cfg.fee_txs_collection)
        print(f"Fetched fees={len(fee_docs)}, fee_txs={len(tx_docs)}")

        print("[2/4] Connecting PostgreSQL...")
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

        fees_processed = 0
        fee_txs_processed = 0
        fees_inserted = 0
        fee_txs_inserted = 0
        skipped_fees_missing_id = 0
        skipped_fee_txs_missing_id = 0
        deleted_fees_not_in_source = 0
        deleted_fee_txs_not_in_source = 0

        with conn:
            with conn.cursor() as cur:
                ensure_target_schema(cur)
                assert_required_schema(cur)

                print("[3/4] Upserting fees...")
                for doc in fee_docs:
                    result = upsert_fee(cur, doc)
                    if result is None:
                        skipped_fees_missing_id += 1
                        continue
                    fees_processed += 1
                    fees_inserted += int(result.inserted)

                print("[4/4] Upserting fee transactions...")
                for doc in tx_docs:
                    result = upsert_fee_transaction(cur, doc)
                    if result is None:
                        skipped_fee_txs_missing_id += 1
                        continue
                    fee_txs_processed += 1
                    fee_txs_inserted += int(result.inserted)

        api_payload_validation: Optional[Dict[str, Any]] = None
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fee")
            fee_count = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM fee_transaction")
            fee_tx_count = int(cur.fetchone()[0])
            if cfg.include_api_payload_validation:
                api_payload_validation = build_api_payload_validation(cur)

        summary = {
            "generated_at_utc": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "source": {
                "raven_url": cfg.raven_url,
                "raven_db": cfg.raven_db,
                "fees_collection": cfg.fees_collection,
                "fee_txs_collection": cfg.fee_txs_collection,
            },
            "target": {
                "pg_host": cfg.pg_host,
                "pg_port": cfg.pg_port,
                "pg_db": cfg.pg_db,
                "pg_user": cfg.pg_user,
            },
            "run_stats": {
                "fees_processed": fees_processed,
                "new_fees_inserted": fees_inserted,
                "fee_transactions_processed": fee_txs_processed,
                "new_fee_transactions_inserted": fee_txs_inserted,
                "skipped_fees_missing_id": skipped_fees_missing_id,
                "skipped_fee_transactions_missing_id": skipped_fee_txs_missing_id,
            },
            "post_load_counts": {
                "fee": fee_count,
                "fee_transaction": fee_tx_count,
            },
        }
        if api_payload_validation is not None:
            summary["api_payload_validation"] = api_payload_validation

        print("Migration completed.")
        print(f"fees_processed: {fees_processed}")
        print(f"new_fees_inserted: {fees_inserted}")
        print(f"fee_transactions_processed: {fee_txs_processed}")
        print(f"new_fee_transactions_inserted: {fee_txs_inserted}")

        if cfg.write_summary_json:
            output_path = cfg.summary_json_path
            if not output_path:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                output_path = f"validation/fees-migration-summary-{timestamp}.json"
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
