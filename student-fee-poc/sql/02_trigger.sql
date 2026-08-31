-- =============================================================================
-- PostgreSQL Triggers for Auto-Timestamping (modified_on)
-- Automatically maintains modified_on timestamp on student, fee, and fee_transaction
-- =============================================================================

-- 1. Auto-update modified_on timestamp trigger function
CREATE OR REPLACE FUNCTION update_modified_on_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.modified_on = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 2. Attach trigger to student table
DROP TRIGGER IF EXISTS trg_student_modified_on ON student;
CREATE TRIGGER trg_student_modified_on
BEFORE UPDATE ON student
FOR EACH ROW
EXECUTE FUNCTION update_modified_on_column();

-- 3. Attach trigger to fee table
DROP TRIGGER IF EXISTS trg_fee_modified_on ON fee;
CREATE TRIGGER trg_fee_modified_on
BEFORE UPDATE ON fee
FOR EACH ROW
EXECUTE FUNCTION update_modified_on_column();

-- 4. Attach trigger to fee_transaction table
DROP TRIGGER IF EXISTS trg_fee_transaction_modified_on ON fee_transaction;
CREATE TRIGGER trg_fee_transaction_modified_on
BEFORE UPDATE ON fee_transaction
FOR EACH ROW
EXECUTE FUNCTION update_modified_on_column();

-- Clean up legacy triggers and audit table if present
DROP TRIGGER IF EXISTS trg_fee_transaction_audit ON fee_transaction;
DROP FUNCTION IF EXISTS log_fee_transaction_audit() CASCADE;
DROP TABLE IF EXISTS fee_transaction_audit;
DROP TABLE IF EXISTS _test_integration_verification;
