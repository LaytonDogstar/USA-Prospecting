"""Alembic environment. The database URL comes from DATABASE_URL (see ecosystem.config)."""
from alembic import context
from sqlalchemy import create_engine

from ecosystem.config import database_url
from ecosystem.models import Base

target_metadata = Base.metadata
url = context.config.attributes.get("url") or database_url()

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url).connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
