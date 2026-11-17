"""Idempotently provision analytics views and a reader after application migrations."""

import asyncio
import sys

from .schema import READER, SCHEMA, literal, views


async def provision(connection, application_schema, password):
    if not password.strip():
        raise ValueError("Grafana analytics password must be nonempty")
    async with connection.transaction():
        await connection.execute("SET LOCAL standard_conforming_strings = on")
        await connection.execute("SELECT pg_advisory_xact_lock(19820731, 1)")
        if not await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", READER):
            await connection.execute(f"CREATE ROLE {READER} LOGIN")
        await connection.execute(
            f"ALTER ROLE {READER} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"NOREPLICATION NOBYPASSRLS PASSWORD {literal(password)}"
        )
        await connection.execute(f"ALTER ROLE {READER} SET default_transaction_read_only = on")
        await connection.execute(f"ALTER ROLE {READER} SET statement_timeout = '10s'")
        await connection.execute(views(application_schema))
        await connection.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
        await connection.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM PUBLIC")
        await connection.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {READER}")
        await connection.execute(f"GRANT SELECT ON {SCHEMA}.requests TO {READER}")


async def main():
    import asyncpg
    from rag._configuration import environment_values
    from rag.runtime._config import RuntimeSettings

    settings = RuntimeSettings()
    values = environment_values()
    password = (values.get("ANALYTICS_PASSWORD") or values.get("GRAFANA_ANALYTICS_PASSWORD")
                or values.get("GRAFANA_PASSWORD", ""))
    connection = await asyncpg.connect(
        host=settings.pg_host, port=settings.pg_port, database=settings.pg_database,
        user=settings.pg_user, password=settings.pg_password.get_secret_value(),
        timeout=15, command_timeout=30,
    )
    try:
        await provision(connection, settings.pg_schema, password)
    finally:
        await connection.close()
    print("Business analytics views and Grafana reader ready; secrets omitted", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Database errors may include SQL/password literals. Never print their text.
        print("Analytics setup failed: " + type(error).__name__, file=sys.stderr, flush=True)
        sys.exit(1)
