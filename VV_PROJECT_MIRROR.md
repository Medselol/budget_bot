# VV → Vita project mirror

This optional receiver is disabled by default and is separate from the existing
read-only exporter. It creates no native Vita operations, users, accounts, or cash
postings. Corrections and cancellations must originate in VV. Do not configure
or enable it until both sides' identities, payer/project mappings, cutoff and
independent write credential have been verified and approved.

## Configuration

The source bot reads these operator-provided variables:

- `VITA_VV_WRITE_ENABLED=1` enables the receiver; absent/other values leave it off.
- `VITA_VV_WRITE_PORT` defaults to `8788`, separate from the read exporter.
- `VITA_VV_WRITE_TOKEN` is a dedicated 32–256 character URL-safe credential. It
  must differ from `VITA_SYNC_TOKEN`; the read token never authorizes a write.
- `VITA_VV_SOURCE_INSTANCE` and positive `VITA_VV_SOURCE_GENERATION` identify VV.
- `VITA_VV_PROJECT_MAPPING` is exact JSON with distinct positive existing project
  IDs for `denis` and `igor`; no other payers are accepted. Verify the project
  owners separately before configuration.
- `VITA_VV_AFTER_ID` is a required explicit VV operation watermark. Only expense
  roots after it qualify. No implicit zero or automatic historical backfill.

Bind only to private trusted networking. This stdlib listener does not terminate
TLS. Do not publish its port as a public endpoint. Startup validates the source
file and projects; it never creates a missing ledger database. No credential is
created or provisioned by this module.

## Protocol

`POST /v1/vv-expenses` requires a single bearer authorization header, JSON content
type and bounded content length. It accepts a full `vv-project-snapshot-v1`
snapshot, not a partial update. Scope contains the exact instance, generation,
project mapping and cutoff. Records are in increasing operation ID order and
retain immutable original amount/currency, payer, project metadata, date/comment,
creation time and root ID. A reversal adds its immutable reversal operation ID
and sets `deleted=true`. All prior records, including tombstones, remain present.

The SHA-256 fingerprint is over UTF-8 sorted-key compact JSON excluding only
`fingerprint` and `captured_at`. Cursor, records and scope are covered. Capture
time is UTC and must not be over five minutes ahead. The acknowledgement is
`vv-project-ack-v1` with the accepted cursor, fingerprint and active record count.
An exact cursor/fingerprint retry does not mutate data or duplicate the audit.

Limits are 10,000 records, 16 MiB, and a 10-second whole-request deadline. Amounts
are positive integer minor units up to VV's 1,000,000,000,000 limit. IDs must fit
SQLite signed 64-bit integer representation. Snapshot validation and apply are atomic: malformed,
stale, out-of-scope, missing-history, changed-content, broken-lineage, undelete,
wrong-reversal or conflicting requests leave all accounting data unchanged.

## Storage, reports and recovery

`vv_project_expenses`, `vv_mirror_state`, and `vv_mirror_audit` are isolated from
native operations and source audit. Full-snapshot reconciliation is serialized
with `BEGIN IMMEDIATE`. The successful cursor, pinned scope and append-only audit
survive restart. Foreign record payloads and tombstones cannot be rewritten or
deleted by normal SQL updates.

Aggregate/project text, CSV, PDF, budget and weekly views include active mirrored
expenses with a VV label. Personal/participant reports, cash balances, local
operation edit lists, receipt links and native source snapshots exclude them.
Aggregate money-change totals explicitly exclude mirror-only expenses. Original
UAH inputs are displayed as UAH; no second conversion or cash debit occurs.

Scope or generation changes after the first accepted snapshot fail closed. A
restored/replaced ledger, wrong mapping, or missing history requires explicit
operator investigation and a reviewed reconciliation plan. There is no API reset,
automatic rebaseline, cursor rewind or silent deletion. Keep normal database
backups, including mirror tables. Turning the receiver off stops new writes;
previously accepted project mirror records remain visible and auditable.

## Synthetic verification

Run `python -m unittest discover -v` and
`python -m unittest discover -s tests -v`. The receiver suite uses temporary local
databases and loopback HTTP only. It covers creates, retries/restarts, per-payer
mapping, original UAH, correction/cancellation chains, rollback, concurrent
cursors, malformed input, authentication, deadlines, reports and no export echo.
