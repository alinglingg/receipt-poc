"""Database tests never use DATABASE_URL or the application's .env file."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.repository import SqlAlchemyReceiptStore


@pytest.fixture
def postgres_schema():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable receipt_poc_test PostgreSQL database")
    if make_url(url).database != "receipt_poc_test":
        pytest.fail("TEST_DATABASE_URL must name the disposable receipt_poc_test database")
    schema = "test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def session_factory(request):
    if request.param == "postgres":
        engine = request.getfixturevalue("postgres_schema")
    else:
        engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    if request.param == "sqlite":
        engine.dispose()


@pytest.fixture
def store(session_factory):
    return SqlAlchemyReceiptStore(session_factory)
