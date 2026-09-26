-- Dedicated, low-privilege schema for the ingest simulator.
-- Run ONCE as a DBA, connected to the target PDB (not CDB$ROOT):
--   sqlplus system@//localhost:1521/ORCLPDB1.localdomain @scripts/ingest_simulator_user.sql
--
-- A separate schema keeps demo rows out of SYSTEM, makes cleanup trivial
-- (DROP USER LOADGEN CASCADE), and the QUOTA caps how much space the demo
-- can consume. Raise or remove the quota if you want to demo a
-- tablespace-full incident as well.

CREATE USER LOADGEN IDENTIFIED BY "oracle"
    DEFAULT TABLESPACE USERS
    TEMPORARY TABLESPACE TEMP
    QUOTA 2G ON USERS;

GRANT CREATE SESSION TO LOADGEN;
GRANT CREATE TABLE   TO LOADGEN;

-- To remove everything after the demo:
--   DROP USER LOADGEN CASCADE;
