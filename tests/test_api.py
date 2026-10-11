from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from conftest import employee_payload, epoch_ms
from bson import ObjectId
from fastapi import HTTPException
from pymongo.database import Database
from pymongo.errors import ServerSelectionTimeoutError

from app import main


def test_health_and_employee_pagination(client):
    assert client.get("/health").status_code == 200
    for number in range(1, 4):
        assert client.post("/employees", json=employee_payload(f"EMP{number:04d}", department="Ops" if number < 3 else "HR")).status_code == 201
    response = client.get("/employees", params={"department": "Ops", "page": 1, "page_size": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["items"][0]["emp_code"] == "EMP0001"


def test_health_reports_mongodb_unavailable(monkeypatch):
    def fail_ping(*args, **kwargs):
        raise ServerSelectionTimeoutError("test outage")

    monkeypatch.setattr(Database, "command", fail_ping)
    try:
        main.health()
    except HTTPException as error:
        assert error.status_code == 503
    else:
        raise AssertionError("health should return 503 when MongoDB is unavailable")


def test_employee_validation_and_duplicate(client):
    assert client.post("/employees", json=employee_payload("bad")).status_code == 422
    assert client.post("/employees", json=employee_payload()).status_code == 201
    assert client.post("/employees", json=employee_payload()).status_code == 409


def test_punch_in_ist_truncation_and_boundaries(client):
    assert client.post("/employees", json=employee_payload()).status_code == 201
    # 09:40:00.900 IST is exactly inside the grace period after truncation.
    instant = datetime(2026, 7, 6, 4, 10, 0, 900000, tzinfo=timezone.utc)
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_ms(instant)})
    assert response.status_code == 201
    body = response.json()
    assert body["date"] == "2026-07-06"
    assert body["punch_in"] == epoch_ms(instant.replace(microsecond=0))
    assert body["late_minutes"] == 0


def test_punch_in_rejects_bad_timestamp_and_duplicate(client):
    client.post("/employees", json=employee_payload())
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 999}).status_code == 422
    timestamp = epoch_ms(datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc))
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp}).status_code == 201
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp}).status_code == 409


def test_punch_in_concurrency_has_one_winner(client):
    client.post("/employees", json=employee_payload())
    timestamp = epoch_ms(datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc))

    def create_record():
        return client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp}).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: create_record(), range(2)))
    assert sorted(statuses) == [201, 409]


def test_half_up_work_hours_controls_half_day(client):
    client.post("/employees", json=employee_payload())
    punch_in = epoch_ms(datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc))
    punch_out = epoch_ms(datetime(2026, 7, 6, 8, 29, 59, tzinfo=timezone.utc))
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in})
    body = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out}).json()
    assert body["work_hours"] == 4.5
    assert body["half_day"] is False


def test_overnight_punch_out_and_derived_values(client):
    client.post("/employees", json=employee_payload(shift_start="22:00", shift_end="06:00"))
    punch_in = epoch_ms(datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc))
    punch_out = epoch_ms(datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc))
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in}).json()["date"] == "2026-07-06"
    body = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out}).json()
    assert body["work_hours"] == 8.75
    assert body["overtime_minutes"] == 40
    assert body["half_day"] is False


def test_punch_out_concurrency_has_one_winner(client):
    client.post("/employees", json=employee_payload())
    punch_in = epoch_ms(datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc))
    punch_out = epoch_ms(datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc))
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in})
    def close_record():
        return client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: close_record(), range(2)))
    assert sorted(statuses) == [200, 409]


def test_regularization_appends_actual_changes(client):
    client.post("/employees", json=employee_payload())
    punch_in = epoch_ms(datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc))
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in})
    corrected = epoch_ms(datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc))
    response = client.patch("/attendance/EMP0001/2026-07-06", json={"punch_in": corrected, "reason": "biometric fix", "regularized_by": "hr"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["history"]) == 1
    assert set(body["history"][0]["changes"]) == {"punch_in", "late_minutes"}
    assert client.patch("/attendance/EMP0001/2026-07-06", json={"reason": "same values", "regularized_by": "hr"}).status_code == 422


def test_regularization_preserves_legacy_missing_history(client, mongo_db):
    client.post("/employees", json=employee_payload())
    mongo_db.attendance_logs.insert_one({
        "_id": ObjectId(),
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })
    response = client.patch("/attendance/EMP0001/2026-07-06", json={"status": "WFH", "reason": "legacy correction", "regularized_by": "hr"})
    assert response.status_code == 200
    assert len(response.json()["history"]) == 1
    assert set(response.json()["history"][0]["changes"]) == {"status"}


def test_attendance_pagination_and_date_validation(client):
    client.post("/employees", json=employee_payload())
    for day in (6, 7):
        timestamp = epoch_ms(datetime(2026, 7, day, 4, 0, tzinfo=timezone.utc))
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp})
    response = client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"})
    assert response.status_code == 422
    response = client.get("/attendance", params={"page": 1, "page_size": 1})
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 1


def test_leaderboard_tie_cutoff(client):
    for number, elapsed_after_shift in enumerate((40, 30, 30, 20), 1):
        client.post("/employees", json=employee_payload(f"EMP{number:04d}"))
        timestamp = epoch_ms(datetime(2026, 7, 6, 4, elapsed_after_shift, tzinfo=timezone.utc))
        client.post("/attendance/punch-in", json={"emp_code": f"EMP{number:04d}", "punched_at": timestamp})
    response = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 3
    assert [item["rank"] for item in response.json()["items"]] == [1, 2, 2]


def test_analytics_use_stored_values_and_include_zero_log_headcount(client, mongo_db):
    client.post("/employees", json=employee_payload("EMP0001", department="Engineering"))
    client.post("/employees", json=employee_payload("EMP0002", department="Engineering", joined_on="2026-08-01"))
    mongo_db.attendance_logs.insert_one({
        "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
        "work_hours": 99.99, "late_minutes": 7, "overtime_minutes": 11,
        "half_day": False, "history": [],
    })
    monthly = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert monthly.status_code == 200
    assert monthly.json()["total_late_minutes"] == 7
    assert monthly.json()["total_overtime_minutes"] == 11
    summary = client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert summary.status_code == 200
    assert summary.json()["items"][0]["headcount"] == 1
    assert summary.json()["items"][0]["avg_work_hours"] == 99.99


def test_trend_fills_empty_days_and_explain_returns_raw_document(client):
    client.post("/employees", json=employee_payload())
    trend = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-12"})
    assert trend.status_code == 200
    assert [item["date"] for item in trend.json()["items"]] == [
        "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"
    ]
    explain = client.get("/admin/explain/attendance_list", params={"date_from": "2026-07-01", "date_to": "2026-07-31"})
    assert explain.status_code == 200
    assert explain.json()["collection"] == "attendance_logs"
    assert "queryPlanner" in explain.json()["explain"]
    requests = {
        "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
        "department_summary": {"month": "2026-07"},
        "late_leaderboard": {"month": "2026-07"},
        "department_trend": {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"},
    }
    for endpoint, params in requests.items():
        response = client.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200, (endpoint, response.text)
        explain = response.json()["explain"]
        assert "queryPlanner" in explain or "stages" in explain


def test_trend_validation_and_explain_contract(client):
    client.post("/employees", json=employee_payload())
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-10", "to": "2026-07-09"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-05"}).status_code == 422
    assert client.get("/admin/explain/employee_monthly").status_code == 422
