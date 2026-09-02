# Local Testing & Onboarding Playbook
## CT-RPG: RavenDB to PostgreSQL Migration & .NET 10 Web API

A comprehensive, step-by-step playbook for developers and evaluators to configure credentials, start infrastructure, run migrations (via Docker or local CLI), execute automated .NET 10 test suites, and inspect database objects and REST API endpoints.

---

## 1. Prerequisites

- **Docker Desktop** (Running with Linux containers)
- **RavenDB Client Certificate** (`.pfx` file if connecting to RavenDB Cloud HTTPS)
- *(Optional)* **.NET 10 SDK & Python 3.12** (Only needed if running scripts/API directly on host machine without Docker)

---

## 2. Configuration Setup

All connection parameters are centralized in a single root **`.env`** file.

### Step A: Create `.env` from Example Template
```bash
cp .env.example .env
```

#### Placeholders Reference Table (`.env`)

| Variable | Placeholder / Default | Description |
|---|---|---|
| `PG_PORT` | `15432` | Host port for Docker PostgreSQL port forwarding (maps `15432:5432` to avoid conflicts with local Postgres `5432`) |
| `API_PORT` | `5000` | Host port for .NET 10 Web API |
| `PG_DB` | `rpg` | Target PostgreSQL database name |
| `PG_USER` | `postgres` | Target PostgreSQL username |
| `PG_PASSWORD` | `<your-postgres-password>` | Target PostgreSQL password |
| `RAVEN_URL` | `https://<your-cluster-name>.ravendb.cloud` | RavenDB instance URL |
| `RAVEN_DB` | `<your-database-name>` (e.g. `rpg` or `BTL`) | Source RavenDB database name |
| `RAVEN_CERT_FILE` | `certs/<your-client-cert>.pfx` | Client certificate path inside `scripts/certs/` |
| `RAVEN_CERT_PASSWORD` | *(leave empty or set password)* | Certificate password (if protected) |

---

### Step B: Place RavenDB Certificate
If connecting to RavenDB Cloud (HTTPS), download the client certificate from the shared Google Drive:
 **[Download RavenDB Client Certificate (.pfx)](https://drive.google.com/file/d/1tcdrDU3Q1zzWBqs-BS0_0PGGvXjR2INI/view?usp=drive_link)**

Copy the downloaded `.pfx` certificate into:
```text
scripts/
└── certs/
    └── <your-client-certificate>.pfx
```
*(All `.pfx` certificate files in this directory are automatically ignored by Git for security).*

---

### Step C: Local Host `appsettings.json` *(Only for Local Devs without Docker)*
When running via **Docker Compose**, connection strings are injected automatically from `.env`.

If you choose to run or debug the .NET 10 API and tests **directly on your host machine** (via Visual Studio, Rider, or `dotnet run`), configure the connection string with your password in:
- `student-fee-poc/dotnet/student-fee-poc/appsettings.json`
- `student-fee-poc/dotnet/student-fee-poc-tests/appsettings.json`

```json
{
  "ConnectionStrings": {
    "Postgres": "Host=localhost;Port=15432;Database=rpg;Username=postgres;Password=<your-postgres-password>"
  }
}
```

---

## 3. Primary Execution Flow: Docker Approach (Zero-Config)

Follow these steps in order:

### Step 1: Check Out the Repository into Local
Check out the repository into your local development machine and navigate into the project root directory.

---

### Step 2: Start PostgreSQL & .NET 10 Web API

> [!TIP]
> **Port Forwarding & Zero Conflict:**
> PostgreSQL host port forwarding is controlled via `PG_PORT` in `.env` (defaulting to `15432:5432`). If you have a local PostgreSQL instance running on `5432`, there is no conflict! If you prefer another port, simply adjust `PG_PORT` in `.env`.

Start the PostgreSQL 16 database and ASP.NET Core 10 Web API container:
```bash
docker compose up -d --build postgres api
```

#### 🔌 Connect pgAdmin to PostgreSQL:
Right after running this step, connect pgAdmin to the running Docker database:
1. Open **pgAdmin**.
2. In the left browser tree, right-click **Servers** ➔ **Register** ➔ **Server...**
3. In the **General** tab:
   - **Name**: `Docker RPG (Port 15432)`
4. In the **Connection** tab:
   - **Host name/address**: `localhost` (or `127.0.0.1`)
   - **Port**: **`15432`**
   - **Maintenance database**: `rpg`
   - **Username**: `postgres`
   - **Password**: `<your-postgres-password>` *(configured in `.env`)*
5. Click **Save**.
6. The `rpg` database is now connected. *(Tables will be populated after Step 3).*

---

### Step 3: Run Data Migration via Docker
Runs the Python ETL pipeline to extract data from RavenDB, dynamically create PostgreSQL base tables, transform documents, and apply indexes, views, and triggers:

```bash
# Run all 6 migration modules (personas, courses, staffs, students, fees, exams):
docker compose run --rm migrator --all
```

> **Selective Module Migration**: To run only specific modules (e.g. Students and Fees):
> ```bash
> docker compose run --rm migrator --module student,fees
> ```

> **View Migrated Tables in pgAdmin**: After migration finishes, in pgAdmin expand `Docker RPG (Port 15432)` ➔ `Databases` ➔ `rpg` ➔ `Schemas` ➔ `public`, then right-click **Tables** and click **Refresh** (or press `F5`) to view all 9 tables and views!

---

### Step 4: Verify Data Parity (RavenDB vs PostgreSQL)
Run the automated parity verification script to perform a 100% field-by-field audit across all 9 domains:
```bash
python scripts/verify_raven_to_postgres.py
```

---

### Step 5: Run Automated Tests (.NET 10)
Executes all xUnit unit & integration tests inside the official .NET 10 container (automatically pulls database connection settings from `.env`):
```bash
docker compose run --rm tests
```

---

### Step 6: Verify REST APIs & Swagger UI

Open your browser to interactively test the API via Swagger UI:
👉 **[http://localhost:5000](http://localhost:5000)**

Or verify endpoints directly in your terminal:
```bash
# 1. Health check:
curl.exe -s http://localhost:5000/health

# 2. CampusTrack Students endpoint (matching /api/stu/student):
curl.exe -s "http://localhost:5000/api/stu/student?limit=2"

# 3. CampusTrack Fee Transactions endpoint (matching /api/feeTx):
curl.exe -s "http://localhost:5000/api/feeTx?limit=2"

# 4. CampusTrack Fee Definitions endpoint (matching /api/fee):
curl.exe -s "http://localhost:5000/api/fee?limit=2"

# 5. Insert Fee Transaction (POST /api/feeTx write test with audit trigger):
curl.exe -s -X POST "http://localhost:5000/api/feeTx" -H "Content-Type: application/json" -d "{\"studentId\": \"cc93c106-82ea-4610-9873-3db87f1307c6\", \"amount\": 500.00, \"status\": \"Active\", \"feeId\": \"a7e2851c-2321-4adc-993a-387deefee3a8\", \"refNo\": \"DEMO-CURL-01\"}"
```

---

## 4. Alternative Execution: Local Host Developer Workflow (Direct CLI)

If you prefer running tools directly on your local host (outside Docker):

### A. Python ETL Migration CLI
1. Install requirements:
   ```bash
   pip install -r scripts/requirements.txt
   ```
2. Run master migration (automatically reads root `.env`):
   ```bash
   python scripts/migrate_all.py --all
   ```
3. Run individual module scripts directly:
   ```bash
   python scripts/students_ravendb_to_postgres_migrate.py
   python scripts/fees_ravendb_to_postgres_migrate.py
   python scripts/courses_ravendb_to_postgres_migrate.py
   python scripts/staffs_ravendb_to_postgres_migrate.py
   python scripts/personas_ravendb_to_postgres_migrate.py
   python scripts/exams_ravendb_to_postgres_migrate.py
   ```

---

### B. .NET 10 Web API & xUnit Test Suite CLI
1. Run xUnit tests directly on host:
   ```bash
   dotnet test student-fee-poc/dotnet/student-fee-poc-tests/student-fee-poc-tests.csproj
   ```
2. Run ASP.NET Core 10 Web API on host:
   ```bash
   dotnet run --project student-fee-poc/dotnet/student-fee-poc/student-fee-poc.csproj
   ```

---

## 5. Database Verification & SQL Inspection

You can run these queries directly in pgAdmin Query Tool or `psql` to verify data integrity:

### 1. Verify Migrated Record Counts
```sql
SELECT 'organization' AS entity, COUNT(*) FROM organization
UNION ALL SELECT 'institute', COUNT(*) FROM institute
UNION ALL SELECT 'student', COUNT(*) FROM student
UNION ALL SELECT 'fee', COUNT(*) FROM fee
UNION ALL SELECT 'fee_transaction', COUNT(*) FROM fee_transaction
UNION ALL SELECT 'persona', COUNT(*) FROM persona
UNION ALL SELECT 'course', COUNT(*) FROM course
UNION ALL SELECT 'staff', COUNT(*) FROM staff
UNION ALL SELECT 'exam', COUNT(*) FROM exam;
```

### 2. Verify Cross-Module View (`student_fee_summary_view`)
```sql
SELECT student_code, student_name, course_name, fee_name, amount, paid_amount, status
FROM student_fee_summary_view
LIMIT 10;
```

### 3. Verify PostgreSQL Trigger Audit Trail
```sql
SELECT tx_no, student_id, amount, status, action, logged_at
FROM fee_transaction_audit
ORDER BY logged_at DESC
LIMIT 5;
```

---

## 6. Teardown & Clean Reset

To stop running containers:
```bash
docker compose down
```

To wipe the database volume and start completely fresh:
```bash
docker compose down -v
```
