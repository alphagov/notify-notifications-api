from logging.config import fileConfig
from pathlib import Path

from alembic import context
from flask import current_app
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata

config.set_main_option("sqlalchemy.url", current_app.config.get("SQLALCHEMY_DATABASE_URI"))
target_metadata = current_app.extensions["migrate"].db.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    engine = engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    connection = engine.connect()
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    try:
        with context.begin_transaction():
            context.run_migrations()

        # if we're running on the main db (as opposed to the test db)
        if engine.url.database == "notification_api":
            with open(Path(__file__).parent / ".current-alembic-head", "w") as f:
                # write the current head to `.current-alembic-head`. This will prevent conflicting migrations
                # being merged at the same time and breaking the build.
                head = context.get_head_revision()
                f.write(head)
    finally:
        connection.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
