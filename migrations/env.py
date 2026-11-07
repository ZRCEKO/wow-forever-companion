from alembic import context
from sqlalchemy import engine_from_config, pool

from goldmaking import models  # noqa: F401
from goldmaking.config import Settings
from goldmaking.db import Base

config = context.config
# Les tests peuvent injecter l'URL sans modifier l'environnement de l'utilisateur.
url = config.attributes.get("database_url") or Settings().database_url
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()
