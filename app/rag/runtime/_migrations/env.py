"""Alembic CLI and Runtime share metadata, settings and transaction handling."""

import asyncio

from alembic import context
from alembic.operations import ops
from sqlalchemy import ForeignKeyConstraint, func, select
from sqlalchemy.schema import CreateSchema

from rag.runtime.postgresql._models import _Base


def _include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and name == "alembic_version")


def _process_revision_directives(migration_context, revision, directives):
    # Defer circular foreign keys until both tables exist. op.create_table
    # does not emit SQLAlchemy's deferred use_alter DDL automatically.
    for script in directives:
        deferred = []
        for operation in script.upgrade_ops.ops:
            if isinstance(operation, ops.CreateTableOp):
                for item in list(operation.columns):
                    if isinstance(item, ForeignKeyConstraint) and item.use_alter:
                        operation.columns = [column for column in operation.columns if column is not item]
                        deferred.append(ops.CreateForeignKeyOp.from_constraint(item))
        script.upgrade_ops.ops.extend(deferred)
        script.downgrade_ops.ops[:0] = [operation.reverse() for operation in reversed(deferred)]


def _run_connection(connection, schema):
    # Reflection must treat the configured search_path as the default schema.
    previous_schema = connection.dialect.default_schema_name
    connection.dialect.default_schema_name = schema
    context.configure(
        connection=connection,
        target_metadata=_Base.metadata,
        version_table_schema=schema,
        include_object=_include_object,
        compare_type=True,
        compare_server_default=True,
        autogenerate_plugins=["alembic.autogenerate.*", "alembic.ext.checkconstraint_byname"],
        process_revision_directives=_process_revision_directives,
    )
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.dialect.default_schema_name = previous_schema


async def _run_cli():
    from rag.runtime._config import RuntimeSettings
    from rag.runtime.lifecycle import _engine

    settings = RuntimeSettings()
    engine = _engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(728341, 1)))
            await connection.execute(CreateSchema(settings.pg_schema, if_not_exists=True))

            await connection.run_sync(_run_connection, settings.pg_schema)
    finally:
        await engine.dispose()


def _run():
    if context.is_offline_mode():
        raise RuntimeError("Runtime migrations require an online PostgreSQL connection")
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run_connection(connection, context.config.attributes["schema"])
    else:
        asyncio.run(_run_cli())


# Ordinary package inspection must do no I/O.
if __name__ == "env_py":
    _run()
