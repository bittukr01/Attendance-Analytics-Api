import os
import uuid
import asyncio

import pytest
import httpx
from pymongo import MongoClient

os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27018"
os.environ["MONGO_DB"] = f"attendance_test_{uuid.uuid4().hex}"

from app import main


@pytest.fixture(scope="session")
def mongo_db():
    database = main.db
    database.client.admin.command("ping")
    yield database
    database.client.drop_database(database.name)
    database.client.close()


@pytest.fixture(autouse=True)
def clean_db(mongo_db):
    mongo_db.employees.delete_many({})
    mongo_db.attendance_logs.delete_many({})
    main.ensure_indexes()


@pytest.fixture
def client(mongo_db):
    class SyncASGIClient:
        def request(self, method, url, **kwargs):
            async def send_request():
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
                    return await async_client.request(method, url, **kwargs)

            return asyncio.run(send_request())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

        def patch(self, url, **kwargs):
            return self.request("PATCH", url, **kwargs)

    yield SyncASGIClient()


def epoch_ms(value):
    return int(value.timestamp() * 1000)


def employee_payload(code="EMP0001", department="Engineering", **overrides):
    payload = {
        "emp_code": code,
        "name": f"Employee {code}",
        "email": f"{code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    payload.update(overrides)
    return payload
