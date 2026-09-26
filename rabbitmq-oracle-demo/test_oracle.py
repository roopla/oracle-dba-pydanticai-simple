import os
import sys

import oracledb
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()

    dsn = oracledb.makedsn(
        host=os.environ["ORACLE_HOST"],
        port=int(os.environ["ORACLE_PORT"]),
        service_name=os.environ["ORACLE_SERVICE"],
    )

    try:
        with oracledb.connect(
            user=os.environ["ORACLE_USER"],
            password=os.environ["ORACLE_PASSWORD"],
            dsn=dsn,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        SYS_CONTEXT('USERENV', 'DB_NAME'),
                        SYS_CONTEXT('USERENV', 'CON_NAME'),
                        USER
                    FROM dual
                    """
                )

                db_name, container_name, username = cursor.fetchone()

                print("Oracle connection succeeded")
                print(f"Database:  {db_name}")
                print(f"Container: {container_name}")
                print(f"User:      {username}")

    except oracledb.Error as exc:
        print(f"Oracle connection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
