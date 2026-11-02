# GET /api/jobs/{job_id} tests

Status: implemented. Test-only; no production code changed. Closes candidate 9 of the full-codebase
architecture review ("`GET /api/jobs/{id}` has no tests").

## Problem

`api/routers/jobs.py` is the one JSON endpoint in an app that otherwise returns HTML fragments. Nothing in
the UI calls it (the pages poll `reels.py`'s HTML fragments), so a rename of a response field, a leaked
column, a lost 404 or a read that changes the row would have shipped without any test failing, and the only
consumers are external clients nobody here controls.

## Change

`tests/test_jobs_router.py`, 21 tests, through a real `TestClient` with an in-memory SQLite database (the
same fixture shape as `tests/test_cut_media.py`):

- the body is exactly `{id, status, progress, error}` with the right values (`id` and `progress` as JSON integers, not floats) and `application/json`, with redirects not followed (a route moved to a trailing-slash path would answer the old URL with a 307 that the client silently follows);
- each of the four `JobStatus` values is serialized as its plain string;
- `progress` 0 is returned, not dropped; a failed job's error text (including non-ASCII and newlines, at the 2000-character maximum `job_task` stores) is returned verbatim, not cut;
- no internal column is exposed: `meta` (scores, note ids, a marker string), the fencing counter and the
  reaper counter are not in the body;
- the requested job is the one returned, with three jobs of different types (enrich, render, publish), states
  and progress (including 100) and job ids that differ from their reels' ids; the running one carries a
  "transient failure, retry 1" error, because a retried job keeps its error through the next claim; a reel id
  is not mistaken for a job id;
- an unknown id is a 404 with `{"detail": "Job not found"}`; 0 and negative ids are also 404;
- a non-integer id (`abc`, `1.5`, `1e3`) is a 422;
- reading a job changes nothing (status, progress, error, `updated_at`, `claim_token` before and after two
  reads);
- POST, PUT, PATCH and DELETE are 405.

## Verification

Twenty mutants over `api/routers/jobs.py` and `api/schemas.py`, restored after each: `meta` or `claim_token`
added to the response schema, the `error` or `progress` field dropped, lookup by `reel_id` instead of the job
id, the 404 replaced by a default body, the 404 text changed, the id typed `str`, `response_model_exclude_none`
and `response_model_exclude_defaults` added, a read that increments `progress` and commits, POST also routed, and (added after review) the route moved to a
trailing-slash path, the error cut to 200 characters, `progress` typed `float` and newlines in the error
replaced by spaces, the error shown only for a failed job, `progress` clamped at 99, and a job type (enrich,
render or publish) answered with a 404.
Nineteen are killed. The survivor (a default value on `JobResponse.status`) is equivalent: the attribute is
always present on a loaded row, so the default is never used.

## Left alone

- `Job.progress` is nullable in the ORM and migration (`server_default="0"`), but `JobResponse.progress` is a
  required `int`, so a row with a NULL progress would make the endpoint answer 500. No code path writes
  `None` (`heartbeat()` and `job_task` assign ints), so it is unreachable today and not pinned.
- `JobResponse.status` is typed `str`, not the enum, so a new `JobStatus` value is served without a schema
  change. That is why the status test is parametrized over `list(models.JobStatus)` and not a literal list.
- `docs/api.md` says `error` is "truncated to 2000 chars, sanitized". That is true of what `job_task` writes
  (`_error_text`, `message[:2000]`), not of this endpoint, which returns the column as stored; the test
  therefore does not assert a length.
