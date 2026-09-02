# CT-RPG: RavenDB to PostgreSQL Migration & .NET 10 Web API

A complete, production-ready demonstration of migrating school and fee management data from **RavenDB / SVC** to **PostgreSQL**, leveraging **PostgreSQL Normal Views with JSONB lateral extraction**, **High-Performance B-Tree & GIN Indexes**, **PostgreSQL Triggers**, and exposing an **ASP.NET Core 10 Web API** with Swagger UI and automated xUnit tests.

---

## 📑 Table of Contents

1. [Architecture Overview](#-architecture-overview)
2. [Prerequisites](#-prerequisites)
3. [Quick Start (One-Click Demo)](#-quick-start-one-click-demo)
4. [Step-by-Step Demo Guide (Manual Execution)](#-step-by-step-demo-guide-manual-execution)
   - [Step 1: Check Out the Repository into Local](#step-1-check-out-the-repository-into-local)
   - [Step 2: Start Infrastructure & Web API](#step-2-start-infrastructure--web-api)
   - [Step 3: Run Data Migration via Docker (Creates Tables & Loads Data)](#step-3-run-data-migration-via-docker-creates-tables--loads-data)
   - [Step 4: Verify Data Parity (RavenDB vs PostgreSQL)](#step-4-verify-data-parity-ravendb-vs-postgresql)
   - [Step 5: Run Automated Unit & Integration Tests](#step-5-run-automated-unit--integration-tests)
   - [Step 6: Execute REST API Tests](#step-6-execute-rest-api-tests)
5. [Database Schema, Views, Indexes & Triggers](#-database-schema-views-indexes--triggers)
6. [API Endpoint Reference](#-api-endpoint-reference)
7. [Repository Structure](#-repository-structure)
8. [Troubleshooting & FAQ](#-troubleshooting--faq)

---

## 🏛️ Architecture Overview

```text
                       ┌──────────────────────────────────────┐
                       │       RavenDB / SVC (Source)         │
                       └──────────────────┬───────────────────┘
                                          │
                                          ▼
                       ┌──────────────────────────────────────┐
                       │     Dockerized Migration Runner      │
                       │   (Python ETL: scripts/Dockerfile)   │
                       │                                      │
                       │ 1. Extracts RavenDB collections      │
                       │ 2. Dynamically creates PG tables     │
                       │ 3. Transforms & loads records        │
                       │ 4. Applies View, Indexes & Triggers  │
                       └──────────────────┬───────────────────┘
                                          │
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ PostgreSQL 16 (Port 5432)                                                              │
│                                                                                        │
│  Base Tables (Created & Loaded by Migration):                                          │
│    ├── student               (GIN Index on enrollments JSONB)                          │
│    ├── fee                   (B-tree Index on name, status)                            │
│    └── fee_transaction       (B-tree on tx_date/student_id + GIN on installments JSONB)│
│                                                                                        │
│  Synchronous Cross-Module View:                                                        │
│    └── student_fee_summary_view (LATERAL jsonb_array_elements join)                    │
└─────────────────────────────────────────┬──────────────────────────────────────────────┘
                                          │
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ ASP.NET Core 10 Web API (Port 5000)                                                    │
│                                                                                        │
│  ├── Swagger UI Documentation (/swagger or root /)                                     │
│  ├── /health                 -> DB Connection Ping                                     │
│  ├── /api/students           -> Reads student domain with JSONB enrollment breakdown   │
│  ├── /api/fees               -> Reads fee definitions                                  │
│  ├── /api/student-fees       -> Reads student_fee_summary_view (Cross-Module Join)     │
│  ├── /api/fee-transactions   -> Reads exact API payload formatted transaction data     │
│  └── POST /api/fee-transactions -> Inserts payment record (Triggers modified_on update)│
└─────────────────────────────────────────┬──────────────────────────────────────────────┘
                                          │
                        ┌─────────────────┴─────────────────┐
                        │                                   │
                        ▼                                   ▼
             ┌─────────────────────┐             ┌─────────────────────┐
             │   cURL API Tests    │             │ xUnit Test Suite    │
             └─────────────────────┘             └─────────────────────┘
```

### Key Architectural Decisions:
- **Zero Hardcoded Schema Initialization**: PostgreSQL starts completely clean. The migration scripts (`scripts/*.py`) dynamically create all tables (`CREATE TABLE IF NOT EXISTS`) and populate real data.
- **Module Boundary Isolation**: C# code does not perform in-memory joins across disparate modules. Each module queries its respective table(s).
- **Synchronous Cross-Module Reads**: Powered purely by PostgreSQL normal view `student_fee_summary_view`.
- **JSONB Arrays with GIN Indexing**: Student enrollments and installment details are stored as JSONB arrays inside base tables, queried via `LATERAL jsonb_array_elements` and accelerated by GIN indexes.
- **PostgreSQL Triggers**: Automatic `modified_on` timestamps on update.

---

## 📋 Prerequisites

- **Docker & Docker Compose** (Docker Desktop on Windows/macOS, or Docker Engine on Linux)
- **.NET 10 SDK** (optional if running tests via host; tests run in Docker automatically)
- **RavenDB Source Instance** (Cloud HTTPS with `.pfx` certificate or local HTTP on port 8080)

---

## 🔑 Configuration & Credentials Setup

Create your **`.env`** file by copying from **`.env.example`**:
```bash
cp .env.example .env
```

| Variable | Placeholder / Example | Description |
|---|---|---|
| `PG_PORT` | `15432` | Host port for PostgreSQL port forwarding (`15432:5432` avoids standard `5432` collisions) |
| `API_PORT` | `5000` | Host port for ASP.NET Core 10 Web API |
| `PG_DB` | `rpg` | Target PostgreSQL database name |
| `PG_USER` | `postgres` | Target PostgreSQL username |
| `PG_PASSWORD` | `<your-postgres-password>` | Target PostgreSQL password |
| `RAVEN_URL` | `https://<your-cluster-name>.ravendb.cloud` or `http://localhost:8080` | RavenDB instance URL |
| `RAVEN_DB` | `<your-database-name>` (e.g. `rpg` or `BTL`) | Target RavenDB database name |
| `RAVEN_CERT_FILE` | `certs/<your-client-cert>.pfx` | Path to client certificate in `scripts/certs/` |
| `RAVEN_CERT_PASSWORD` | `<your-cert-password>` (or leave empty) | Certificate password (if protected) |

> **Certificate Placement**: If using RavenDB Cloud or HTTPS, download the client certificate from the shared Google Drive: **[Download RavenDB Client Certificate (.pfx)](https://drive.google.com/file/d/1tcdrDU3Q1zzWBqs-BS0_0PGGvXjR2INI/view?usp=drive_link)** and place your `.pfx` file into `scripts/certs/` (e.g. `scripts/certs/<your-cert>.pfx`). All certificate files in `scripts/certs/` are ignored by Git for security.
>
> **Local Host .NET Execution (`appsettings.json`)**: When running with Docker Compose, connection strings are injected dynamically from `.env`. If running .NET directly on the host machine without Docker, update the password placeholder in `student-fee-poc/dotnet/StudentFeePoc/appsettings.json`.

---

## 📖 Step-by-Step Demo Guide

---

### Step 1: Check Out the Repository into Local
Check out the repository into your local development machine and navigate into the project root directory.

---

### Step 2: Start Infrastructure & Web API

> [!TIP]
> **Port Forwarding & Zero Conflict:**
> PostgreSQL host port forwarding is controlled via `PG_PORT` in `.env` (defaulting to `15432:5432`). If you already have a local PostgreSQL instance running on `5432`, there is no conflict! If you prefer another port, simply adjust `PG_PORT` in `.env`.

Start the clean PostgreSQL database and ASP.NET Core Web API container using Docker Compose:

```bash
docker compose up -d --build rpg-postgres rpg-api
```

#### What happens:
1. **PostgreSQL Container (`rpg-postgres`)** starts with host port forwarding on **`15432`** (`15432:5432`).
2. **.NET Web API Container (`rpg-api`)** starts on host port **`5000`**.
   - Waits for PostgreSQL to be healthy before starting.

#### 🔌 Connect pgAdmin / DBeaver to PostgreSQL:
- **Host**: `localhost` (or `127.0.0.1`)
- **Port**: **`15432`**
- **Maintenance database**: `rpg`
- **Username**: `postgres`
- **Password**: `<your-postgres-password>` (as configured in `.env`)

#### Verify:
- Open your browser to **Swagger UI**: [http://localhost:5000](http://localhost:5000)
- Check health endpoint:
  ```bash
  curl -s http://localhost:5000/health
  ```
  *Expected Output:*
  ```json
  {"status":"Healthy","database":"Connected","timestamp":"2026-08-31T04:15:00Z"}
  ```

---

### Step 3: Run Data Migration via Docker (Creates Tables & Loads Data)

Extract data from RavenDB, dynamically create the PostgreSQL tables, load transformed records, and apply views & triggers:

```bash
# Run migration across all modules (personas, courses, staffs, students, fees, exams)
docker compose run --rm rpg-migrator --all
```

> **Tip**: To run only specific modules (e.g. Students and Fees):
> ```bash
> docker compose run --rm rpg-migrator --module student,fees
> ```

#### What happens:
1. The Python migration scripts connect to RavenDB, extract document collections, dynamically execute `CREATE TABLE IF NOT EXISTS`, and bulk load records.
2. The orchestrator automatically executes `01_student_fee_view.sql` and `02_trigger.sql` to build the performance indexes, `student_fee_summary_view`, and audit triggers.

---

### Step 4: Verify Data Parity (RavenDB vs PostgreSQL)

Run the automated parity verification script to perform a 100% field-by-field audit across all 9 domains:
```bash
python scripts/verify_raven_to_postgres.py
```

---

### Step 5: Run Automated Unit & Integration Tests

Run the xUnit test suite inside the .NET 10 container (automatically pulls database connection settings from `.env`):

```bash
docker compose run --rm rpg-tests
```

*(Alternatively, if running on host with .NET 10 SDK: `dotnet test ./student-fee-poc/dotnet/student-fee-poc-tests/student-fee-poc-tests.csproj`)*

#### Test Suite Coverage:
1. `CanConnectToDatabase`: Verifies PostgreSQL connectivity.
2. `CanQueryRecentStudentsWithEnrollments`: Verifies reading student table and extracting JSONB enrollments.
3. `CanQueryStudentFeeSummaryView`: Verifies executing the synchronous cross-module view `student_fee_summary_view`.
4. `CanQueryFeeTransactionsApiPayload`: Verifies constructing API response payloads.
5. `CanInsertFeeTransactionAndVerifyPersistenceAndAuditTrigger`: Inserts a transaction, verifies database persistence, and checks that PostgreSQL trigger `trg_fee_transaction_audit` recorded the audit row.

---

### Step 6: Execute REST API Tests

> [!TIP]
> **Windows PowerShell Users:** In Windows PowerShell, type **`curl.exe`** instead of `curl` (e.g. `curl.exe -s "http://localhost:5000/api/students?limit=2"`).

Test all exposed REST API endpoints:

#### 1. CampusTrack Student Endpoint
```bash
curl.exe -s "http://localhost:5000/api/stu/student?activeStudentsOnly=false&todaysAbsenteesOnly=false&limit=2"
```

#### 2. CampusTrack Fee Transactions Endpoint
```bash
curl.exe -s "http://localhost:5000/api/feeTx?id=&limit=2"
```

#### 3. CampusTrack Fee Definitions Endpoint
```bash
curl.exe -s "http://localhost:5000/api/fee?limit=2"
```

#### 4. Insert Fee Transaction (POST Write API with Audit Trigger)
```bash
curl.exe -s -X POST "http://localhost:5000/api/feeTx" \
  -H "Content-Type: application/json" \
  -d '{
    "studentId": "cc93c106-82ea-4610-9873-3db87f1307c6",
    "amount": 500.00,
    "status": "Active",
    "feeId": "a7e2851c-2321-4adc-993a-387deefee3a8",
    "refNo": "DEMO-CURL-01"
  }'
```


---

## 🗄️ Database Schema, Views, Indexes & Triggers

### 1. High-Performance Indexes
```sql
-- B-tree Indexes for fast lookups and sorting
CREATE INDEX idx_student_student_id ON student (student_id);
CREATE INDEX idx_student_name ON student (name);
CREATE INDEX idx_fee_name ON fee (name);
CREATE INDEX idx_fee_transaction_student_id ON fee_transaction (student_id);
CREATE INDEX idx_fee_transaction_tx_date ON fee_transaction (tx_date DESC NULLS LAST);

-- GIN Indexes for fast JSONB querying
CREATE INDEX idx_student_enrollments_gin ON student USING GIN (enrollments);
CREATE INDEX idx_fee_transaction_installments_gin ON fee_transaction USING GIN (installments_paid);
```

### 2. Triggers & Audit Function
Defined in `student-fee-poc/sql/02_trigger.sql`:
- **`trg_student_modified_on` / `trg_fee_modified_on` / `trg_fee_transaction_modified_on`**: Automatically maintains `modified_on = CURRENT_TIMESTAMP` before any update on `student`, `fee`, and `fee_transaction`.
- **`trg_fee_transaction_audit`**: Records immutable append-only audit trail logs in `fee_transaction_audit`.

---

## 🔌 API Endpoint Reference

| Method | Route | Description | CampusTrack URL Parity |
|---|---|---|---|
| `GET` | `/health` | Healthcheck & PostgreSQL ping | System |
| `GET` | `/api/stu/student` | Full student profile + father/mother/guardian contacts + JSONB enrollments | `https://svc.campustrack.net/api/stu/student` |
| `GET` | `/api/feeTx` | Fee transactions with installments, fines, discounts & adjustments | `https://svc.campustrack.net/api/feeTx` |
| `POST` | `/api/feeTx` | Inserts transaction & activates database audit trigger | `https://svc.campustrack.net/api/feeTx` |
| `GET` | `/api/fee` | Fee definition list with `instId`, `displayText`, and `isTxDone` flag | `https://svc.campustrack.net/api/fee` |

---

## 📁 Repository Structure

```text
CT-RPG/
├── docker-compose.yml              # Orchestrates PostgreSQL + .NET API + Migrator
├── .env                            # Central configuration & port definitions
├── README.md                       # Master demo and architecture documentation
├── playbook.md                     # Step-by-step local testing playbook
│
├── scripts/                        # Containerized Migration Tool
│   ├── Dockerfile                  # Python migration container
│   ├── requirements.txt            # psycopg2-binary, requests, cryptography, requests-pkcs12
│   ├── migrate_all.py              # Master migration CLI runner (loads data + applies view/triggers)
│   ├── certs/                      # RavenDB PKCS#12 client certificate (.pfx)
│   ├── students_ravendb_to_postgres_migrate.py
│   ├── fees_ravendb_to_postgres_migrate.py
│   ├── courses_ravendb_to_postgres_migrate.py
│   ├── exams_ravendb_to_postgres_migrate.py
│   ├── personas_ravendb_to_postgres_migrate.py
│   └── staffs_ravendb_to_postgres_migrate.py
│
└── student-fee-poc/                # .NET Web API & PostgreSQL POC
    ├── README.md                   # Module-specific documentation
    ├── sql/
    │   ├── 01_student_fee_view.sql # student_fee_summary_view definition + GIN/B-tree indexes
    │   └── 02_trigger.sql          # Trigger and audit function definitions
    └── dotnet/
        ├── student-fee-poc/        # ASP.NET Core 10 Web API
        │   ├── Dockerfile          # Production multi-stage Docker build
        │   ├── Program.cs          # REST endpoints, DI, and Swagger configuration
        │   ├── student-fee-poc.csproj
        │   ├── appsettings.json    # PostgreSQL connection strings
        │   ├── Db/                 # Data Access Layer
        │   │   ├── Student/        # StudentQueries.cs
        │   │   └── Fee/            # FeeQueries.cs, FeeTransactionQueries.cs, FeeTransactionWriteQueries.cs
        │   └── Models/             # StudentFeeViewModel.cs, FeeTransactionApiResponse.cs
        └── student-fee-poc-tests/  # xUnit Automated Test Suite
            ├── student-fee-poc-tests.csproj
            ├── TestFixture.cs      # Test DB setup and connection pooling
            ├── DbReadTests.cs      # Student reads, view joins, API payload tests
            └── DbWriteTests.cs     # Transaction insertion & audit trigger tests
```

---

## 🛠️ Troubleshooting & FAQ

#### 1. Port Conflict on 5433 or 5000 / Instance Already Running?
- If older containers are still running on your host, stop them with:
  ```bash
  docker stop ct-postgres ct-dotnet-api
  ```
- **PostgreSQL**: Bound to `5433` on the host to avoid conflicting with a default local Postgres on `5432`.
- **.NET API**: Bound to `5000` on the host.

#### 2. RavenDB connection fails during migration?
- Verify `RAVEN_URL` in `scripts/.env`. If RavenDB is running on your host machine, use `http://host.docker.internal:8080` (configured by default for Docker).
- If RavenDB runs inside Docker, make sure it is started (`docker start ravendb`).

#### 3. How to reset the database completely?
```bash
docker compose down -v
docker compose up -d --build rpg-postgres rpg-api
```
