# Student-Fee POC

## 1. Purpose

This POC validates the module database architecture using PostgreSQL data produced by the RavenDB migration scripts.

It demonstrates:
- **Zero Premature Schemas**: Base tables (`student`, `fee`, `fee_transaction`) are created dynamically by the RavenDB Python ETL migration scripts.
- **Module-Specific Database Isolation**: Student module queries against `student`, Fee module queries against `fee` and `fee_transaction`.
- **Synchronous Cross-Module Reads**: Cross-module queries (Student + Enrollments + Fee + Transactions) executed via PostgreSQL View (`student_fee_summary_view`) with dynamic JSONB extraction (`jsonb_array_elements`).
- **High Performance Indexing**: B-tree indexes on identifiers and dates + GIN indexes on JSONB arrays (`enrollments`, `installments_paid`).
- **PostgreSQL Triggers**: Automatic timestamp updating (`modified_on`).
- **ASP.NET Core 10 Web API**: Clean REST endpoints with Swagger UI documentation.
- **Automated Testing**: xUnit integration test suite verifying reads and writes against PostgreSQL.

---

## 2. Architecture

```text
RavenDB / SVC Source
        │
        ▼
Dockerized Migration Tool (Python ETL)
        │
        ├─► 1. Dynamically executes CREATE TABLE IF NOT EXISTS & loads data
        │      ├── student (with enrollments JSONB)
        │      ├── fee
        │      └── fee_transaction (with installments_paid JSONB)
        │
        └─► 2. Automatically applies post-migration SQL:
               ├── Performance GIN & B-tree Indexes
               ├── Synchronous Cross-Module View: student_fee_summary_view
               └── Auto-Timestamping Triggers (modified_on)
        │
        ▼
.NET 10 Web API (REST Endpoints + Swagger UI + xUnit Tests)
```

---

## 3. Database Schema, Indexes & Triggers

### High-Performance Indexes (defined in `sql/01_student_fee_view.sql`)
- **B-tree Indexes**:
  - `idx_student_student_id` on `student(student_id)`
  - `idx_student_name` on `student(name)`
  - `idx_fee_name` on `fee(name)`
  - `idx_fee_transaction_student_id` on `fee_transaction(student_id)`
  - `idx_fee_transaction_tx_date` on `fee_transaction(tx_date DESC NULLS LAST)`
  - `idx_fee_transaction_tx_no` on `fee_transaction(tx_no)`
- **GIN Indexes for JSONB**:
  - `idx_student_enrollments_gin` on `student USING GIN (enrollments)`
  - `idx_fee_transaction_installments_gin` on `fee_transaction USING GIN (installments_paid)`

### Triggers (defined in `sql/02_trigger.sql`)
- **`trg_student_modified_on` / `trg_fee_modified_on` / `trg_fee_transaction_modified_on`**:
  - Automatically updates `modified_on = CURRENT_TIMESTAMP` on every `UPDATE` operation.

---

## 4. REST API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service health check and database ping |
| `GET` | `/api/students?limit=10` | Single-module student list (reads JSONB enrollments) |
| `GET` | `/api/fees?limit=10` | Single-module fee list |
| `GET` | `/api/student-fees?limit=10` | Synchronous cross-module read via `student_fee_summary_view` (full API parity) |
| `GET` | `/api/fee-transactions?limit=10` | PostgreSQL-derived exact fee transactions payload |
| `POST` | `/api/fee-transactions` | Inserts fee transaction (verifies write + trigger) |

---

## 5. Running the Application

### Running with Docker Compose (Recommended)
From repo root:
```bash
# 1. Start clean Postgres & Web API
docker compose up -d --build postgres api

# 2. Run Data Migration (Creates tables, loads data, and applies view/triggers)
docker compose run --rm migrator --all
```
- Swagger UI: [http://localhost:5000](http://localhost:5000)

### Running Automated Tests
```bash
cd student-fee-poc/dotnet/StudentFeePoc.Tests
dotnet test
```

---

## 6. Verification with cURL

```bash
# 1. Health check
curl -s http://localhost:5000/health

# 2. Query students (reads enrollments JSONB)
curl -s "http://localhost:5000/api/students?limit=3"

# 3. Query fees
curl -s "http://localhost:5000/api/fees?limit=3"

# 4. Cross-module view query (student + fee summary with full API parity)
curl -s "http://localhost:5000/api/student-fees?limit=3"

# 5. Insert payment transaction
curl -s -X POST "http://localhost:5000/api/fee-transactions" \
  -H "Content-Type: application/json" \
  -d '{
    "studentId": "00000000-0000-0000-0000-000000000001",
    "amount": 500.00,
    "status": "Active",
    "feeId": "00000000-0000-0000-0000-000000000002",
    "refNo": "TXN-TEST-001"
  }'
```
