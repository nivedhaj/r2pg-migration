-- =============================================================================
-- Regular View & Performance Indexes: student_fee_summary_view
-- Performs dynamic, synchronous cross-module join across student, fee, and transactions.
-- Provides full API-parity tabular dataset for student fee transactions.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- -----------------------------------------------------------------------------
-- 1. Performance Indexes (GIN for JSONB + B-tree for Foreign Keys & Dates)
-- -----------------------------------------------------------------------------

-- Indexes for student table
CREATE INDEX IF NOT EXISTS idx_student_student_id ON student (student_id);
CREATE INDEX IF NOT EXISTS idx_student_name ON student (name);
CREATE INDEX IF NOT EXISTS idx_student_enrollments_gin ON student USING GIN (enrollments);

-- Indexes for fee table
CREATE INDEX IF NOT EXISTS idx_fee_name ON fee (name);
CREATE INDEX IF NOT EXISTS idx_fee_status ON fee (status);

-- Indexes for fee_transaction table
CREATE INDEX IF NOT EXISTS idx_fee_transaction_student_id ON fee_transaction (student_id);
CREATE INDEX IF NOT EXISTS idx_fee_transaction_tx_date ON fee_transaction (tx_date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_fee_transaction_tx_no ON fee_transaction (tx_no);
CREATE INDEX IF NOT EXISTS idx_fee_transaction_status ON fee_transaction (status);
CREATE INDEX IF NOT EXISTS idx_fee_transaction_installments_gin ON fee_transaction USING GIN (installments_paid);

-- -----------------------------------------------------------------------------
-- 2. Synchronous Cross-Module View
-- -----------------------------------------------------------------------------

CREATE OR REPLACE VIEW student_fee_summary_view AS
WITH raw_paid_installments AS (
    SELECT
        ft.id AS fee_transaction_id,
        ft.owner_id,
        COALESCE(ft.parent_id, ft.owner_id) AS parent_id,
        ft.tx_no,
        ft.tx_date,
        ft.student_id,
        ft.amount AS transaction_amount,
        ft.status AS transaction_status,
        CASE 
            WHEN ft.status = 'Disabled' THEN 99
            WHEN ft.status = 'Active' THEN 1
            ELSE 0
        END AS status_code,
        ft.ref_no,
        ft.paid_by,
        ft.fines_paid,
        ft.discounts,
        ft.fee_adjustment,
        COALESCE(ft.is_fine_paid, false) AS is_fine_paid,
        COALESCE(ft.is_discount_given, false) AS is_discount_given,
        COALESCE(ft.is_opening_balance_adjusted, false) AS is_opening_balance_adjusted,
        NULLIF(COALESCE(
            installment ->> 'FeeId',
            installment ->> 'feeId',
            installment ->> 'Id',
            installment ->> 'id'
        ), '') AS raw_fee_id,
        NULLIF(COALESCE(
            installment ->> 'FeeName',
            installment ->> 'feeName'
        ), '') AS raw_fee_name,
        NULLIF(COALESCE(
            installment ->> 'FeeDisplayText',
            installment ->> 'feeDisplayText'
        ), '') AS raw_fee_display_text,
        NULLIF(COALESCE(
            installment ->> 'InstallmentId',
            installment ->> 'installmentId',
            installment ->> 'Id',
            installment ->> 'id'
        ), '') AS raw_installment_id,
        NULLIF(COALESCE(
            installment ->> 'Description',
            installment ->> 'description'
        ), '') AS raw_installment_desc,
        NULLIF(COALESCE(
            installment ->> 'DueDate',
            installment ->> 'dueDate'
        ), '') AS raw_due_date,
        NULLIF(COALESCE(
            installment ->> 'Amount',
            installment ->> 'PaidAmount',
            installment ->> 'amount',
            installment ->> 'paidAmount'
        ), '') AS raw_paid_amount
    FROM fee_transaction ft
    LEFT JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(ft.installments_paid) = 'array'
                THEN ft.installments_paid
            ELSE '[]'::jsonb
        END
    ) AS installment ON TRUE
),
paid_installments AS (
    SELECT
        fee_transaction_id,
        owner_id,
        parent_id,
        tx_no,
        tx_date,
        student_id,
        transaction_amount,
        transaction_status,
        status_code,
        ref_no,
        paid_by,
        fines_paid,
        discounts,
        fee_adjustment,
        is_fine_paid,
        is_discount_given,
        is_opening_balance_adjusted,
        CASE
            WHEN raw_fee_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN raw_fee_id::uuid
            ELSE NULL
        END AS fee_id,
        raw_fee_name,
        raw_fee_display_text,
        CASE
            WHEN raw_installment_id ~ '^[0-9]+$'
                THEN raw_installment_id::integer
            ELSE NULL
        END AS installment_id,
        raw_installment_desc,
        raw_due_date,
        CASE
            WHEN raw_paid_amount ~ '^-?[0-9]+(\.[0-9]+)?$'
                THEN raw_paid_amount::numeric(14, 2)
            ELSE NULL
        END AS paid_amount
    FROM raw_paid_installments
)
SELECT
    pi.fee_transaction_id AS id,
    pi.owner_id,
    pi.parent_id,
    pi.tx_no,
    pi.tx_date,
    s.id AS student_id,
    s.student_id AS student_code,
    s.name AS student_name,
    COALESCE(se.enrollment->>'CourseId', '') AS course_id,
    COALESCE(se.enrollment->>'CourseName', '') AS course_name,
    COALESCE(se.enrollment->>'SectionName', '') AS section,
    COALESCE(se.enrollment->>'TermName', '') AS term_name,
    pi.transaction_amount AS amount,
    COALESCE(pi.fee_id, f.id) AS fee_id,
    COALESCE(pi.raw_fee_name, f.name) AS fee_name,
    COALESCE(pi.raw_fee_display_text, f.display_text, f.name) AS fee_display_text,
    pi.installment_id,
    pi.raw_installment_desc AS installment_description,
    pi.raw_due_date AS due_date,
    COALESCE(pi.paid_amount, pi.transaction_amount) AS paid_amount,
    COALESCE(pi.fines_paid, '[]'::jsonb) AS fines_paid,
    COALESCE(pi.discounts, '[]'::jsonb) AS discounts,
    COALESCE(pi.fee_adjustment, '{}'::jsonb) AS fee_adjustment,
    pi.is_fine_paid,
    pi.is_discount_given,
    pi.is_opening_balance_adjusted,
    pi.transaction_status AS status,
    pi.status_code,
    pi.ref_no,
    pi.paid_by
FROM paid_installments pi
LEFT JOIN student s ON s.id = pi.student_id
LEFT JOIN LATERAL (
    SELECT enrollment 
    FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(s.enrollments) = 'array' THEN s.enrollments ELSE '[]'::jsonb END
    ) AS enrollment 
    LIMIT 1
) se ON TRUE
LEFT JOIN fee f ON f.id = pi.fee_id;
