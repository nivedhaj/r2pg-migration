FROM python:3.12-slim

WORKDIR /app

# Install system dependencies if required
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependencies and install
COPY scripts/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all migration scripts and SQL view definitions
COPY scripts/ ./scripts/
COPY student-fee-poc/sql/ ./sql/

WORKDIR /app/scripts

ENTRYPOINT ["python", "migrate_all.py"]
CMD ["--all"]
