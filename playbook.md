# Local Testing & Onboarding Playbook
## CT-RPG: RavenDB to PostgreSQL Migration & .NET 10 Web API

A step-by-step walkthrough for developers and evaluators to configure credentials, start infrastructure, run the data migration, execute tests, and inspect the PostgreSQL database and ASP.NET Core 10 Web API.

---

## 1. Prerequisites

- **Docker Desktop** (Running with Linux containers)
- **RavenDB Client Certificate** (`.pfx` file if connecting to RavenDB Cloud)
- *(Optional)* **.NET 10 SDK** (Only needed if running API or tests directly on the host machine instead of Docker)

---

## 2. Configuration Setup

### A. Environment Configuration (`.env`)
Create your **`.env`** file by copying from **`.env.example`**:
```bash
cp .env.example .env
```

#### Placeholders Reference Table (`.env`)

| Variable | Placeholder / Example | Description |
|---|---|---|
| `PG_PORT` | `5422` | Host port for Docker PostgreSQL (avoids conflicts with local Postgres 5432/5433) |
| `API_PORT` | `5000` | Host port for .NET 10 Web API |
| `PG_DB` | `rpg` | Target PostgreSQL database name |
| `PG_USER` | `postgres` | Target PostgreSQL username |
| `PG_PASSWORD` | `<your-postgres-password>` | Target PostgreSQL password |
| `RAVEN_URL` | `https://<your-cluster-name>.ravendb.cloud` | RavenDB instance URL |
| `RAVEN_DB` | `<your-database-name>` (e.g. `rpg` or `BTL`) | Source RavenDB database name |
| `RAVEN_CERT_FILE` | `certs/<your-client-cert>.pfx` | Client certificate path inside `scripts/certs/` |
| `RAVEN_CERT_PASSWORD` | *(leave empty or set password)* | Certificate password (if protected) |

---

### B. Certificate Placement
If connecting to RavenDB Cloud (HTTPS), copy your `.pfx` certificate into:
```text
scripts/
└── certs/
    └── <your-client-certificate>.pfx
```
*(All `.pfx` and certificate files in this directory are automatically ignored by Git for security).*

---

### C. Local Host Execution: `appsettings.json` *(Optional)*
When running via **Docker Compose**, connection strings are injected automatically from `.env`.

If you choose to run or debug the .NET 10 API and tests **directly on your host machine** (via Visual Studio, Rider, or `dotnet run`), update the connection string with your password in:
- `student-fee-poc/dotnet/StudentFeePoc/appsettings.json`
- `student-fee-poc/dotnet/StudentFeePoc.Tests/appsettings.json`

```json
{
  "ConnectionStrings": {
    "Postgres": "Host=localhost;Port=5422;Database=rpg;Username=postgres;Password=<your-postgres-password>"
  }
}
```

---

## 3. Step-by-Step Walkthrough

Follow these 4 steps in order:

### Step 1: Start PostgreSQL & .NET 10 Web API

> [!TIP]
> **Port Conflict / Existing Instances:** If you encounter an error like `port is already allocated` or older container instances are still running, stop them before starting:
> ```bash
> docker stop ct-postgres ct-dotnet-api
> ```

Start the PostgreSQL 16 database and ASP.NET Core 10 Web API container:
```bash
docker compose up -d --build postgres api
```

---

#### 🔌 How to Connect pgAdmin to PostgreSQL:
Right after running Step 1, connect pgAdmin to the running Docker database:

1. Open **pgAdmin**.
2. In the left browser tree, right-click **Servers** ➔ **Register** ➔ **Server...**
3. In the **General** tab:
   - **Name**: `Docker RPG (Port 5422)`
4. In the **Connection** tab:
   - **Host name/address**: `localhost` (or `127.0.0.1`)
   - **Port**: **`5422`**
   - **Maintenance database**: `rpg`
   - **Username**: `postgres`
   - **Password**: `<your-postgres-password>` *(configured in `.env`)*
5. Click **Save**.
6. The `rpg` database is now connected. *(Base tables will be populated after Step 2).*

---

### Step 2: Run Data Migration via Docker
Runs the Python ETL pipeline to extract data from RavenDB, dynamically create PostgreSQL base tables, transform documents, and apply indexes, views, and triggers:
```bash
docker compose run --rm migrator --all
```

> **View Migrated Tables in pgAdmin**: After migration finishes, in pgAdmin expand `Docker RPG (Port 5422)` ➔ `Databases` ➔ `rpg` ➔ `Schemas` ➔ `public`, then right-click **Tables** and click **Refresh** (or press `F5`) to view all 9 tables and views!

---

### Step 3: Run Automated Tests (.NET 10)
Executes all xUnit unit & integration tests inside the official .NET 10 container (automatically pulls database connection settings from `.env`):
```bash
docker compose run --rm tests
```

---

### Step 4: Verify REST APIs & Swagger UI

Open your browser to interactively test the API via Swagger UI:
👉 **[http://localhost:5000](http://localhost:5000)**

Or verify endpoints directly in your terminal:
```bash
# 1. Health check:
curl.exe -s http://localhost:5000/health

# 2. Query students (JSONB enrollments):
curl.exe -s "http://localhost:5000/api/students?limit=2"

# 3. Query cross-module fee summary (View join):
curl.exe -s "http://localhost:5000/api/student-fees?limit=2"

# 4. Insert Fee Transaction (POST write test):
curl.exe -s -X POST "http://localhost:5000/api/fee-transactions" -H "Content-Type: application/json" -d "{\"studentId\": \"cc93c106-82ea-4610-9873-3db87f1307c6\", \"amount\": 500.00, \"status\": \"Active\", \"feeId\": \"a7e2851c-2321-4adc-993a-387deefee3a8\", \"refNo\": \"DEMO-CURL-01\"}"
```

---

## 4. Teardown & Clean Reset

To stop the containers:
```bash
docker compose down
```

To wipe the database volume and start completely fresh:
```bash
docker compose down -v
```
