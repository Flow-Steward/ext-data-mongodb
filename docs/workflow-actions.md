# MongoDB workflow actions

Configure a MongoDB project connection with its host, port or SRV setting, TLS, target database and authentication source. Store username/password in connection secrets; do not paste a connection URI into Host. Grant the user the collection read/write permissions required by the chosen action. Select that connection and collection in each action. Reads return canonical Extended JSON, so BSON values such as ObjectId and dates retain their types. Use a Dataset for reusable read-only queries.

Action names below match the workflow builder. IDs in parentheses remain compatible with saved workflows. Examples show field values; bind values from earlier steps as needed.

## Choose and configure an action

### Find matching documents (`find`)

Read up to 100 MongoDB documents using a filter, optional projection and ordered sort. Results use canonical Extended JSON.

```json
{
  "collection": "orders",
  "filter": {
    "status": "pending"
  },
  "projection": {
    "status": 1,
    "total": 1,
    "_id": 0
  },
  "sort": {
    "total": -1,
    "_id": 1
  }
}
```

### Find one matching document (`find_one`)

Read one matching MongoDB document with an optional field projection. A missing match returns null and found=false.

```json
{
  "collection": "orders",
  "filter": {
    "status": "pending"
  },
  "projection": {
    "status": 1,
    "total": 1,
    "_id": 0
  }
}
```

### Run a read-only aggregation pipeline (`aggregate`)

Summarize or transform documents with up to 16 supported aggregation stages and at most 100 results. Cross-collection, JavaScript and write stages are rejected.

```json
{
  "collection": "orders",
  "pipeline": [
    {
      "$match": {
        "status": "approved"
      }
    },
    {
      "$group": {
        "_id": "$status",
        "count": {
          "$sum": 1
        }
      }
    }
  ]
}
```

### Insert one new document (`insert_one`)

Insert a MongoDB document. Supply a stable _id to detect duplicate submissions; this action does not update an existing document.

```json
{
  "collection": "orders",
  "document": {
    "_id": "order_1042",
    "status": "pending"
  }
}
```

### Insert a batch of new documents (`insert_many`)

Insert 1–100 MongoDB documents. A failure can leave a partially inserted batch; automatic retries are disabled.

```json
{
  "collection": "orders",
  "documents": [
    {
      "_id": "order_1042",
      "status": "pending"
    }
  ]
}
```

### Update one matching document (`update_one`)

Apply MongoDB update operators to one document selected by a nonempty filter. Enable upsert to create a document when no match exists.

```json
{
  "collection": "orders",
  "filter": {
    "status": "pending"
  },
  "update": {
    "$set": {
      "status": "approved"
    }
  }
}
```

### Update all matching documents (`update_many`)

Apply update operators to every match of a nonempty filter. Explicitly acknowledge the bulk change; optional upsert creates a document when nothing matches.

```json
{
  "collection": "orders",
  "filter": {
    "status": "pending"
  },
  "update": {
    "$set": {
      "status": "approved"
    }
  },
  "allow_many": true
}
```

### Delete one matching document (`delete_one`)

Delete at most one document selected by a nonempty filter. No match succeeds with deleted_count=0.

```json
{
  "collection": "orders",
  "filter": {
    "status": "pending"
  }
}
```

### Delete all matching documents (`delete_many`)

Delete every document selected by a nonempty filter. Explicitly acknowledge the bulk change and inspect the filter with Find first.

```json
{
  "collection": "orders",
  "filter": {
    "status": "pending"
  },
  "allow_many": true
}
```

## Requirements and result handling

Writes selected by a filter require a nonempty object. Updates require operator objects such as `{"$set":{"status":"approved"}}`, not replacement documents. Bulk update/delete also require `allow_many:true`. Preview the same filter with Find before changing all matches. Aggregation supports at most 16 read-only stages; cross-collection, write and JavaScript stages are rejected. Inserts with stable `_id` values help detect duplicates. A batch error can follow partial inserts; inspect stored documents before retrying.

Workflow write actions are suppressed in test mode. Read/search actions still read the configured service. If a write fails after dispatch, inspect the service before repeating it; the write may already have applied.
