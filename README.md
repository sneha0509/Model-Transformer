# Model-Transformer
Smart AI powered model refresher based on Python and LLMs

## Hybrid model refresh

The **Start Model Refresh** action divides every configured batch into sub-batches
whose size is limited by **Max Parallelism**. Each sub-batch is sent to Power BI
using the selected commit mode. If a transactional sub-batch fails, every table
in that sub-batch is retried with one API request per table. The same full
sub-batch retry behavior applies to partial-batch mode. Grouped object statuses
are not treated as final because transactional rollback can report every object
as failed. Each table's individual retry determines its final status: successful
tables are green and only tables whose individual call fails remain red. An
individual result is successful only when its overall status, extended status,
object statuses, and engine messages contain no failure signal.

Power BI can briefly retain the active-refresh slot after a failed request. An
individual retry that receives `RefreshInProgressException` waits for the slot
and retries the POST instead of being recorded as a table failure.

The refresh popup keeps grouped and individual attempts separate. A failed
grouped sub-batch remains visible as `Grouped: Failed`; each table then shows
its own `Individual retry` progression and result without overwriting the
original grouped failure.

## AI-powered refresh metadata

The **AI Powered Refresh** control calls the existing advanced table-data
workflow and prepares a JSON payload for the selected tables. The payload
includes workspace and model identity, row/column/partition counts, and a flat
list of relationships for each selected table. It also includes the model's
direct upstream Fabric lineage, limited to `id`, `type`, `displayName`, and
`workspaceId`. The same `lineage` array is saved in presets after **Fetch
Advance Data**. Lineage is retrieved from the Fabric beta endpoint
`/v1/workspaces/<workspace_id>/items/<model_id>/relations/upstream?beta=true`.
The implementation is in
`core/ai_powered_refresh.py` and is exposed through the
`/api/workspaces/<workspace_id>/models/<model_id>/ai-refresh-metadata` endpoint.
Each generated payload is saved under `output/ai_refresh_metadata`; the
directory is created automatically when it does not exist.

When direct upstream lineage is empty or contains a `SqlAnalyticsEndpoint`,
the AI-powered flow also requests the semantic model definition from
`/v1/workspaces/<workspace_id>/semanticModels/<model_id>/getDefinition?format=TMDL`.
Only this AI-powered flow stores model-definition data. The generated metadata
adds `modelDefinition.format` and a `modelDefinition.partitionQueries` array;
each entry identifies its table and partition and retains the complete
partition query block in TMDL format. Partition queries are included only for
the tables selected for that AI-powered refresh, never for every table in the
semantic model.

## AI table precedence

`core/ai_table_precedence.py` reads the newest JSON file from
`output/ai_refresh_metadata` and asks GitHub Copilot to order every selected
table. The prompt evaluates whether each table appears as `fromTable` or
`toTable`, its corresponding cardinality, relationship activity, and filter
direction. The validated result retains the source row, column, and partition
counts and is saved under `output/ai_table_precedence`. Source partition TMDL
is excluded from the Copilot prompt. After the order is validated, each table's
partition queries are copied directly from the source metadata into that
table's precedence entry without LLM processing.

## AI batch creation

`core/ai_batch_creation.py` reads a saved JSON file from
`output/ai_table_precedence` and asks GitHub Copilot to create an optimized
batch plan. Copilot evaluates row count, column count, partition count, and
partition TMDL complexity. Adjacent simple or moderate tables can share a
batch, while expensive tables can be isolated. The backend rejects any plan
that omits, duplicates, or reorders precedence tables. Final plans include the
workspace and model IDs and are saved under `output/ai_batch_creation`.

The implementation uses the official GitHub Copilot SDK and signed-in-user
authentication. It does not accept or transmit a raw Copilot token. In the UI,
after the AI refresh metadata JSON is created, the application checks for a
reusable Copilot login. If authentication is required, it runs the Copilot CLI
browser flow automatically and continues setting table precedence after sign-in
completes. The stored login is reused on later refreshes.

For command-line-only use, sign in through the Copilot CLI before running the
script:

```powershell
copilot login
```

The model defaults to `gpt-5`; setting `COPILOT_MODEL` in `.env` is optional:

```text
COPILOT_MODEL=gpt-5
```

The SDK stages its managed runtime automatically on first use. It can also be
downloaded in advance with `python -m copilot download-runtime`. Run the script
against the newest metadata file:

```powershell
python -m core.ai_table_precedence
```

Or pass a specific metadata file:

```powershell
python -m core.ai_table_precedence output\ai_refresh_metadata\<file-name>.json
```

In the UI, **AI Powered Refresh** keeps the full-screen loading overlay active
while it performs all phases in sequence. It saves table metadata under
`output/ai_refresh_metadata`, creates validated precedence under
`output/ai_table_precedence`, then creates and applies the optimized batch plan
from `output/ai_batch_creation`. The screen is unlocked only after all phases
complete or an error is displayed.
