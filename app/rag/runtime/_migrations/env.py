from alembic import context


def _run():
    context.configure(connection=context.config.attributes["connection"], target_metadata=None,
                      version_table_schema=context.config.attributes["schema"])
    with context.begin_transaction():
        context.run_migrations()


# Alembic loads env.py as env_py. Ordinary package inspection must do no I/O.
if __name__ == "env_py":
    _run()
