from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from mellow.models import Base


def create_database(database_url: str):
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_schema(session_factory):
    # Convenient for tests/local bootstrap; production uses the Alembic migration first.
    async with session_factory() as session:
        async with session.begin():
            await session.run_sync(Base.metadata.create_all)
