# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` | The skip offset used `page * page_size`, so page 1 skipped the first page, and `total` ignored the department filter. | Create 21 employees, request page 1, and request a department filter; the first page is empty or the total is too large. | Use `(page - 1) * page_size` and pass the same filter to `count_documents`. |
| 2 | `health` | It returned success without checking MongoDB. | Stop MongoDB and call `/health`; the starter still returned 200. | Ping MongoDB and return 503 on a PyMongo failure. |
| 3 | `create_employee` | The find-then-insert sequence was not concurrency-safe and `created_at` was a naive local datetime. | Send simultaneous duplicate creates or inspect stored timezone data. | Add a unique index and handle `DuplicateKeyError`; store UTC BSON datetimes. |
| 4 | `punch_in` | It did not validate the employee, used local time, calculated the date incorrectly for IST and overnight shifts, and allowed duplicate races. | Unknown employee, an early-morning overnight punch, or simultaneous requests exposed the errors. | Use validated epoch milliseconds, IST date rules, and the unique compound index. |
| 5 | `compute_work_hours` / `compute_overtime` | Python `round` is not half-up, and overtime ignored the 30-minute threshold and overnight end date. | Values exactly at a half-cent or at 29/30 minutes after shift end produce wrong derived fields. | Use Decimal `ROUND_HALF_UP`, correct shift bounds, and the threshold. |
| 6 | `list_attendance` | It loaded and sorted the entire collection in Python and exposed MongoDB IDs. | A large collection causes unbounded memory use; responses contain `id`. | Use MongoDB filtering, sorting, counting, skip/limit, and explicit response serialization. |
| 7 | Missing endpoints | Punch-out, regularization, all analytics, and explain were not implemented. | The routes were absent from the OpenAPI route list. | Implemented the required routes in `app/main.py`. |

Additional defects found during the contract audit:

| # | Where | What was wrong | How it was observed | Fix |
|---|---|---|---|---|
| 8 | Monthly analytics | Presence was fetched into Python instead of being calculated in MongoDB. | The implementation contained an unbounded `find`/`list` for monthly records. | Moved stored metric aggregation, weekday filtering, and half-day contribution into one MongoDB pipeline. |
| 9 | Department summary | Logs dated before an employee's `joined_on` could contribute to department metrics. | A seeded log before the join date changed the summary. | Added the join-date predicate inside the attendance lookup and calculate averages in MongoDB. |
| 10 | Regularization | Legacy records missing `history` returned 409 instead of being updateable. Missing derived fields were also reported as changes from `None`. | A seeded legacy-shaped record failed PATCH and produced false audit changes. | Match missing history safely and normalize documented default values when building `changes`. |
| 11 | Department trend | `$densify` had no boundary source when the range had no logs and could omit the inclusive final date. | An empty seven-day range returned six or zero rows. | Seed range boundaries with MongoDB `$documents`, union the filtered logs, then densify. |
| 12 | Explain endpoint | Several branches explained a reduced query, and attendance used an incompatible PyMongo `Cursor.explain` call. | Explain calls either failed at runtime or did not match the endpoint pipeline. | Use the same filters and aggregation builders, and call MongoDB's executionStats explain command directly. |
| 13 | Startup lifecycle | Index creation could prevent the app from starting when MongoDB was down, making `/health` unable to return 503. | A startup connection failure aborted the ASGI app. | Tolerate startup failure, then retry index creation after a successful health ping. |

Investigated but not treated as defects: the starter's default shift values matched the contract, and the supplied sample data intentionally omits some legacy fields. The implementation therefore defaults missing `history`, `half_day`, and numeric derived fields at response/calculation boundaries rather than rewriting seeded documents.

Also note anything you looked at and decided was **not** a defect, and why.
