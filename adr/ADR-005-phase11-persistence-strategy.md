# ADR-005: Phase11 Persistence Strategy

## Context

Phase11 introduces durable job history and audit workflow foundations for the
local PoC API. The current API keeps job queue state in memory through
`services/api/job_queue.py`, keeps review audit events in memory inside
`services/api/poc_web.py`, and already has an encrypted temporary artifact
boundary in `services/api/temp_file_store.py`.

This ADR records the persistence strategy only. Production DB operation, schema
implementation, UI behavior, and identity-provider integration stay outside
this issue.

The persistence layer must fail closed when provenance, authorization context,
record scope, or artifact linkage is missing. It must also preserve the
authoritative lifecycle record over derived status surfaces: detail responses,
counters, badges, and timelines are projections and must be recomputed from the
stored authoritative records.

## Candidate Comparison

| Candidate | Local MVP fit | Operations burden | Audit/read consistency | Migration posture | Decision |
| --- | --- | --- | --- | --- | --- |
| SQLite metadata DB | Strong. It runs with the Python stdlib, needs no local service, and fits a single-node developer PoC. | Low. Local reset, backup, and test setup can use repository-relative files. | Good for the MVP when each logical change is committed atomically and read rollups use one committed snapshot. | Good if callers depend on a repository interface and SQL stays portable. | Selected as the Phase11 MVP default. |
| PostgreSQL metadata DB | Strong future fit for multi-user and production-like operation. | Higher. Requires a running service, credentials, migrations, backup policy, and CI/service wiring. | Strong when production operation needs concurrent clients, managed backup, and access controls. | Best long-term target, but premature for the first local history/audit slice. | Not selected as the MVP default; preserve a PostgreSQL-compatible repository boundary. |
| JSON or file-only metadata store | Simple for append-only prototypes. | Low initially but brittle as lifecycle queries, retries, and cleanup grow. | Weak for snapshot-consistent detail rollups and all-or-nothing multi-record writes. | Poor. It would likely need a later rewrite before P11 audit workflows stabilize. | Not selected for authoritative metadata. |
| Artifact file store | Strong for binary upload/result bytes that should not be embedded in DB rows. | Moderate. Needs root configuration, retention, cleanup, and integrity checks. | Good when metadata rows explicitly bind to artifact identifiers and content hashes. | Compatible with either SQLite or PostgreSQL metadata. | Selected for binary artifact bytes. |

## Decision

Selected: SQLite-backed metadata store plus artifact file store.

SQLite is the Phase11 MVP default for authoritative metadata because it gives
the local PoC durable job and audit records without adding a database service to
developer setup. PostgreSQL is not the Phase11 MVP default, but all persistence
callers must go through a PostgreSQL-compatible repository boundary so P11 or a
later production-readiness phase can replace the backend without changing
`services/api/poc_web.py` handlers.

SQLite remains responsible for authoritative metadata:

- job lifecycle state, idempotency keys, attempts, timestamps, and terminal
  markers
- append-only job event and audit event records
- source document metadata and explicit artifact bindings
- review decision records and actor/scope references captured after local auth
  validation
- schema migration version records

File store remains responsible for binary artifact bytes:

- uploaded source files
- generated DOCX/XLSX/JSON result bytes
- temporary conversion intermediates that need durable cleanup accounting

The metadata DB stores only artifact identifiers, category, content hash,
original filename, MIME type or format, retention state, and lifecycle linkage.
It must not infer tenant, repository, job, source document, or review linkage
from filenames, path shape, or nearby metadata. Every artifact row must bind to
an authoritative source/job record or be rejected.

## Local Development And Test Operation

The default local development paths are repository-relative and may be
overridden with environment variables:

| Purpose | Default | Override |
| --- | --- | --- |
| SQLite metadata DB | `var/veridoc/dev.sqlite3` | `VERIDOC_DB_PATH` |
| Artifact file store root | `var/veridoc/artifacts` | `VERIDOC_ARTIFACT_STORE_ROOT` |
| Test SQLite DB | pytest `tmp_path` fixture | test fixture setup |
| Test artifact store | pytest `tmp_path` fixture | test fixture setup |

Initialization is owned by the persistence module command:

```bash
python3 -m services.api.persistence init-db
```

The command creates the parent directory for `VERIDOC_DB_PATH`, applies all
available migrations, and exits non-zero if the configured path is malformed,
escapes the intended local root, or cannot be opened. Artifact roots are created
by the artifact store when configured and must use the same fail-closed path
validation style as the existing `TemporaryFileStore` boundary.

Local cleanup is explicit. Deleting `var/veridoc/dev.sqlite3` resets metadata;
deleting `var/veridoc/artifacts` resets local artifact bytes. Tests must not
depend on those paths and should create isolated temporary DB/file-store roots.

## Entity List And Responsibility Boundary

P11-02 should introduce the minimal schema behind a repository interface, not
direct SQL calls from request handlers.

| Entity | Responsibility |
| --- | --- |
| `jobs` | Authoritative conversion job lifecycle, idempotency key binding, request mode, source document binding, attempt count, terminal status, and created/updated timestamps. |
| `job_events` | Append-only job lifecycle events such as queued, running, retry requested, succeeded, and failed. |
| `source_documents` | Uploaded source metadata, source type, original filename, hash, upload artifact binding, and validation status. |
| `generated_artifacts` | Result artifact metadata, content hash, format, storage category, retention state, and explicit source/job binding. |
| `review_decisions` | Review edit/approval decisions anchored to a job, source document, generated artifact, actor, role, and decision target. |
| `audit_events` | Append-only audit chain records for review and job actions, including integrity linkage and authoritative scope references. |
| `schema_migrations` | Applied migration identifiers and timestamps. |

The initial module boundary is:

- `services/api/persistence.py`: connection setup, migrations, transaction
  helper, repository protocol, and SQLite implementation.
- `services/api/artifact_store.py`: durable artifact file-store adapter that can
  reuse or wrap `TemporaryFileStore` encryption and cleanup behavior where it
  fits longer-lived artifacts.
- `services/api/poc_web.py`: request/response orchestration only; it should call
  repositories and artifact-store interfaces rather than issue SQL or path
  operations directly.
- `services/api/job_queue.py`: may keep in-memory worker coordination for the
  local server, but authoritative job lifecycle state should move to the
  repository as P11 issues implement durable history.

`TemporaryFileStore` remains the current encrypted temporary artifact boundary.
The durable artifact store may share its validation, encryption, and cleanup
rules, but must not rely on temporary retention semantics for permanent audit
history.

## Migration And Backup Posture

Migrations are explicit Python-owned steps applied by
`python3 -m services.api.persistence init-db`. Each migration must be
idempotent, recorded in `schema_migrations`, and covered by focused repository
tests before handlers rely on the new tables.

One logical change that writes multiple records must persist atomically. Failed,
forbidden, or rejected paths must leave no orphan job, source document,
generated artifact, review decision, audit event, or half-written artifact
binding. Tests for failed writes should assert both the returned error and the
clean durable state afterward.

Read surfaces that combine multiple entities, including detail responses,
readiness checks, backup/export flows, and audit rollups, must read from a
single committed snapshot or explicitly reject a mixed-snapshot result. Do not
hold a transaction open across network hops, queued worker waits, LLM adapter
dispatch, or other remote waits. Stage the remote boundary, commit or roll
back, then continue in a new transaction if more durable state is needed.

SQLite backup for the local MVP is file-level backup after the process is idle
or through SQLite's backup API. Artifact backup must include both the SQLite DB
and `VERIDOC_ARTIFACT_STORE_ROOT` from the same committed point in time; if
that cannot be guaranteed, the backup command must reject the attempt instead
of producing stitched partial state. PostgreSQL backup operation remains a
future production-readiness concern.

## Follow-Up Implementation Boundary

P11-02 should implement the minimal SQLite schema and repository tests for
`jobs`, `job_events`, `source_documents`, `generated_artifacts`,
`review_decisions`, `audit_events`, and `schema_migrations`.

P11-03 and later issues may wire `services/api/poc_web.py` handlers to the
repository, but each handler change should keep the enforcement boundary close
to the authoritative write. Idempotency, authorization, source document
binding, artifact linkage, and audit append behavior should fail closed at the
repository boundary when required signals are absent.

PostgreSQL support should be introduced only after the repository contract is
stable enough to run the same behavioral tests against a PostgreSQL-backed
implementation.

## Verification

- `python3 -m pytest -q tests/test_phase11_persistence_decision.py`
- `python3 scripts/ci/repo_hygiene.py`
- `python3 -m pytest -q`
