# MongoDB for Flow Steward

Connect MongoDB to Flow Steward for document lookup, reusable JSON Dataset queries and workflow record changes. Workflow reads support find, find-one, projections, sorting and read-only aggregation. Writes insert one document or a batch, apply update operators with optional upsert, and delete matching documents. Multi-document update and delete actions require explicit acknowledgement, while mutation filters must be nonempty.

Read-only Datasets add authorized collection discovery, sampled schema inspection, parameterized find queries, a smaller aggregation subset, previews and output-schema inference. Dataset resource and field selections apply to queries and returned fields. Workflow document results preserve BSON types using canonical Extended JSON; Dataset results convert supported values into JSON while retaining large integers and Decimal128 values as strings.

Configure a database with username/password and a direct or SRV endpoint. Reads return at most 100 documents, but multi-document updates and deletes can affect every match. Aggregation result limits do not cap all server computation. Transactions, change streams, cross-collection joins and executable JavaScript are outside this connector. Sampled schemas describe observed documents rather than enforce collection structure. Atlas vector search and embedding generation are not provided.

## Runtime and operation requirements

An independent MongoDB extension with document reads and writes and bounded read-only aggregation. No database triggers. Install only this connector; it does not include SQL, Redis, or other database drivers.

## Install

Choose the ZIP matching the **Linux host** architecture, not the machine running your browser:

- `flowsteward.mongodb-1.1.5-linux-amd64.zip`
- `flowsteward.mongodb-1.1.5-linux-arm64.zip`

Each ZIP contains one MongoDB-only Google MCP Toolbox executable, the host-v2 adapter, manifests, and source/build provenance. Verify `SHA256SUMS` before importing the archive. Flow Steward platform 1.5.0 or newer and public extension SDK 0.4.0 are required.

## Connection

Create a project **MongoDB** connection with `host` and `database`. Use the server hostname alone, with no URI, credentials or path. The connection fixes the database; each workflow action selects a `collection`. Data is stored directly in your collection without an added tenant prefix. Database credentials and MongoDB roles control access, so use a user restricted to the intended database.

| Setting | Default | Meaning |
| --- | --- | --- |
| `port` | 27017 | Standard connection port; ignored SRV ports cannot be supplied |
| `srv` | false | Enable for an Atlas/SRV hostname, such as `cluster.example.mongodb.net` |
| `tls` | true | Verify the server certificate and hostname using system trust roots |
| `auth_source` | admin | Authentication database |
| `request_timeout_seconds` | 15 | Shared operation deadline, from 1 to 30 seconds |

Store `username` and `password` in the secret fields. Credentials are never part of action inputs or returned diagnostics. Standalone, replica discovery and SRV connection paths are implemented. SRV requires TLS and the default port. Public endpoints always require TLS. Private endpoints also require the host administrator's `FS_ALLOW_PRIVATE_REMOTE_URLS` opt-in. Every discovered node is resolved and checked by the native dialer, then connected using its validated IP while retaining the TLS hostname. Custom certificate files, insecure TLS, arbitrary URI options, tunnels, and executable overrides are not exposed.

## Actions

Every action requires `connection_ref` and `collection`.

| Action | Inputs | Result |
| --- | --- | --- |
| `find` | Optional `filter`, `projection`, `sort`, `limit` | `documents`, `count`, `found` |
| `find_one` | Optional `filter`, `projection` | `document` or null, `found` |
| `aggregate` | `pipeline`, optional `limit` | `documents`, `count`, `found` |
| `insert_one` | `document` | `inserted_id` |
| `insert_many` | `documents` | `inserted_ids` |
| `update_one` | Nonempty `filter`, operator `update`, optional `upsert` | `matched_count`, `modified_count`, `upserted_count`, `upserted_id` |
| `update_many` | Same as update_one, plus `allow_many: true` | Same counters |
| `delete_one` | Nonempty `filter` | `deleted_count` |
| `delete_many` | Nonempty `filter`, `allow_many: true` | `deleted_count` |

Every result also includes `collection`; writes include `applied`. A matched update that changes nothing returns `applied: false`. Zero-match reads and deletes succeed normally. Filters on mutations must be nonempty; this does not prove selectivity, so review them before use. Many-document writes can affect all matching documents and are not limited to 100. Insert batches contain 1–100 documents. This connector offers operator updates and upserts; it does not expose replacement updates, transactions or change streams.

Reads return at most 100 documents (default 20); `find_one` returns at most one. Sorting preserves field order. Aggregation accepts at most 16 stages and appends the configured result limit. Allowed stages are `$match`, `$project`, `$group`, `$sort`, `$limit`, `$skip`, `$unwind`, `$addFields`, `$set`, `$unset`, `$count`, `$replaceRoot`, `$replaceWith`. Write stages, cross-collection joins and JavaScript operators/BSON code are rejected. A result limit bounds returned documents, not the server work needed by a sort or group. Requests are capped at 256 KiB; inline results at 480 KiB; native output at 1 MiB. Larger results fail with an explicit error; use narrower filters/projections.

## BSON types and examples

Use JSON objects in JSON fields. Results use **canonical MongoDB Extended JSON**, preserving BSON types and 64-bit integer precision. Numbers in returned documents are tagged, for example `{"$numberInt":"3"}`. Write counters are ordinary JSON integers. To query an ObjectId returned by an insert, pass the returned `inserted_id` unchanged:

```json
{"connection_ref":"my-mongodb","collection":"documents","filter":{"_id":{"$oid":"0123456789abcdef01234567"}}}
```

An upsert example:

```json
{"connection_ref":"my-mongodb","collection":"documents","filter":{"slug":"intro"},"update":{"$set":{"title":"Introduction","revision":{"$numberLong":"9007199254740993"}}},"upsert":true}
```

Bare integers outside the interoperable JSON safe range are rejected; use `$numberLong` or the appropriate Extended JSON representation. String values containing `${ENV}`, `{{templates}}`, or `$operators` remain literal data.

## Writes and failure handling

Live writes require the host's durable `external_effect_id` claim. Test mode validates input and suppresses writes before starting Toolbox. Automatic MongoDB read/write retries are disabled and writes request majority acknowledgement. Neither the claim nor an insert batch makes a write transaction atomic: a timeout, broken connection or batch error may follow partial effects. Such failures report `retryable: false` and `external_effect_status: timeout_unknown`. Reconcile existing documents before deciding to retry. Stable explicit `_id` values help detect duplicate inserts. Do not automatically retry `$inc`, pushes, or partially completed batches.

## Build and verify

```sh
./scripts/build_toolbox.sh
python3 scripts/package_extension.py --runtime-target linux-amd64
python3 scripts/package_extension.py --runtime-target linux-arm64
```

The build pins upstream Toolbox v1.16.0 commit `06ea49d9a487a72c22419b5a8d5c22c11f0d7485` and a Go container by digest. The included source patch corrects MongoDB sorting and result handling, adds literal payloads and the dial policy, and disables automatic retries. Binary SHA-256, source, patch and build image are recorded in `toolbox-provenance.json`. Binaries are built locally and omitted from Git source history. Archives have deterministic ordering, timestamps and permissions, contain one architecture, and stay within the installer's 50 MiB compressed/150 MiB unpacked limits.

From a Flow Steward checkout, run the supported container gate against this standalone checkout:

```sh
./core/entrypoints/cli/scripts/lint.sh --checks extension-static,extension-tests --extension-root /absolute/path/to/ext-data-mongodb
```

Unit tests cover contracts, input limits, literal data, write claims, failure semantics, transport deadlines and archive boundaries. Live tests opt in through ignored `tests/live-mongodb.local.json` with `host`, `port`, `database`, `auth_source`, `username`, `password`, `tls`. Use a disposable test database. They exercise authenticated document CRUD, Extended JSON, projection, sorting, upsert and aggregation. Without a fixture they are explicitly skipped. Local integration uses MongoDB 8.0.17; Atlas/SRV, replica failover and a real public TLS deployment require separate environment acceptance.

Apache-2.0. See `LICENSE` and `NOTICE`. This is an independent Flow Steward integration, unaffiliated with MongoDB, Inc.

## Native Dataset queries (1.1.0)

Dataset queries use JSON, with exact collection names and explicit top-level output fields:

```json
{"resource_id":"orders","operation":"find","arguments":{"filter":{"status":{"$eq":{"$param":"status"}}},"projection":{"status":1,"total":1},"sort":{"total":-1}}}
```

Declare parameters with JSON Schema scalar, object or array types. Single-member
`{"$param":"name"}` markers are allowed only inside arguments and preserve types.
`find` supports filter, inclusion projection, sort and limit. Filters support
field equality, `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`, `$nin`, `$exists`
and `$and`/`$or`/`$nor`. Field names are conservative ASCII top-level names; nested
field selection, computed projections, arbitrary expressions and scripts are unsupported.
`aggregate` supports only `$match`, `$sort`, `$skip`, `$limit` and inclusion
`$project`, and requires an explicit projection. Collections and fields are checked
against the Dataset scope before dispatch and output fields are checked again.
Implicit `_id` is suppressed unless explicitly selected. Writes, cross-collection
stages and JavaScript BSON are rejected, including inside parameter objects.
These Dataset operations provide document reads, with no Atlas vector or RAG claim.

Collection discovery lists only names authorized by MongoDB in the configured
database, with at most 32 collections and a 33rd sentinel for truncation. Schema
refresh samples at most five documents per collection and reports at most 100
top-level fields in total. Types are observations from samples, not schema guarantees;
empty collections have no invented columns. Mixed fields report `value_type: json`,
all observed sample types and a JSON Schema union; empty query inference uses `{}`
for unknown field types. Values retain their native JSON types. Scoped refresh projects only allowed
fields and does not discover other collections. Results identify sampling/truncation.

Queries and bound arguments are limited to 64 KiB, depth 20 and 10,000 nodes.
Preview and runtime row caps are separate, each 1–100; response budgets are at most
400 KiB. A single deadline of at most 30 seconds includes discovery and sampling;
there are no retries. Canonical Extended JSON becomes JSON numeric, boolean,
ObjectId string and date (ISO string or epoch milliseconds) values while nested
objects and arrays are preserved. Integers beyond JavaScript's safe range and
Decimal128 values remain strings to preserve precision.
Workflow CRUD remains project scoped and writes still require a durable effect
claim; test mode suppresses writes. Dataset admin read actions accept host admin
contexts with null account and project IDs. No triggers are provided.

Discovery scans at most 33 raw authorized collection names. Names outside the
conservative selectable-name rules (for example `system.views` or names above
120 UTF-8 bytes) are skipped without sampling; metadata reports a skipped count
and truncation. Caller byte limits apply to the complete assembled metadata.
Validation with unresolved typed parameters reports `sample_parameters_required`
and `required_sample_params`: static structure/types are checked, while actual
values are required for complete scope/range validation and every execution.
