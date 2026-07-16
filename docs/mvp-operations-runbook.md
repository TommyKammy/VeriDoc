# MVP Operations Runbook

This runbook covers the repository-local VeriDoc MVP/PoC API. Run every command
from the repository root. It is an operator guide for the MVP and is not an
approved production SOP or a commercial distribution procedure.

## Scope And Safety Boundary

The durable MVP state wired into the API has two directly linked parts:

- SQLite job metadata and job audit records at `VERIDOC_DB_PATH` (default:
  `var/veridoc/dev.sqlite3`);
- source, primary, debug JSON, and downloadable audit artifacts at
  `VERIDOC_ARTIFACT_STORE_ROOT` (default: `var/veridoc/artifacts`).

Configure both variables for a non-default state location and keep them on
storage available only to the local operator account:

```bash
export VERIDOC_DB_PATH="${VERIDOC_DB_PATH:-var/veridoc/dev.sqlite3}"
export VERIDOC_ARTIFACT_STORE_ROOT="${VERIDOC_ARTIFACT_STORE_ROOT:-var/veridoc/artifacts}"
```

Treat the database and artifact store as one state set. Audit rows in SQLite
refer to jobs and artifacts, while downloadable audit JSON is held in the
artifact store. Do not copy, restore, retain, or delete one side as if it were a
complete record. Stop the only writer before maintenance. Missing provenance,
an unverified backup, a broken audit chain, an unexpected second writer, or a
partially restored state is a blocking condition, not permission to infer that
the operation succeeded.

Review events accepted by `/api/review-events` remain process-local in this
MVP. They are not SQLite review records, are discarded when the API stops, and
are not included in the backup or restore procedure below. Record any review
evidence that must survive a restart in an approved external system before
stopping the API; never describe the SQLite backup as containing those events.

Template registrations created or updated through `POST /api/templates` are
also process-local in this MVP. Custom template versions and their change
history are discarded when the API stops and are not included in the backup or
restore procedure below. Export any custom template definition and required
change evidence to an approved external system before stopping the API.

Always stop the API before copying or restoring either part of the state set.

Use real locally issued credentials when authentication is enabled. Placeholder
values such as `<operator-token>` document command shape only and must never be
accepted as working credentials. Do not trust forwarded identity headers as a
substitute for `VERIDOC_LOCAL_AUTH_TOKENS`.

## Start

1. Confirm the checkout and entry point before opening the listener:

   ```bash
   git status --short --branch
   python3 services/api/poc_web.py --check
   ```

2. If this is a new state root, initialize the database explicitly. The API also
   initializes its managed schema, but this command exposes path or permission
   errors before startup:

   ```bash
   python3 -m services.api.persistence init-db --db-path "$VERIDOC_DB_PATH"
   ```

3. Start the API in the foreground so startup failures and audit-integrity
   failures remain visible:

   ```bash
   python3 services/api/poc_web.py
   ```

   The expected message is `VeriDoc PoC web API listening on
   http://127.0.0.1:8788`. The web UI is at that address. For token-protected
   operation, set `VERIDOC_LOCAL_AUTH_TOKENS` from a trusted local secret source
   before starting; do not put the value in the repository or shell history.

4. From another terminal, make a read-only health request:

   ```bash
   python3 - <<'PY'
   from urllib.request import urlopen

   with urlopen("http://127.0.0.1:8788/", timeout=5) as response:
       assert response.status == 200
       assert "text/html" in response.headers.get("content-type", "")
   print("MVP API health check passed")
   PY
   ```

The conversion smoke request in `README.md` may be used after this read-only
check. When authentication is enabled, use a real token with only the required
role and clear it from the browser tab after the check.

## Stop

1. Stop the foreground API with `Ctrl-C` and wait for the Python process to
   exit. Do not begin backup, restore, or deletion while a request or conversion
   worker is still running.
2. Confirm that the listener is down:

   ```bash
   python3 - <<'PY'
   from urllib.error import HTTPError, URLError
   from urllib.request import urlopen

   try:
       urlopen("http://127.0.0.1:8788/", timeout=2)
   except HTTPError:
       raise SystemExit("MVP API still accepts HTTP connections")
   except URLError as error:
       if not isinstance(error.reason, ConnectionRefusedError):
           raise SystemExit(f"cannot prove that the MVP API is stopped: {error.reason}")
       print("MVP API is stopped (connection refused)")
   else:
       raise SystemExit("MVP API still accepts connections")
   PY
   ```

If the process does not stop, identify and terminate the repository-owned API
process before continuing. Do not kill an unknown listener merely because it
uses the expected port.

## Backup

Create the backup only after completing **Stop**. A stopped API makes the SQLite
and artifact read set snapshot-consistent for this single-writer MVP. The
following command uses SQLite's backup API, copies the artifact tree, and writes
a SHA-256 manifest. Set `VERIDOC_BACKUP_DIR` to a new, access-controlled
directory; `<backup-root>` is a placeholder, not a literal path.

```bash
export VERIDOC_BACKUP_DIR="<backup-root>/veridoc-$(date -u +%Y%m%dT%H%M%SZ)"
python3 - <<'PY'
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory

from services.api.job_queue import JobQueue
from services.api.poc_web import JobAuditEventStore

source_db = Path(os.environ["VERIDOC_DB_PATH"])
source_artifacts = Path(os.environ["VERIDOC_ARTIFACT_STORE_ROOT"])
backup = Path(os.environ["VERIDOC_BACKUP_DIR"])
if backup.exists() or not source_db.is_file():
    raise SystemExit("backup target must be new and the source database must exist")
if source_artifacts.is_symlink():
    raise SystemExit("artifact store root must not be a symlink")
try:
    backup.resolve().relative_to(source_artifacts.resolve())
except ValueError:
    pass
else:
    raise SystemExit("backup target must be outside the artifact store")
backup.mkdir(parents=True, mode=0o700)

database_backup = backup / "database.sqlite3"
with sqlite3.connect(source_db) as source, sqlite3.connect(database_backup) as target:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise SystemExit(f"backup database integrity check failed: {result!r}")
    referenced_artifacts = target.execute(
        "SELECT DISTINCT content_sha256, size_bytes FROM job_queue_artifacts"
    ).fetchall()

for digest, size_bytes in referenced_artifacts:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise SystemExit("database contains an invalid artifact reference")
    artifact = source_artifacts / digest[:2] / f"{digest}.bin"
    if artifact.parent.is_symlink() or artifact.is_symlink() or not artifact.is_file():
        raise SystemExit(f"referenced artifact is missing or symlinked: {digest}")
    content = artifact.read_bytes()
    if len(content) != size_bytes or hashlib.sha256(content).hexdigest() != digest:
        raise SystemExit(f"referenced artifact failed verification: {digest}")

artifact_backup = backup / "artifacts"
if source_artifacts.is_dir():
    shutil.copytree(source_artifacts, artifact_backup)
else:
    artifact_backup.mkdir()

with TemporaryDirectory(prefix="veridoc-backup-verify-") as validation_dir:
    validation_root = Path(validation_dir)
    validation_db = validation_root / "database.sqlite3"
    validation_artifacts = validation_root / "artifacts"
    shutil.copy2(database_backup, validation_db)
    shutil.copytree(artifact_backup, validation_artifacts)
    JobQueue(database_path=validation_db, artifact_store_root=validation_artifacts)
    JobAuditEventStore(database_path=validation_db)

entries = []
for path in sorted(p for p in backup.rglob("*") if p.is_file()):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    entries.append(f"{digest}  {path.relative_to(backup).as_posix()}")
(backup / "SHA256SUMS").write_text("\n".join(entries) + "\n", encoding="utf-8")
print(f"backup created: {backup}")
PY
```

Retain the directory according to the approved local retention period, access
control, and storage capacity. Encrypt or otherwise protect the backup at rest
when the source documents require it. Keep at least one previously verified
backup until the new one has passed manifest verification and a restore drill.
Do not treat a partial copy as a valid backup, and do not restart the API if the
database backup, artifact copy, integrity check, or manifest write fails.

## Restore

Restore only during a stopped maintenance window. Restoration replaces the
entire state set; it does not merge backup rows or artifacts with newer state.

1. Complete **Stop**, set `VERIDOC_RESTORE_DIR` to one backup directory, and
   verify every manifest entry before changing current state:

   ```bash
   export VERIDOC_RESTORE_DIR="<backup-root>/<verified-backup>"
   python3 - <<'PY'
   import hashlib
   import os
   from pathlib import Path

   root = Path(os.environ["VERIDOC_RESTORE_DIR"])
   manifest = root / "SHA256SUMS"
   if not manifest.is_file():
       raise SystemExit("backup manifest is missing")
   database = root / "database.sqlite3"
   artifacts = root / "artifacts"
   if (
       database.is_symlink()
       or artifacts.is_symlink()
       or not database.is_file()
       or not artifacts.is_dir()
       or any(path.is_symlink() for path in artifacts.rglob("*"))
   ):
       raise SystemExit("backup state set is incomplete")

   expected_files = {"database.sqlite3"}
   expected_files.update(
       path.relative_to(root).as_posix()
       for path in artifacts.rglob("*")
       if path.is_file()
   )
   manifest_entries = {}
   for line in manifest.read_text(encoding="utf-8").splitlines():
       if "  " not in line:
           raise SystemExit("backup manifest contains an invalid entry")
       expected, relative = line.split("  ", 1)
       relative_path = Path(relative)
       if (
           len(expected) != 64
           or any(character not in "0123456789abcdef" for character in expected)
           or relative in manifest_entries
           or relative_path.is_absolute()
           or ".." in relative_path.parts
       ):
           raise SystemExit(f"backup manifest contains an invalid entry: {relative}")
       manifest_entries[relative] = expected

   if set(manifest_entries) != expected_files:
       raise SystemExit("backup manifest does not cover the complete state set")
   for relative, expected in manifest_entries.items():
       path = root / relative
       if (
           path.is_symlink()
           or not path.is_file()
           or hashlib.sha256(path.read_bytes()).hexdigest() != expected
       ):
           raise SystemExit(f"backup verification failed: {relative}")
   print("backup manifest verified")
   PY
   ```

2. Back up the current state using **Backup** as the rollback set. Stage the
   verified database and artifacts on the same filesystem as their destination.
   Treat the configured database plus `${VERIDOC_DB_PATH}-wal` and
   `${VERIDOC_DB_PATH}-shm` as one old SQLite state set: before installing the
   restored database, move the main database and every present sidecar into the
   rollback location, then confirm no old sidecar remains at the configured
   path. Move both staged components into their exact configured locations. Do
   not restart between the two moves. The MVP has no cross-filesystem
   transaction for this pair, so a failed move must keep reads blocked until
   both old components are restored or both new components are in place.
3. Run `PRAGMA integrity_check` against the restored database, verify the
   restored artifact tree against `SHA256SUMS`, and start the API. Startup and
   audit reads must remain fail closed if the stored audit hash chain is
   inconsistent. Exercise one known job's detail and artifact download before
   reopening the instance for use.
4. If any validation fails, stop, quarantine the incomplete restored state, and
   reinstate the rollback set. Confirm that no half-restored database or orphan
   artifact tree remains before the next attempt.

## Evaluation

Run the narrow documentation regression first, then the Phase 12 representative
MVP harness, the acceptance report, and repository hygiene from the repository
root:

```bash
python3 -m unittest tests.test_mvp_operations_runbook
python3 scripts/evaluate_dataset.py --mvp-harness
python3 scripts/evaluate_dataset.py --poc-acceptance-report
python3 scripts/ci/repo_hygiene.py
```

The MVP harness must report `acceptance_handoff.overall_status: pass` with no
failed or unknown representative cases, and the acceptance report must finish
with `overall_status: pass`. Treat a missing prerequisite, failed case, stale or
mixed report input, non-zero
`high_risk_false_auto_confirmed_count`, or absent audit evidence as a failed
gate. Do not replace a failed result with operator judgment. For broader
dataset commands and metric definitions, see `datasets/README.md`; for the MVP
transition boundary, see `docs/mvp-transition-decision.md`.

## Troubleshooting

| Symptom | Focused check | Action |
| --- | --- | --- |
| `--check` or import fails | Confirm the command is run from the repository root and read the first traceback | Restore the expected checkout/environment; do not edit import paths with workstation-specific literals. |
| Address already in use | Identify the process listening on TCP 8788 | Stop only the known repository-owned instance, or keep startup blocked until ownership is established. |
| Database cannot open or is locked | Print `VERIDOC_DB_PATH`; confirm its parent permissions and that no second writer is running | Stop the other known writer. Do not delete SQLite WAL/SHM files from a live database. |
| Artifact root is rejected | Print `VERIDOC_ARTIFACT_STORE_ROOT`; check that the configured root and existing parents are not symlinks | Choose a controlled real directory. Do not bypass the path validation. |
| PDF returns `server_dependency_unavailable` | Check the server traceback and optional local PDF extractor installation | Install the documented local dependency or use a supported non-PDF fixture; do not report the conversion as successful. |
| Authentication is rejected | Confirm a trusted `VERIDOC_LOCAL_AUTH_TOKENS` source and the minimum required role | Rotate or reissue the real token. Never promote sample, TODO, or placeholder credentials. |
| Audit integrity or restored-state validation fails | Keep the API stopped and verify the database plus artifact manifest from the same backup | Restore the last verified complete state set. Never suppress the integrity failure or splice records from different backups. |
| Artifact download is missing | Resolve the artifact only through the job-bound manifest entry | Restore the directly linked state set or record a failed job; do not infer a sibling artifact from its name or directory. |
| Evaluation reports failure | Read the failing criterion and its evidence reference | Correct the authoritative fixture/code boundary and rerun; do not edit the summary to say pass. |
| A request returns an error | Inspect the foreground traceback and then verify no orphan job, audit row, or artifact survived | Preserve evidence, repair the narrow failure, and retry only after durable state is clean. |

## Data Deletion

There is no approved selective audit-history deletion flow in this MVP.
Individual SQLite rows and content-addressed artifact files must not be removed
by hand because that can break authoritative linkage and the audit hash chain.
Apply the approved retention decision to the complete state set and its backups.

For an approved full local-state purge:

1. Record the deletion scope, approver, retention decision, and backup decision
   outside the state being deleted. Complete **Stop**. If retention requires a
   final backup, complete and verify **Backup** first.
2. Set both storage variables explicitly and require an exact confirmation. The
   guard below rejects the filesystem root, home directory, repository root,
   identical DB/artifact paths, and an artifact root that contains the DB:

   ```bash
   export VERIDOC_DELETE_CONFIRM="delete-mvp-state"
   python3 - <<'PY'
   import os
   from pathlib import Path
   import shutil

   if os.environ.get("VERIDOC_DELETE_CONFIRM") != "delete-mvp-state":
       raise SystemExit("deletion confirmation is missing")

   def resolve_non_symlink_target(configured: str, label: str) -> Path:
       candidate = Path(configured).expanduser()
       if not candidate.is_absolute():
           candidate = Path.cwd() / candidate
       for path in (candidate, *candidate.parents):
           if path.is_symlink():
               raise SystemExit(f"{label} target or parent is a symlink: {path}")
       return candidate.resolve()

   db = resolve_non_symlink_target(os.environ["VERIDOC_DB_PATH"], "database")
   artifacts = resolve_non_symlink_target(
       os.environ["VERIDOC_ARTIFACT_STORE_ROOT"], "artifact"
   )
   forbidden = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
   if db in forbidden or artifacts in forbidden or db == artifacts:
       raise SystemExit("unsafe deletion target")
   if artifacts in db.parents:
       raise SystemExit("database must not be inside the artifact root")

   for suffix in ("", "-wal", "-shm"):
       Path(str(db) + suffix).unlink(missing_ok=True)
   if artifacts.exists():
       shutil.rmtree(artifacts)

   survivors = [Path(str(db) + suffix) for suffix in ("", "-wal", "-shm")]
   if any(path.exists() for path in survivors) or artifacts.exists():
       raise SystemExit("state deletion is incomplete")
   print("MVP database and artifact state deleted")
   PY
   ```

3. After the command, verify that no database, WAL, SHM, or artifact data remains. Remove or expire
   each backup separately under the same recorded retention decision; deletion
   of live state does not imply deletion of backups.
4. Run `init-db` and start only when a deliberate empty instance is required.

The `python3 -m services.api.persistence reset-db` command resets managed SQLite
tables but does not remove the artifact store or retained backups. Therefore, do
not run reset-db by itself as a full deletion procedure. If any deletion step
fails, keep the API stopped, report the exact surviving paths, and finish or
roll back the approved operation instead of declaring the data deleted.
