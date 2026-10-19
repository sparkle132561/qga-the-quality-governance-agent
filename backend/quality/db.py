from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from quality.models import Merchant


def create_database(url: str):
    connect_args = {"connect_timeout": 5} if url.startswith("postgresql") else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    return engine, sessionmaker(engine, expire_on_commit=False)


def seed_merchants(session_factory) -> None:
    with session_factory.begin() as session:
        for merchant_id, name in [("merchant-a", "演示商家甲"), ("merchant-b", "演示商家乙")]:
            if session.scalar(select(Merchant).where(Merchant.id == merchant_id)) is None:
                session.add(Merchant(id=merchant_id, name=name))
