"""FastAPI implementation for the HROne attendance assignment."""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("invalid date; expected YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    try:
        first = date.fromisoformat(value + "-01")
        if len(value) != 7 or value[4] != "-":
            raise ValueError
    except ValueError:
        raise ValueError("invalid month; expected YYYY-MM")
    next_month = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    return first, next_month - timedelta(days=1)


def to_datetime(value: Optional[int], now: bool = False) -> Optional[datetime]:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0) if now else None
    if type(value) is not int or not MIN_MS <= value <= MAX_MS:
        raise HTTPException(422, "timestamp must be integer epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)


def serial(value: Any) -> Any:
    if isinstance(value, datetime): return to_epoch(value)
    if isinstance(value, ObjectId): return str(value)
    if isinstance(value, list): return [serial(x) for x in value]
    if isinstance(value, dict): return {k: serial(v) for k, v in value.items() if k != "_id"}
    return value


def day_value(value: str) -> int: return int(value[:2]) * 60 + int(value[3:])


def shift_bounds(employee: dict, attendance_date: date) -> tuple[datetime, datetime]:
    start_minutes, end_minutes = day_value(employee["shift_start"]), day_value(employee["shift_end"])
    start = datetime.combine(attendance_date, time(start_minutes // 60, start_minutes % 60), IST)
    end_date = attendance_date + timedelta(days=1) if end_minutes <= start_minutes else attendance_date
    end = datetime.combine(end_date, time(end_minutes // 60, end_minutes % 60), IST)
    return start.astimezone(UTC), end.astimezone(UTC)


def attendance_date(punch_in: datetime, employee: dict) -> date:
    local = punch_in.astimezone(IST)
    result = local.date()
    if day_value(employee["shift_end"]) <= day_value(employee["shift_start"]):
        if local.hour * 60 + local.minute < day_value(employee["shift_end"]): result -= timedelta(days=1)
    return result


def derived(employee: dict, day: date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    if punch_in is None: return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = shift_bounds(employee, day)
    elapsed_late = (punch_in - start).total_seconds()
    late = int(elapsed_late // 60) if elapsed_late > 600 else 0
    if punch_out is None: return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = float((Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    overtime = int((punch_out - end).total_seconds() // 60)
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime if overtime >= 30 else 0, "half_day": hours < 4.50}


def rounded(value: Optional[float], places: int = 2) -> Optional[float]:
    return None if value is None else float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def employee_response(doc: dict) -> dict:
    return serial({k: doc[k] for k in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at")})


def attendance_response(doc: dict) -> dict:
    return serial({"emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"], "punch_in": doc.get("punch_in"), "punch_out": doc.get("punch_out"), "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0), "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False), "history": doc.get("history", [])})


def ensure_indexes():
    db.employees.create_index([('emp_code', ASCENDING)], unique=True, name='uq_employee_code')
    db.employees.create_index([('department', ASCENDING), ('emp_code', ASCENDING)], name='employee_department_code')
    db.employees.create_index([('joined_on', ASCENDING), ('department', ASCENDING)], name='employee_joined_department')
    db.attendance_logs.create_index([('emp_code', ASCENDING), ('date', ASCENDING)], unique=True, name='uq_attendance_day')
    db.attendance_logs.create_index([('date', DESCENDING), ('emp_code', ASCENDING)], name='attendance_date_code')
    db.attendance_logs.create_index([('date', ASCENDING), ('status', ASCENDING)], name='attendance_date_status')
    db.attendance_logs.create_index([('punch_in', DESCENDING)], name='attendance_punch_in')


@asynccontextmanager
async def lifespan(_app):
    try:
        ensure_indexes()
    except PyMongoError:
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_date(cls, value): parse_date(value); return value

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None and (type(value) is not int or not MIN_MS <= value <= MAX_MS): raise ValueError("invalid timestamp")
        return value

    @field_validator("status")
    @classmethod
    def presence(cls, value):
        if value not in PRESENCE: raise ValueError("invalid punch-in status")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None and (type(value) is not int or not MIN_MS <= value <= MAX_MS): raise ValueError("invalid timestamp")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in STATUSES: raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamp(cls, value):
        if value is not None and (type(value) is not int or not MIN_MS <= value <= MAX_MS): raise ValueError("invalid timestamp")
        return value


def date_range(date_from, date_to):
    try:
        if date_from: parse_date(date_from)
        if date_to: parse_date(date_to)
    except ValueError as exc: raise HTTPException(422, str(exc))
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not be after date_to")


@app.get('/health')
def health():
    try:
        client.admin.command('ping')
        ensure_indexes()
    except PyMongoError: raise HTTPException(503, 'MongoDB unavailable')
    return {'status': 'ok'}


@app.post('/employees', status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(); doc['created_at'] = datetime.now(UTC).replace(microsecond=0)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, 'emp_code already exists')
    return employee_response(doc)


@app.get('/employees')
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {'department': department} if department is not None else {}
    cursor = db.employees.find(query, {'_id': 0}).sort('emp_code', ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {'items': [employee_response(x) for x in cursor], 'total': db.employees.count_documents(query), 'page': page, 'page_size': page_size}


@app.post('/attendance/punch-in', status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({'emp_code': body.emp_code})
    if not employee: raise HTTPException(404, 'employee not found')
    punched = to_datetime(body.punched_at, True); day = attendance_date(punched, employee)
    doc = {'emp_code': body.emp_code, 'date': day.isoformat(), 'status': body.status, 'punch_in': punched, 'punch_out': None, 'history': []}
    doc.update(derived(employee, day, punched, None))
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, 'already punched in for this date')
    return attendance_response(doc)


@app.post('/attendance/punch-out')
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({'emp_code': body.emp_code})
    if not employee: raise HTTPException(404, 'employee not found')
    punched = to_datetime(body.punched_at, True)
    candidate = db.attendance_logs.find_one({'emp_code': body.emp_code, 'punch_in': {'$lte': punched}}, sort=[('punch_in', DESCENDING)])
    if not candidate: raise HTTPException(404, 'no eligible punch-in found')
    if candidate.get('punch_out') is not None: raise HTTPException(409, 'attendance record already punched out')
    elapsed = (punched - candidate['punch_in']).total_seconds()
    if elapsed <= 0 or elapsed > 86400: raise HTTPException(422, 'punch-out must be after punch-in and within 24 hours')
    values = derived(employee, parse_date(candidate['date']), candidate['punch_in'], punched)
    result = db.attendance_logs.find_one_and_update({'_id': candidate['_id'], 'punch_out': None}, {'$set': {'punch_out': punched, **values}}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, 'attendance record already punched out')
    return attendance_response(result)


@app.get('/attendance')
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    date_range(date_from, date_to)
    if status and status not in STATUSES: raise HTTPException(422, 'invalid status')
    query = {k: v for k, v in [('emp_code', emp_code), ('status', status)] if v is not None}
    if date_from or date_to: query['date'] = {k: v for k, v in [('$gte', date_from), ('$lte', date_to)] if v is not None}
    cursor = db.attendance_logs.find(query, {'_id': 0}).sort([('date', DESCENDING), ('emp_code', ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {'items': [attendance_response(x) for x in cursor], 'total': db.attendance_logs.count_documents(query), 'page': page, 'page_size': page_size}


@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code: str, date: str, body: RegularizeIn):
    try: target = parse_date(date)
    except ValueError as exc: raise HTTPException(422, str(exc))
    employee = db.employees.find_one({'emp_code': emp_code})
    current = db.attendance_logs.find_one({'emp_code': emp_code, 'date': date})
    if not employee or not current: raise HTTPException(404, 'employee or attendance record not found')
    status = body.status if body.status is not None else current['status']
    pin = to_datetime(body.punch_in) if body.punch_in is not None else current.get('punch_in')
    pout = to_datetime(body.punch_out) if body.punch_out is not None else current.get('punch_out')
    if status in {'ABSENT', 'LEAVE'}:
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422, 'ABSENT and LEAVE cannot have punch times')
        pin = pout = None
    elif pin is None: raise HTTPException(422, 'presence status requires punch_in')
    if pin is not None:
        if attendance_date(pin, employee) != target: raise HTTPException(422, 'punch_in does not belong to attendance date')
        if pout is not None and ((pout - pin).total_seconds() <= 0 or (pout - pin).total_seconds() > 86400): raise HTTPException(422, 'invalid punch_out')
    values = {'work_hours': None, 'late_minutes': 0, 'overtime_minutes': 0, 'half_day': False} if status not in PRESENCE else derived(employee, target, pin, pout)
    proposed = {'status': status, 'punch_in': pin, 'punch_out': pout, **values}
    tracked = ['status', 'punch_in', 'punch_out', 'work_hours', 'late_minutes', 'overtime_minutes', 'half_day']
    defaults = {'work_hours': None, 'late_minutes': 0, 'overtime_minutes': 0, 'half_day': False}
    old_values = {k: current.get(k, defaults.get(k)) for k in tracked}
    changes = {k: {'from': old_values[k], 'to': proposed[k]} for k in tracked if old_values[k] != proposed[k]}
    if not changes: raise HTTPException(422, 'correction changes nothing')
    entry = {'at': datetime.now(UTC).replace(microsecond=0), 'by': body.regularized_by, 'reason': body.reason, 'changes': changes}
    result = db.attendance_logs.find_one_and_update({'_id': current['_id'], '$or': [{'history': current.get('history', [])}, {'history': {'$exists': False}}]}, {'$set': proposed, '$push': {'history': entry}}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, 'attendance record was changed concurrently')
    return attendance_response(result)


def working_days(start: date, end: date) -> int:
    result = 0
    while start <= end:
        result += start.weekday() < 5; start += timedelta(days=1)
    return result


def month_or_422(month):
    try: return parse_month(month)
    except ValueError as exc: raise HTTPException(422, str(exc))


@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code: str, month: str = Query(...)):
    first, last = month_or_422(month); employee = db.employees.find_one({'emp_code': emp_code})
    if not employee: raise HTTPException(404, 'employee not found')
    joined = parse_date(employee['joined_on'])
    match = {'emp_code': emp_code, 'date': {'$gte': max(first, joined).isoformat(), '$lte': last.isoformat()}}
    pipeline = [{'$match': match}, {'$group': {'_id': None, 'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', list(PRESENCE)]}, {'$in': [{'$dayOfWeek': {'$dateFromString': {'dateString': '$date'}}}, [2, 3, 4, 5, 6]]}]}, {'$cond': [{'$eq': [{'$ifNull': ['$half_day', False]}, True]}, 0.5, 1]}, 0]}}, 'leave_days': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}, 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}}, 'total_overtime_minutes': {'$sum': {'$ifNull': ['$overtime_minutes', 0]}}}}]
    aggregate = next(db.attendance_logs.aggregate(pipeline), {})
    present = aggregate.get('present_days', 0)
    joined = max(first, joined); days = working_days(joined, last) if joined <= last else 0
    return {'emp_code': emp_code, 'month': month, 'working_days': days, 'present_days': rounded(present), 'leave_days': aggregate.get('leave_days', 0), 'late_count': aggregate.get('late_count', 0), 'total_late_minutes': aggregate.get('total_late_minutes', 0), 'total_overtime_minutes': aggregate.get('total_overtime_minutes', 0), 'attendance_pct': rounded(present / days * 100, 4) if days else None}


def department_pipeline(month, department=None):
    first, last = parse_month(month); match = {'joined_on': {'$lte': last.isoformat()}}
    if department is not None: match['department'] = department
    return [{'$match': match}, {'$lookup': {'from': 'attendance_logs', 'let': {'code': '$emp_code', 'joined': '$joined_on'}, 'pipeline': [{'$match': {'$expr': {'$and': [{'$eq': ['$emp_code', '$$code']}, {'$gte': ['$date', first.isoformat()]}, {'$lte': ['$date', last.isoformat()]}, {'$gte': ['$date', '$$joined']}]}}}], 'as': 'logs'}}, {'$unwind': {'path': '$logs', 'preserveNullAndEmptyArrays': True}}, {'$group': {'_id': '$department', 'headcount': {'$sum': 1}, 'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$logs.status', list(PRESENCE)]}, {'$in': [{'$dayOfWeek': {'$dateFromString': {'dateString': '$logs.date'}}}, [2, 3, 4, 5, 6]]}]}, {'$cond': [{'$eq': [{'$ifNull': ['$logs.half_day', False]}, True]}, 0.5, 1]}, 0]}}, 'avg_work_hours': {'$avg': {'$cond': [{'$and': [{'$in': ['$logs.status', list(PRESENCE)]}, {'$ne': ['$logs.work_hours', None]}]}, '$logs.work_hours', None]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$logs.late_minutes', 0]}, 0]}, 1, 0]}}, 'total_late_minutes': {'$sum': {'$ifNull': ['$logs.late_minutes', 0]}}, 'leave_count': {'$sum': {'$cond': [{'$eq': ['$logs.status', 'LEAVE']}, 1, 0]}}, 'on_duty_count': {'$sum': {'$cond': [{'$eq': ['$logs.status', 'ON_DUTY']}, 1, 0]}}}}, {'$sort': {'_id': 1}}]


@app.get('/analytics/departments/summary')
def department_summary(month: str = Query(...), department: Optional[str] = None):
    month_or_422(month); items = []
    for doc in db.employees.aggregate(department_pipeline(month, department)):
        items.append({'department': doc.pop('_id'), 'headcount': doc['headcount'], 'present_days': rounded(doc['present_days']), 'avg_work_hours': rounded(doc.get('avg_work_hours')), 'late_count': doc['late_count'], 'total_late_minutes': doc['total_late_minutes'], 'leave_count': doc['leave_count'], 'on_duty_count': doc['on_duty_count']})
    return {'month': month, 'items': items}


@app.get('/analytics/leaderboard/late')
def leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {'month': month, 'items': list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))}


def leaderboard_pipeline(month, limit, department=None):
    first, last = month_or_422(month)
    pipeline = [{'$match': {'date': {'$gte': first.isoformat(), '$lte': last.isoformat()}}}, {'$group': {'_id': '$emp_code', 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}}}, {'$match': {'total_late_minutes': {'$gt': 0}}}, {'$lookup': {'from': 'employees', 'localField': '_id', 'foreignField': 'emp_code', 'as': 'employee'}}, {'$unwind': '$employee'}]
    if department is not None: pipeline.append({'$match': {'employee.department': department}})
    pipeline += [{'$setWindowFields': {'sortBy': {'total_late_minutes': -1}, 'output': {'rank': {'$rank': {}}}}}, {'$match': {'rank': {'$lte': limit}}}, {'$sort': {'total_late_minutes': -1, '_id': 1}}, {'$project': {'_id': 0, 'rank': 1, 'emp_code': '$_id', 'name': '$employee.name', 'department': '$employee.department', 'total_late_minutes': 1, 'late_count': 1}}]
    return pipeline


def trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    start_dt = datetime.combine(start, time.min, UTC)
    end_dt = datetime.combine(end, time.min, UTC)
    log_pipeline = [{'$match': {'date': {'$gte': start.isoformat(), '$lte': end.isoformat()}}}, {'$lookup': {'from': 'employees', 'localField': 'emp_code', 'foreignField': 'emp_code', 'as': 'employee'}}, {'$unwind': '$employee'}, {'$match': {'employee.department': department}}, {'$group': {'_id': {'$dateFromString': {'dateString': '$date'}}, 'present_count': {'$sum': {'$cond': [{'$in': ['$status', list(PRESENCE)]}, {'$cond': ['$half_day', 0.5, 1]}, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}}}]
    return [{'$documents': [{'_id': start_dt, 'present_count': 0, 'late_count': 0}, {'_id': end_dt, 'present_count': 0, 'late_count': 0}]}, {'$unionWith': {'coll': 'attendance_logs', 'pipeline': log_pipeline}}, {'$group': {'_id': '$_id', 'present_count': {'$max': '$present_count'}, 'late_count': {'$max': '$late_count'}}}, {'$densify': {'field': '_id', 'range': {'step': 1, 'unit': 'day', 'bounds': [start_dt, end_dt]}}}, {'$set': {'date': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$_id'}}}}, {'$lookup': {'from': 'employees', 'let': {'day': '$date'}, 'pipeline': [{'$match': {'department': department, '$expr': {'$lte': ['$joined_on', '$$day']}}}, {'$count': 'n'}], 'as': 'heads'}}, {'$set': {'headcount': {'$ifNull': [{'$arrayElemAt': ['$heads.n', 0]}, 0]}, 'present_count': {'$ifNull': ['$present_count', 0]}, 'late_count': {'$ifNull': ['$late_count', 0]}}}, {'$set': {'is_working_day': {'$and': [{'$gte': [{'$dayOfWeek': '$_id'}, 2]}, {'$lte': [{'$dayOfWeek': '$_id'}, 6]}]}, 'attendance_rate': {'$cond': [{'$and': [{'$gte': [{'$dayOfWeek': '$_id'}, 2]}, {'$lte': [{'$dayOfWeek': '$_id'}, 6]}, {'$gt': ['$headcount', 0]}]}, {'$divide': ['$present_count', '$headcount']}, None]}}}, {'$setWindowFields': {'sortBy': {'_id': 1}, 'output': {'rates': {'$push': '$attendance_rate', 'window': {'documents': [-6, 0]}}}}}, {'$set': {'moving_avg_7d': {'$let': {'vars': {'valid': {'$filter': {'input': '$rates', 'as': 'rate', 'cond': {'$ne': ['$$rate', None]}}}}, 'in': {'$cond': [{'$gt': [{'$size': '$$valid'}, 0]}, {'$avg': '$$valid'}, None]}}}}}, {'$sort': {'_id': 1}}, {'$project': {'_id': 0, 'date': 1, 'is_working_day': 1, 'headcount': 1, 'present_count': 1, 'late_count': 1, 'attendance_rate': 1, 'moving_avg_7d': 1}}]


def execute_trend(department: str, start: date, end: date) -> list[dict]:
    response = db.command('aggregate', 1, pipeline=trend_pipeline(department, start, end), cursor={})
    items = response['cursor']['firstBatch']
    for item in items:
        item['attendance_rate'], item['moving_avg_7d'] = rounded(item['attendance_rate'], 4), rounded(item['moving_avg_7d'], 4)
    return items


@app.get('/analytics/departments/{department}/trend')
def trend(department: str, from_date: str = Query(..., alias='from'), to_date: str = Query(..., alias='to')):
    try: start, end = parse_date(from_date), parse_date(to_date)
    except ValueError as exc: raise HTTPException(422, str(exc))
    if end < start or (end - start).days + 1 > 92: raise HTTPException(422, 'invalid trend range')
    if not db.employees.find_one({'department': department}): raise HTTPException(404, 'department not found')
    return {'department': department, 'items': execute_trend(department, start, end)}
    pipeline = [{'$match': {'date': {'$gte': from_date, '$lte': to_date}}}, {'$lookup': {'from': 'employees', 'localField': 'emp_code', 'foreignField': 'emp_code', 'as': 'employee'}}, {'$unwind': '$employee'}, {'$match': {'employee.department': department}}, {'$group': {'_id': {'$dateFromString': {'dateString': '$date'}}, 'present_count': {'$sum': {'$cond': [{'$in': ['$status', list(PRESENCE)]}, {'$cond': ['$half_day', 0.5, 1]}, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}}}, {'$unionWith': {'coll': 'attendance_logs', 'pipeline': [{'$documents': [{'_id': datetime.combine(start, time.min, UTC), 'present_count': 0, 'late_count': 0}, {'_id': datetime.combine(end, time.min, UTC), 'present_count': 0, 'late_count': 0}]}]}}, {'$group': {'_id': '$_id', 'present_count': {'$max': '$present_count'}, 'late_count': {'$max': '$late_count'}}}, {'$densify': {'field': '_id', 'range': {'step': 1, 'unit': 'day', 'bounds': [datetime.combine(start, time.min), datetime.combine(end, time.min)]}}}, {'$set': {'date': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$_id'}}}}, {'$lookup': {'from': 'employees', 'let': {'day': '$date'}, 'pipeline': [{'$match': {'department': department, '$expr': {'$lte': ['$joined_on', '$$day']}}}, {'$count': 'n'}], 'as': 'heads'}}, {'$set': {'headcount': {'$ifNull': [{'$arrayElemAt': ['$heads.n', 0]}, 0]}, 'present_count': {'$ifNull': ['$present_count', 0]}, 'late_count': {'$ifNull': ['$late_count', 0]}}}, {'$set': {'is_working_day': {'$and': [{'$gte': [{'$dayOfWeek': '$_id'}, 2]}, {'$lte': [{'$dayOfWeek': '$_id'}, 6]}]}, 'attendance_rate': {'$cond': [{'$and': [{'$gte': [{'$dayOfWeek': '$_id'}, 2]}, {'$lte': [{'$dayOfWeek': '$_id'}, 6]}, {'$gt': ['$headcount', 0]}]}, {'$divide': ['$present_count', '$headcount']}, None]}}}, {'$setWindowFields': {'sortBy': {'_id': 1}, 'output': {'rates': {'$push': '$attendance_rate', 'window': {'documents': [-6, 0]}}}}}, {'$set': {'moving_avg_7d': {'$let': {'vars': {'valid': {'$filter': {'input': '$rates', 'as': 'rate', 'cond': {'$ne': ['$$rate', None]}}}}, 'in': {'$cond': [{'$gt': [{'$size': '$$valid'}, 0]}, {'$avg': '$$valid'}, None]}}}}}, {'$sort': {'_id': 1}}, {'$project': {'_id': 0, 'date': 1, 'is_working_day': 1, 'headcount': 1, 'present_count': 1, 'late_count': 1, 'attendance_rate': 1, 'moving_avg_7d': 1}}]
    items = list(db.attendance_logs.aggregate(pipeline))
    for item in items: item['attendance_rate'], item['moving_avg_7d'] = rounded(item['attendance_rate'], 4), rounded(item['moving_avg_7d'], 4)
    return {'department': department, 'items': items}


@app.get('/admin/explain/{endpoint}')
def explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, from_date: Optional[str] = Query(None, alias='from'), to_date: Optional[str] = Query(None, alias='to'), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint not in {'attendance_list', 'employee_monthly', 'department_summary', 'late_leaderboard', 'department_trend'}: raise HTTPException(422, 'unsupported endpoint')
    if endpoint in {'employee_monthly', 'department_summary', 'late_leaderboard'} and not month: raise HTTPException(422, 'month is required')
    if endpoint == 'employee_monthly' and not emp_code: raise HTTPException(422, 'emp_code is required')
    if endpoint == 'department_trend' and (not department or not from_date or not to_date): raise HTTPException(422, 'department, from, and to are required')
    if endpoint == 'attendance_list':
        date_range(date_from, date_to)
        if status and status not in STATUSES: raise HTTPException(422, 'invalid status')
        query = {k: v for k, v in [('emp_code', emp_code), ('status', status)] if v is not None}
        if date_from or date_to: query['date'] = {k: v for k, v in [('$gte', date_from), ('$lte', date_to)] if v is not None}
        result = db.command('explain', {'find': 'attendance_logs', 'filter': query, 'sort': {'date': -1, 'emp_code': 1}, 'skip': (page - 1) * page_size, 'limit': page_size}, verbosity='executionStats')
        collection = 'attendance_logs'
    elif endpoint == 'employee_monthly':
        first, last = month_or_422(month)
        employee = db.employees.find_one({'emp_code': emp_code})
        joined = parse_date(employee['joined_on']) if employee else last + timedelta(days=1)
        match = {'emp_code': emp_code, 'date': {'$gte': max(first, joined).isoformat(), '$lte': last.isoformat()}}
        pipeline = [{'$match': match}, {'$group': {'_id': None, 'present_days': {'$sum': {'$cond': [{'$and': [{'$in': ['$status', list(PRESENCE)]}, {'$in': [{'$dayOfWeek': {'$dateFromString': {'dateString': '$date'}}}, [2, 3, 4, 5, 6]]}]}, {'$cond': [{'$eq': [{'$ifNull': ['$half_day', False]}, True]}, 0.5, 1]}, 0]}}, 'leave_days': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}}, 'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}, 'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}}, 'total_overtime_minutes': {'$sum': {'$ifNull': ['$overtime_minutes', 0]}}}}]
        result, collection = db.command('explain', {'aggregate': 'attendance_logs', 'pipeline': pipeline, 'cursor': {}}, verbosity='executionStats'), 'attendance_logs'
    elif endpoint == 'department_summary':
        result, collection = db.command('explain', {'aggregate': 'employees', 'pipeline': department_pipeline(month, department), 'cursor': {}}, verbosity='executionStats'), 'employees'
    elif endpoint == 'late_leaderboard':
        pipeline = leaderboard_pipeline(month, limit, department)
        result, collection = db.command('explain', {'aggregate': 'attendance_logs', 'pipeline': pipeline, 'cursor': {}}, verbosity='executionStats'), 'attendance_logs'
    else:
        try: start, end = parse_date(from_date), parse_date(to_date)
        except ValueError as exc: raise HTTPException(422, str(exc))
        if end < start or (end - start).days + 1 > 92: raise HTTPException(422, 'invalid trend range')
        result, collection = db.command('explain', {'aggregate': 1, 'pipeline': trend_pipeline(department, start, end), 'cursor': {}}, verbosity='executionStats'), 'attendance_logs'
    return {'endpoint': endpoint, 'collection': collection, 'explain': serial(result)}
