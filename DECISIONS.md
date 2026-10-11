# DECISIONS.md

1. **Indexes.** `employees.emp_code` is unique for the business identifier. The compound unique index on `attendance_logs(emp_code, date)` enforces one record per employee-day. The date-descending/employee-ascending index serves the attendance listing sort, while department/code supports employee filtering. The joined-on/department index supports eligible headcount aggregation, and the date/status index supports filtered attendance reads. I considered a standalone status index, but rejected it because status is low-cardinality and the useful predicates also include dates.

2. **Punch-in race.** Both requests first validate the employee, then attempt the same insert. MongoDB's unique compound index serializes the result: one insert succeeds and returns 201; the other receives `DuplicateKeyError` and becomes 409. There is no check-then-insert assumption.

3. **Ties.** The leaderboard uses MongoDB `$rank`, so equal totals share a rank and the next rank is skipped. The rank filter is applied after ranking; every employee tied at the cutoff rank is returned.

4. **Headcount.** Department summary starts from eligible employees and uses a lookup into attendance logs with `preserveNullAndEmptyArrays`. An employee with no logs therefore still contributes one headcount row.

5. **100x data.** I would move analytics to pre-aggregated daily facts or a reporting read model, retaining the transactional logs as the source of truth. That would keep interactive requests from repeatedly scanning a much larger history.
