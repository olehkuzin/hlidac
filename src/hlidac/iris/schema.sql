-- IRIS SQL tables of the hlidac app: schema Hlidac in the FHIRSERVER namespace (WP0 contract).
-- `IrisStore` (iris/store.py, WP10) implements src/hlidac/store.py on these tables; the production
-- (WP11) reads and updates Hlidac.outbox. Created by `python -m hlidac.iris.setup` (idempotent).
--
-- Conventions:
-- * record_json holds the full Pydantic record (model_dump_json); the other columns exist only
--   for lookups and for the production, and the Store keeps them in step with record_json.
-- * Exception, Hlidac.outbox: the status columns are the truth and job_json holds only the job,
--   so the production updates columns and never rewrites JSON.
-- * Timestamps are UTC (TIMESTAMP is %PosixTime in IRIS 2026.1: read it back with %ODBCOUT);
--   the JSON keeps the original offset.
-- * Ids and keys are COLLATE %EXACT: IRIS compares strings case-insensitively by default, and our
--   ids and FHIR ids are case-sensitive. Emails keep the default, so uniqueness ignores case.
-- * Column names avoid SQL reserved words (role, action, at, key, value, user, level ...).
-- * Only our own rows live here; hospital data stays in the FHIR repository, read only.
-- * The app's IRIS account may only SELECT, INSERT and UPDATE here, and only SELECT and INSERT on
--   the append-only tables (assessment, grade_review, audit_log); it may never DELETE (setup.py).

CREATE TABLE Hlidac.account (
    account_id      VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    email           VARCHAR(254) NOT NULL UNIQUE,  -- stored lower-case
    account_role    VARCHAR(16)  NOT NULL,         -- patient | nurse
    patient_id      VARCHAR(64)  COLLATE %EXACT,
    record_json     VARCHAR(32000) NOT NULL
);

CREATE TABLE Hlidac.login_code (
    code_id         VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    account_id      VARCHAR(64)  COLLATE %EXACT NOT NULL,
    created_at      TIMESTAMP    NOT NULL,
    expires_at      TIMESTAMP    NOT NULL,
    used_at         TIMESTAMP,
    record_json     VARCHAR(4000) NOT NULL         -- holds the code's hash, never the code
);
CREATE INDEX login_code_account ON Hlidac.login_code (account_id);

CREATE TABLE Hlidac.patient_plan (
    patient_id      VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    care_team_id    VARCHAR(64)  COLLATE %EXACT NOT NULL,  -- Store.list_patient_plans (WP13)
    record_json     VARCHAR(32000) NOT NULL
);
CREATE INDEX patient_plan_team ON Hlidac.patient_plan (care_team_id);

CREATE TABLE Hlidac.checkin (
    checkin_id      VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    patient_id      VARCHAR(64)  COLLATE %EXACT NOT NULL,
    checkin_status  VARCHAR(32)  NOT NULL,
    started_at      TIMESTAMP    NOT NULL,
    updated_at      TIMESTAMP    NOT NULL,
    record_json     VARCHAR(1000000) NOT NULL
);
CREATE INDEX checkin_patient ON Hlidac.checkin (patient_id);

CREATE TABLE Hlidac.assessment (              -- append-only, replayable
    assessment_id   VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    checkin_id      VARCHAR(64)  COLLATE %EXACT NOT NULL,
    patient_id      VARCHAR(64)  COLLATE %EXACT NOT NULL,
    assessment_status VARCHAR(32) NOT NULL,
    assessed_at     TIMESTAMP    NOT NULL,
    record_json     VARCHAR(200000) NOT NULL
);
CREATE INDEX assessment_patient ON Hlidac.assessment (patient_id);

CREATE TABLE Hlidac.grade_review (            -- append-only: a clinician confirms or corrects
    review_id       VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    assessment_id   VARCHAR(64)  COLLATE %EXACT NOT NULL,
    grade_id        VARCHAR(64)  COLLATE %EXACT NOT NULL,
    reviewed_at     TIMESTAMP    NOT NULL,         -- the grade's confirmed_at
    record_json     VARCHAR(32000) NOT NULL
);
CREATE INDEX grade_review_assessment ON Hlidac.grade_review (assessment_id);

CREATE TABLE Hlidac.alert (
    alert_id        VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    patient_id      VARCHAR(64)  COLLATE %EXACT NOT NULL,
    alert_status    VARCHAR(16)  NOT NULL,         -- open | acknowledged | resolved
    priority        VARCHAR(16)  NOT NULL,         -- urgent | routine
    created_at      TIMESTAMP    NOT NULL,
    ack_deadline    TIMESTAMP    NOT NULL,
    record_json     VARCHAR(32000) NOT NULL
);
CREATE INDEX alert_status_idx ON Hlidac.alert (alert_status, ack_deadline);

CREATE TABLE Hlidac.notification (
    notification_id VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    recipient_account_id VARCHAR(64) COLLATE %EXACT NOT NULL,
    created_at      TIMESTAMP    NOT NULL,
    read_at         TIMESTAMP,
    record_json     VARCHAR(32000) NOT NULL
);
CREATE INDEX notification_recipient ON Hlidac.notification (recipient_account_id);

CREATE TABLE Hlidac.outbox (
    outbox_id       VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    idempotency_key VARCHAR(200) COLLATE %EXACT NOT NULL UNIQUE,  -- same key twice: a no-op
    job_kind        VARCHAR(32)  NOT NULL,         -- fhir_transaction | email
    outbox_status   VARCHAR(16)  NOT NULL,         -- pending | done | failed
    attempts        INTEGER      NOT NULL DEFAULT 0,
    created_at      TIMESTAMP    NOT NULL,
    processed_at    TIMESTAMP,
    last_error      VARCHAR(4000),
    job_json        VARCHAR(1000000) NOT NULL      -- FhirTransactionJob or EmailJob
);
CREATE INDEX outbox_pending ON Hlidac.outbox (outbox_status, created_at);

CREATE TABLE Hlidac.audit_log (                -- append-only; includes exactly what models saw
    audit_id        VARCHAR(64)  COLLATE %EXACT NOT NULL PRIMARY KEY,
    logged_at       TIMESTAMP    NOT NULL,
    actor           VARCHAR(64)  COLLATE %EXACT NOT NULL,
    audit_action    VARCHAR(64)  NOT NULL,
    target          VARCHAR(200) COLLATE %EXACT,
    record_json     VARCHAR(1000000) NOT NULL
);
CREATE INDEX audit_target ON Hlidac.audit_log (target);
