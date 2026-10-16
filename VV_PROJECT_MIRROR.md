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

## Independent construction treasury (additive v2)

The new `vv-project-snapshot-v2` stream uses the fixed, fingerprint-covered
`origin_namespace: "construction_cash"`. It has independent integer operation IDs,
correction roots, reversal IDs, cursor, explicit cutoff, durable state and audit.
It accepts only new Vita expenses from VV's independent construction treasury;
Shevchenkovo expenses and construction income do not appear in this stream.
V1 personal-ledger snapshots, identity, cutoff, records, tables and acknowledgement
format are unchanged. Do not renumber or copy personal operations into v2.

V2 uses the same configured `POST /v1/vv-expenses` URL, dedicated existing write
token, source instance/generation and exact Denis/Igor project mapping. No new
credential, listener, project or permission is introduced. Protocol version 2
must **not** be confused with a new source generation. Enable this requested
source explicitly on both applications:

- `VITA_VV_CONSTRUCTION_ENABLED=1`
- `VITA_VV_CONSTRUCTION_AFTER_ID=0` for a newly created, empty treasury. This is an
  independent construction-operation watermark, never the personal v1 cutoff.
  Review another cutoff explicitly if a treasury already contains records.

Without the opt-in, v2 HTTP requests return `construction_stream_disabled`, while
v1 continues normally. The v2 acknowledgement is `vv-project-ack-v2` with
`origin_namespace`, cursor, fingerprint and active count. The namespace must
match exactly; no fallback to v1 is allowed. Existing transport authentication,
size/time bounds, no-redirect handling and scope validation remain in force.

Storage is additive: `vv_construction_project_expenses`,
`vv_construction_mirror_state`, and `vv_construction_mirror_audit`, with equivalent
append-only/tombstone protections. BEFORE INSERT identity-collision guards also
block SQL REPLACE from overwriting records or audit rows, including hidden-rowid
aliases, even when SQLite recursive triggers are disabled. These additive guards
protect both mirror namespaces and do not rewrite stored history. Table names are fixed by local scope type,
never wire input. Overlapping operation IDs between v1 and v2 are valid.
Construction report IDs are `VV:construction_cash:<operation_id>`; existing v1
IDs remain `VV:<operation_id>`. Reports include each active version once, label
its construction-treasury provenance and remain cash-neutral. Native operations,
accounts, personal reports and the Vita-to-VV export never receive these rows.

Construction sources are `vlad`, `valera`, `common`, and `unknown`. `unknown` is
stored and reported unchanged; it must never be guessed or converted to Common.
V1 still rejects `unknown`. A source correction appends a reversal and replacement
under the original root. Original UAH input amounts remain UAH; the receiver
never recalculates exchange rates or makes a second cash debit. Receipts and
actor identifiers are not sent by either mirror stream.

### Reviewed rollout order

1. Back up both databases through their supported consistent backup procedures.
2. Publish/deploy the receiver update first, preserving all existing scope values
   and credentials. Confirm old v1 snapshots continue to be accepted.
3. Enable v2 on the receiver with the explicitly reviewed construction cutoff.
4. Publish/deploy VV's treasury and sender update, then enable v2 with the same
   cutoff. Do not change personal `VITA_VV_AFTER_ID` or source generation.
5. Check both stream acknowledgements and compare active project mirror counts;
   use synthetic tests, not a real test financial transaction. On any lineage
   mismatch, investigate rather than resetting state or replaying into v1.

Disabling v2 only pauses new delivery. Existing v2 report rows remain auditable,
and the immutable treasury retains the outbox for later exact retry. It does not
reset cursors, delete rows or move history back into personal cash.

### Added verification

`python -m unittest test_vv_construction_mirror -v` covers overlapping v1/v2 IDs,
independent watermarks, unknown source, source corrections, voids, retries and
restart, immutable history, rollback, protocol/scope rejection, opt-in HTTP
routing, project-only reports and no native-cash/export echo. Run both existing
full discovery suites as well. A separate cross-repository loopback integration
check exercises the actual VV sender against this receiver with disposable
SQLite databases; it creates no real operations or network configuration.
