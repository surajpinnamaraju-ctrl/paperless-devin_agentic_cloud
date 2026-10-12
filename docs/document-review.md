# Permission-aware document review (PLX-001)

This implementation extends Paperless itself. AI-generated implementation and
documentation by Devin, with separate backend and Angular implementation agents.

## Setup from a fresh checkout

Use the existing locked Python/Angular dependencies; no new packages are required.
Python 3.12 and Node 24.15 or newer are suitable (Angular 22 rejects Node 24.9).
Validation used Python 3.12.13, Node 24.15.0, and pnpm 11.15.1.
Follow the normal bare-metal/development prerequisites, including Ghostscript,
Tesseract, Poppler, qpdf, libmagic, and Redis for workers.

```bash
python -m pip install --user uv
uv python install 3.12.13
uv sync --group dev --link-mode copy --python "$(uv python find --managed-python 3.12.13)"
uv run prek install
mkdir -p consume media
# Set PAPERLESS_SECRET_KEY to a local development value, not a production secret.
uv run python src/manage.py check
uv run python src/manage.py migrate
uv run python src/manage.py makemigrations --check --dry-run
cd src-ui
pnpm install --frozen-lockfile
pnpm ng lint
pnpm ng build --configuration production,en-US
```

For development, start Django (`uv run manage.py runserver` from `src/`), the
normal Angular development server (`pnpm start` from `src-ui/`), and Redis/Celery
plus the document consumer when testing file processing. Review mutations are
synchronous database operations and do not need a separate review worker.
No external AI services or production credentials are needed. The managed Python
build supports SQLite extension loading used by the existing synthetic AI tests;
copy-mode dependency installation avoids NLTK's rejection of hard-linked resources.

## API and permissions

`GET /api/documents/{id}/review/` returns persistent state, revision, assigned
reviewer, submitter, and ordered history. Absent review rows mean `unreviewed`,
revision zero; reading an existing collection does not create one row per document.
Additional `allowed_actions` and `eligible_reviewers` fields drive the Angular UI.

POST the same URL with `action`, a nonnegative integer `expected_revision`, and a
UUID string `request_id`. `submit` also requires `reviewer`; `reject` requires a
trimmed 1–2000 character comment. Other payload fields are rejected. Submit moves
unreviewed to pending; only the assigned reviewer can approve/reject. Reopen moves
approved/rejected to unreviewed and retains the history. Submitters cannot decide
their own submission, including superusers.

Global model permissions and the existing owner/group/object access rules both
apply. Review does not grant document read/download access. Current permissions
are checked before idempotency replay. A retry with the same document/actor/key
and identical payload returns the original successful response; different payloads
conflict. Distinct keys competing at one revision yield one mutation.

`GET /api/review_queue/` accepts optional `state`, `reviewer`, `page`, and
`page_size` (1–100). Only persisted reviews with positive revisions are included,
ordered by document ID, with visibility filtering before count and pagination.

## Content, versions, and lifecycle

Content/file checksums, archive changes, page/MIME metadata, version creation,
and version merging share the document lock with review decisions. A real change
atomically invalidates pending/approved review, clears assignments, increments
the revision, and records one `invalidate` event. Title-only edits and no-op
content writes do not invalidate. Authenticated edit/version/merge paths propagate
the actor; background processing uses null when no actor is available.

Trash/restore preserve state and history; mutations while trashed are forbidden.
Permanent model/queryset deletion removes review, events, and stored request
responses. Migrations add review storage without rewriting document rows or files.

## Developer validation

```bash
cd src
../.venv/bin/pytest documents/tests/test_review*.py -q
../.venv/bin/pytest documents/tests/test_api_document_versions.py \
  documents/tests/test_merge_documents_as_versions.py documents/tests/test_consumer.py -q
cd ../src-ui
pnpm exec jest --runInBand --coverage=false
```

Run the existing regression suite rather than disabling tests. The final handoff
report records the actual commands, outcomes, failures, and untested areas.

## Failure diagnostics

- Django needs a local `PAPERLESS_SECRET_KEY` and existing consume/media directories.
- Angular CLI version errors: use the minimum Node release required by the locked
  CLI (Node 24.15 worked here).
- Missing OCR/PDF executables cause document-consumer regression failures; install
  the normal system prerequisites rather than skipping processing tests.
- A stale revision returns HTTP 409; the UI refreshes review state and asks the
  user to retry. It never silently approves a newer document revision.
- Existing review migrations must be applied to the same persistent database used
  by Django and workers before starting the application.
