-- ---------------------------------------------------------------------
-- 01-primary-prep.sql
-- Prepares the ORCLCDB primary for a physical standby (ORCLCDB_STBY).
--
-- Run inside the oracle19c container as SYSDBA:
--     docker exec -it oracle19c bash
--     sqlplus / as sysdba @/tmp/01-primary-prep.sql
--
-- Safe to re-run: every step is guarded or idempotent.
-- ---------------------------------------------------------------------

SET SERVEROUTPUT ON
SET LINESIZE 200
SET PAGESIZE 100

PROMPT ====== BEFORE ======
SELECT name, db_unique_name, log_mode, force_logging, open_mode,
       database_role
FROM   v$database;

SELECT group#, thread#, bytes/1024/1024 AS mb, members, status
FROM   v$log ORDER BY group#;

-- ---------------------------------------------------------------------
-- 1. ARCHIVELOG mode (requires a restart if currently NOARCHIVELOG)
-- ---------------------------------------------------------------------
DECLARE
    v_log_mode  VARCHAR2(30);
BEGIN
    SELECT log_mode INTO v_log_mode FROM v$database;

    IF v_log_mode = 'ARCHIVELOG' THEN
        DBMS_OUTPUT.PUT_LINE('ARCHIVELOG already enabled - skipping.');
    ELSE
        DBMS_OUTPUT.PUT_LINE(
            '*** NOARCHIVELOG. Run the block printed below, then re-run '
            || 'this script. This bounces the database.');
        DBMS_OUTPUT.PUT_LINE('  SHUTDOWN IMMEDIATE;');
        DBMS_OUTPUT.PUT_LINE('  STARTUP MOUNT;');
        DBMS_OUTPUT.PUT_LINE('  ALTER DATABASE ARCHIVELOG;');
        DBMS_OUTPUT.PUT_LINE('  ALTER DATABASE OPEN;');
        RAISE_APPLICATION_ERROR(-20001, 'Enable ARCHIVELOG first.');
    END IF;
END;
/

-- ---------------------------------------------------------------------
-- 2. FORCE LOGGING - stops NOLOGGING operations creating unrecoverable
--    blocks on the standby.
-- ---------------------------------------------------------------------
DECLARE
    v_force VARCHAR2(10);
BEGIN
    SELECT force_logging INTO v_force FROM v$database;

    IF v_force = 'YES' THEN
        DBMS_OUTPUT.PUT_LINE('FORCE LOGGING already on - skipping.');
    ELSE
        EXECUTE IMMEDIATE 'ALTER DATABASE FORCE LOGGING';
        DBMS_OUTPUT.PUT_LINE('FORCE LOGGING enabled.');
    END IF;
END;
/

-- ---------------------------------------------------------------------
-- 3. Standby redo logs.
--    Rule: (online groups per thread + 1), same size as online redo.
--    These live on the PRIMARY too, so it is ready to become a standby
--    after a switchover.
-- ---------------------------------------------------------------------
DECLARE
    v_size_mb     NUMBER;
    v_online_grps NUMBER;
    v_stby_grps   NUMBER;
    v_next_grp    NUMBER;
    v_needed      NUMBER;
BEGIN
    SELECT MAX(bytes)/1024/1024, COUNT(*)
      INTO v_size_mb, v_online_grps
      FROM v$log WHERE thread# = 1;

    SELECT COUNT(*) INTO v_stby_grps
      FROM v$standby_log WHERE thread# = 1;

    v_needed := v_online_grps + 1;

    IF v_stby_grps >= v_needed THEN
        DBMS_OUTPUT.PUT_LINE(
            'Standby redo logs already present (' || v_stby_grps
            || ') - skipping.');
    ELSE
        SELECT NVL(MAX(group#), 0) INTO v_next_grp
          FROM (SELECT group# FROM v$log
                UNION ALL
                SELECT group# FROM v$standby_log);

        FOR i IN 1 .. (v_needed - v_stby_grps) LOOP
            v_next_grp := v_next_grp + 1;
            EXECUTE IMMEDIATE
                'ALTER DATABASE ADD STANDBY LOGFILE THREAD 1 GROUP '
                || v_next_grp || ' SIZE ' || v_size_mb || 'M';
            DBMS_OUTPUT.PUT_LINE(
                'Added standby redo group ' || v_next_grp
                || ' (' || v_size_mb || 'M)');
        END LOOP;
    END IF;
END;
/

-- ---------------------------------------------------------------------
-- 4. Data Guard parameters.
--    Both databases keep identical file paths (separate containers,
--    separate volumes), so no *_file_name_convert is needed.
-- ---------------------------------------------------------------------
ALTER SYSTEM SET log_archive_config =
    'DG_CONFIG=(ORCLCDB,ORCLCDB_STBY)' SCOPE=BOTH;

ALTER SYSTEM SET log_archive_dest_2 =
    'SERVICE=ORCLCDB_STBY ASYNC VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME=ORCLCDB_STBY'
    SCOPE=BOTH;

ALTER SYSTEM SET log_archive_dest_state_2 = 'ENABLE' SCOPE=BOTH;

ALTER SYSTEM SET fal_server = 'ORCLCDB_STBY' SCOPE=BOTH;

ALTER SYSTEM SET standby_file_management = 'AUTO' SCOPE=BOTH;

-- Needed so RMAN can authenticate remotely with the password file.
ALTER SYSTEM SET remote_login_passwordfile = 'EXCLUSIVE' SCOPE=SPFILE;

PROMPT ====== AFTER ======
SELECT name, db_unique_name, log_mode, force_logging, database_role
FROM   v$database;

SELECT group#, thread#, bytes/1024/1024 AS mb, status
FROM   v$standby_log ORDER BY group#;

SELECT name, value FROM v$parameter
WHERE  name IN ('log_archive_config','log_archive_dest_2','fal_server',
                'standby_file_management','db_unique_name')
ORDER BY name;

PROMPT
PROMPT Primary prep complete. Next: 02-create-standby.sh
