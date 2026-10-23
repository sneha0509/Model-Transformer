# Model-Transformer
Smart AI powered model refresher based on Python and LLMs

## Application areas

Run the application with `python driver.py`. The initial screen keeps the
Microsoft browser sign-in flow. After sign-in, choose **Legacy** or **Fabric**:

- **Legacy** (`/legacy`) contains the existing presets, model/table selection,
  batch editing, manual refresh, and AI-powered refresh workflows.
- **Fabric** (`/fabric`) lists the signed-in user's Fabric workspaces and
  resolves each workspace's linked Azure DevOps repository. If the workspace
  is not linked, the user can select an accessible Azure DevOps repository for
  the current flow without changing the workspace's Git settings. Select one
  repository semantic model to create a preset, which appears on Fabric home.
  The preset page shows source details, the local repository location, and an
  interactive semantic-model-first lineage graph with JSON and CSV downloads.
- **Dashboards** in either header returns to the selection screen without
  signing out.

The application is organized by area:

```text
driver.py                   Application entry point
shared/
    app.py                  Flask factory and blueprint registration
    paths.py                Stable project-root resolution
    backend/                Microsoft authentication, login/logout, request helpers
    frontend/               Login, dashboard picker, shell, theme, and session UI
legacy/
    backend/                Power BI clients, metadata, presets, batches, AI services, API routes
    frontend/               Legacy page routes, templates, and styles
fabric/
    backend/                Workspace/Git, repository models, presets, static lineage
    frontend/               Fabric selection flow, saved presets, lineage graph and inventory
```

Feature code stays within its own area; shared infrastructure does not call
Legacy services. The shared application factory is the composition point.
The Fabric REST client inside `legacy/backend` remains part of Legacy because
the existing refresh workflow uses it for lineage and model definitions.

Shared authentication uses `/api/login` and `/api/logout`. Legacy API routes
now live under `/api/legacy`; selected-table and saved-batch pages live under
`/legacy`. Workspace discovery happens only when the Legacy model selector is
opened, not during login. Opening Fabric loads its own workspace and preset lists.

Fabric semantic model discovery scans the selected repository's linked branch
(or its default branch for a manually selected repository). It recognizes model
folders containing `definition.pbism` or `model.bim`, including TMDL projects,
and uses `.platform` display names when present. Fabric presets are stored under
`presets/fabric`, separately from Legacy presets. Selecting the same workspace,
repository, branch, and model path again reopens the existing preset rather than
creating a duplicate. No remote repository files or Fabric Git settings are changed.
Use **Delete** on a saved preset's Fabric home card to remove it after confirmation.
Only that preset is deleted; the shared local repository snapshot and other
presets are retained. Recreating a deleted preset reuses the existing snapshot.

Creating or opening a Fabric preset runs `git clone` for the selected Azure DevOps
repository branch directly into the project-root `repo` folder only if its local
checkout is missing. Git must be installed and available on the server's `PATH`.
All presets for the same repository and branch share that snapshot, regardless
of workspace or semantic model. Snapshots use readable folders:
`repo/<repository-name>/<branch-name>/code`, with branch slashes replaced by
`__` and characters invalid on Windows replaced by `_`. Identity metadata
prevents different sources from sharing a folder; a short suffix is added only
when names collide. Renamed repositories keep their existing local folder.
Previously downloaded snapshots, including the older hashed folders, are still
reused without cloning again. New downloads are real Git checkouts with a `.git`
directory. Git's `core.longpaths` option and Windows extended-length filesystem
paths support deeply nested repository files. Existing checkouts are reused
across application restarts without fetching, pulling, or checking for updates.
There is no temporary `.download-*` folder, ZIP extraction, copy, or directory
rename: Git writes directly to `repo/<repository-name>/<branch-name>/code`.
The checkout is marked complete only after Git succeeds. A failed clone cleans
up its partial code folder and can be retried without saving a new preset.
Manual refreshes clone into a sibling staging folder and then replace `code`;
transient Windows access-denied errors during that swap are retried. If the
application stops between swap steps, the previous snapshot is restored and
the abandoned staging folder is removed on the next open or refresh.
If the server is interrupted or cleanup fails, an incomplete checkout is reported
instead of reused; after checking that no clone is running, remove only the
reported incomplete code folder before retrying. Previously saved
presets clone their repository on first opening if its local copy is missing.
The local `repo` folder is
excluded from version control, and the signed-in user's Azure DevOps access is
passed to Git through its subprocess environment only when cloning is needed.
Tokens are not included in command-line arguments, remote URLs, or saved Git
configuration. Repository model discovery still queries the remote branch and
does not update the saved local checkout. Clone failures show the repository,
branch, Git exit code when available, and Git error message in Fabric and the
application log, with authorization credentials redacted. A missing Git executable
or a clone exceeding the ten-minute timeout is reported explicitly.

Authentication is tracked in a signed, HTTP-only application session. A stored
browser profile alone does not grant access to dashboards or APIs. Sign-out
clears the application session and cached Microsoft credential; it does not
sign the user out of Microsoft globally. Set `FLASK_SECRET_KEY` in the
environment to keep application sessions valid across server restarts;
otherwise a process-local random key is used.

Existing `presets` and `output/ai_*` directories stay at the project root so
saved data continues to work without migration. Legacy refresh failure reports
are written to `output/legacy/failure`. Paths resolve from the project root
regardless of the current working directory.

Run the groundwork regression tests with:

```powershell
python -m unittest shared.tests.test_portal legacy.tests.test_legacy fabric.tests.test_fabric
```

## Fabric artifact mapping

Fabric presets start lineage at the **selected semantic model**, then follow its
physical source tables to **producer notebooks** and the **pipelines** that run
them. The graph reads TMDL or `model.bim`, including Direct Lake entity partitions,
literal Power Query schema/item navigation, and literal SQL queries. Logical
model table names and physical source names are kept separately. Calculated
tables and unresolved sources remain visible without invented producers.
Resolved notebook callers and parent pipelines are followed with cycle protection.
Readers of a source table are not presented as its upstream producers.

The preset graph supports node dragging, canvas panning, scroll/pinch zoom,
zoom buttons, fit-to-view, layout reset, node search, and a model-table selector
for focusing on one branch. Nodes and relationships are keyboard accessible;
select either to inspect source evidence and pipeline invocation parameters.
Confirmed source identities use solid relationships. Table-name matches whose
schema, workspace or lakehouse identity cannot be proved (for example, a OneLake GUID
versus a notebook's explicit lakehouse name) are **dashed, unverified candidates**,
not confirmed lineage. Conflicting known scopes are excluded. Multiple possible
producers are retained rather than arbitrarily selecting one. Missing producers,
unsupported partition expressions, and uncertain scopes are listed as analysis
gaps. This remains static notebook-level lineage, not proof of runtime execution
or column-level flow.

Older saved mappings rebuild automatically from the existing local snapshot on
their next open, without fetching or pulling the repository. **Refresh repository
& lineage** still explicitly downloads the latest branch and regenerates reports.

Opening a Fabric preset scans **all Fabric `.DataPipeline` and `.Notebook`
folders in its downloaded repository snapshot**, not only artifacts related to
the selected semantic model, to discover the selected model's producers and
orchestrators. The graph is model-specific; the complete searchable artifact
inventory remains below it. Legacy folders and Git internals are excluded.
Windows long-path support keeps deeply nested artifacts in the scan.

The mapping joins pipeline notebook IDs to exported `.platform` logical IDs,
includes nested activity containers and child-pipeline references, and resolves
notebook table dependencies using Python AST inspection and Spark SQL parsing.
Notebook code is never executed and no cloud artifacts are changed. CTEs and
session temporary views are excluded from persisted tables. Each dependency
includes the notebook source path, line, and operation; pipeline calls include
the activity JSON pointer, parameters, workspace reference, and dependencies.
The page includes pipeline mappings, a complete notebook inventory, reverse
table producer/consumer mappings, and explicit analysis gaps.

Python source warnings (including invalid string escapes such as `"\."` and
`"\$"`) are recorded once per notebook location in the analysis issues instead
of repeated `<unknown>` console warnings. The downloaded code is not rewritten.
Nested dynamic SQL expressions are kept as unresolved references. If a standalone
interpolated SQL clause prevents parsing, the analyzer retries the static
statement without that clause and records an explicit coverage gap; it does not
infer dependencies inside the missing clause. Unsupported SQL commands and
malformed SQL remain source-located gaps rather than repeated parser log messages.

Literal pipeline parameter overrides apply only to tagged parameter cells.
Notebook-default and pipeline-invocation contexts are reported separately.
Unqualified table identity includes its exported default lakehouse binding;
explicit workspace/lakehouse names are not guessed to be equivalent to GUIDs.
Runtime expressions, unsupported syntax, missing IDs, and unmatched parameters
are disclosed as gaps rather than silently omitted or guessed. Dependencies
are notebook-level, not column-level or proof that every read feeds every write.

Reports are regenerated on page opening and persisted under
`output/fabric/lineage/<preset-id>/mapping.json` and `mapping.csv`.
The JSON contains the selected model's sources and graph, full inventory, calls,
tables, reverse mappings, evidence, and issues. The CSV provides model-relationship
evidence rows (including scope verification status), each pipeline/table operation
and notebook-default context, plus unresolved call rows. Downloads require
sign-in and an existing preset. Scanning uses the local snapshot; it does not
fetch or pull the repository. Install the declared `sqlglot` dependency through
the normal requirements workflow.

Run Fabric mapping and regression tests with:

```powershell
python -m unittest fabric.tests.test_lineage fabric.tests.test_fabric
```

## Fabric AI impact analysis and schema cascade recommendations

Open a Fabric preset and choose the **Impact Analysis** button. This desktop
page fetches the selected semantic model's **current tables and columns through
the Fabric REST API** using the signed-in Microsoft account. It resolves the
model by name in the preset workspace (including paginated results), retrieves
its TMSL definition, and polls long-running definition requests when necessary.
Missing or ambiguous models and denied API access are reported explicitly;
there is no fallback to an outdated repository schema.
If the repository display name differs from the deployed model, the page lists
the workspace's accessible live semantic models for explicit selection.
After a successful definition fetch, **Load tables & columns** saves the chosen
Fabric model ID in that preset. Later schema requests use this ID, even if the
live model is renamed; the repository selection and lineage path stay unchanged.
A deleted or inaccessible binding requires reselection rather than silently
switching to a different same-named model. An empty workspace list explains that
the model must be deployed and accessible in the selected workspace.

Select impacted tables or individual columns across multiple tables (up to 200
columns). Describe **all intended changes in one shared instruction box**;
there are no per-column change-type selectors, target fields, or comment boxes.
Include table/column names and explain whether the physical source column,
logical model column, or both change, along with target names/types and any
replacement, migration, or business rules. The complete shared instructions are
passed to Copilot for each artifact and retained in the report.
Physical `sourceColumn` mappings and partition sources are preserved separately
from logical table and column names.

This version is **review-only**. It neither edits local files nor changes live
Fabric items or source schemas. Analysis uses the **live selected model
definition** from Fabric and inventories exported Fabric notebooks and other
semantic models in the preset's **existing local repository snapshot**.
The selection page does not need the snapshot, but running the code analysis
requires it. Use the preset's explicit **Refresh repository &
lineage** action before analysis when newer branch content is needed; impact
analysis never fetches or pulls automatically.

The backend uses the official `github-copilot-sdk` with the desktop's signed-in
GitHub Copilot identity. If sign-in is required, impact analysis displays a
**Sign in to GitHub Copilot** popup. Choose **Sign in with GitHub** to open the
browser authorization flow on the computer running the backend. After successful
authorization, analysis retries automatically with the selected columns and
instructions preserved. Cancel before starting sign-in to leave the inputs
unchanged, or retry from the popup if authorization fails. Browser authorization
waits up to five minutes. Install the `copilot` CLI on that desktop for this login
flow; the SDK-managed inference runtime is not an interactive login CLI.
The app checks that the installed CLI supports `copilot login --web-flow` and
explicitly requires that flow, with no device-code fallback. If unsupported,
update the CLI and retry. GitHub redirects authorization to the CLI's local
loopback callback; the CLI stores the credentials and the SDK reuses them.
No device pairing code needs to be copied or entered. GitHub can still require
the account's normal two-factor authentication.
Microsoft sign-in and GitHub Copilot sign-in are separate.
The existing SDK dependency is sufficient; no API key or raw Copilot token is
accepted by this workflow. Optionally set `FABRIC_IMPACT_MODEL` to an available
Copilot model ID. When unset (or set to `auto`), the application selects an
available concrete model with advertised context limits that fits the targeted
request, rather than creating a session with the limit-less `auto` router.
Automatic selection prefers a lower advertised billing multiplier; when pricing
is unavailable, lightweight mini/Haiku/Flash models are preferred. Policy-disabled
or unconfigured models are excluded. Explicit concrete IDs are honored, never
silently replaced. If the configured model is unavailable or lacks usable limits,
the error lists eligible IDs. No context window is guessed, and every session is
checked to confirm the selected model before inference. If no model fits, the
artifact is skipped with the existing exact context-budget gap. This desktop workflow
does not require a separate approval checkbox before analysis.

The web application and standalone AI scripts automatically keep the SDK-managed
runtime in `.session/copilot-runtime/<installed-sdk-version>/`, relative to the
project root rather than the current working directory. No machine-specific
path, runtime version, or `.env` setting is required. Startup sets
`COPILOT_CLI_EXTRACT_DIR` to this location, replacing any existing value. The
gitignored cache is provisioned on first use and reused across analyses; upgrading
the SDK selects a separate cache automatically. This changes only the managed
runtime cache, not Copilot authentication storage or session working directories.
Do not delete the cache while a runtime is using it. Concurrent first-use
provisioning and endpoint-protection restrictions can still require attention.

Before inference, analysis maps the selected columns' physical source tables and
backtracks notebook producers and their upstream inputs, with cycle protection.
It separately follows readers and downstream writes for cascade candidates,
without expanding unrelated consumers of upstream-only tables. Notebook callers
and parent pipeline invocations supply execution context. Known conflicting
schemas/workspaces/lakehouses are excluded; unverified scopes remain candidates
with explicit gaps. The selected live model and other models using affected
physical tables are retained. Other artifacts are marked `out_of_scope`, not
claimed as analyzed.

Code, not AI, then builds an **object-level impacted lineage**. The saved graph
contains selected columns, physical tables, artifacts, notebook cells, model
objects and pipeline activities, with typed dependency edges and evidence levels.
TMDL and TMSL (`model.bim`) indexing resolves source mappings, DAX references and
transitive measure/calculated-table dependencies, relationship endpoints,
sort-by columns, hierarchies, role filters and source expressions. A dependency
is a review candidate, not a claim that an edit is required. Pipeline activities
that invoke affected notebooks/pipelines or contain literal physical-table
metadata are targeted separately; unrelated activities are excluded. Referenced
pipeline parameter/variable declarations are included without unrelated global
metadata. Literal parameter defaults can identify candidates, but runtime overrides
and dynamic expressions still require validation. When a downstream table is
known but its derived column mapping cannot be resolved, its source binding stays
in scope and the missing column/calculation coverage is explicitly disclosed.

Notebook requests contain **complete relevant code cells**, not a concatenated
whole-notebook export. Static variable, function, import and temporary-view
dependencies retain prerequisite and consuming cells, including parameter cells.
When cell dependencies cannot be safely resolved (for example, non-Python syntax
or dynamic SQL), the table-lineage pass conservatively retains candidates, but
the AI targeting pass sends only evidenced cells and resolvable prerequisites.
Omitted dynamic dependencies are explicit manual-review gaps, not a reason to
send the entire notebook. Imports and parameter prerequisites remain context
rather than being presented as affected code by default. Markdown, outputs and
Fabric export metadata are excluded. Requests retain original notebook cell
numbers and exported-code lines; AI citation locations are unverified in v1.
Fixes are still validated against supplied cells only.
The report shows selected cells and per-request context budgets.

AI runs **automatically only for code-identified targets**, one scoped request
per affected artifact, with relevant selected columns and shared instructions.
Model and pipeline requests contain exact source fragments for selected objects
and their prerequisites, never entire definitions (even for compact single-line
JSON). Original source coordinates are retained. Copilot generates **actual
replacement code** for each affected target; it cannot expand the target set.
Each editable source entity has a content-addressed `sourceId`; context-only
prerequisites are marked non-editable. Copilot returns that ID and the complete
AI-generated `after` code, not a retyped original. The backend supplies the exact
`file` and `before` text, verifies the ID belongs to the named target, and
validates source scope and replacement syntax before generating the diff.
Raw source, whitespace and final newlines are preserved in the request instead
of a line-numbered rendering. Original `startLine` coordinates remain available
for citations. Saved reports and caches retain the existing complete
`before`/`after` format and are revalidated against the supplied source.
Prose-only or manual-validation findings are
rejected and retried through the correction attempt, never saved as successful
actions. A second invalid response is an explicit analysis error. For a numeric
column becoming text, the AI is instructed to generate appropriate conversions
and aggregation rewrites, rather than telling the user to implement a conversion.
Missing business rules, unknown replacements or unresolved behavior remain
explicit code-generation gaps; no alternate columns or business rules are invented.
Targets with no code change needed are
recorded separately as `unaffectedTargets`, with reasons and evidence, for
coverage and cache validation; they are not displayed as impact findings.
Unresolved targets remain explicit coverage gaps rather than no-impact claims.
Short impact/recommendation metadata remains in the report JSON for compatibility
but is not rendered as prose or manual checklists in action cards.
The backend extracts the complete affected BIM/JSON object, TMDL declaration or
notebook code cell and merges source-checked edits into its before/after versions.
Edits to another target or a prerequisite cannot substitute for fixing the named
entity. Ambiguous matches and overlapping replacements are rejected.
JSON replacements must parse and preserve the entity's JSON type. Python
replacement syntax is checked when the original cell parses as Python; this
does not execute code or validate DAX, Scala, notebook magics or runtime behavior.
The backend generates a unified `diff` for each source-validated replacement,
including deletions and missing final newlines; it does not trust AI-generated
patch text. These are **snippet diffs**: hunk coordinates are relative to the
displayed snippet, not the full file, and are not apply-ready file patches.
A physical rename does
not necessarily require renaming the logical model column or rewriting DAX.
No proposed change is automatically applied.

Scala exports named `notebook-content.scala` are discovered alongside Python and
IPYNB exports. Scala `// CELL`, parameter, markdown and metadata boundaries retain
the original cell/line coordinates and exclude export metadata from code previews.
Literal Spark table reads, `saveAsTable`/`insertInto` writes, SQL calls and notebook
invocations supply candidate lineage. Scala variables, interpolated strings,
runtime parameter values and cross-cell dependencies are not resolved and remain
explicit coverage gaps, not inferred column flow.

The desktop page keeps column selection and the change description side by side.
Reports start with an overview of affected artifacts, findings, high-priority
items, and selected columns. Findings are grouped by **Semantic model**,
**Notebook**, or **Pipeline**, ordered by priority, and filterable by artifact
type. Each action presents its entity/location and visible **Before**,
**After (AI proposed)**, and a highlighted **Unified diff**. Impact paragraphs,
prose recommendations, implementation notes and manual-validation checklists
are not shown. Source references remain secondary, expandable evidence.
Only artifacts with findings appear in the affected counts,
cards and filters; static dependencies or analyzed status alone do not create
an impact card. Older prose-only findings are shown as generation gaps with a
rerun action, not as manual-validation cards or no-impact claims.
Older reports without generated diffs still show their before/after code and
prompt for a new analysis to generate the diff. Source references and
dependency connections provide progressively more detail. Code generation gaps
remain visible without exposing internal identifiers or request diagnostics.
**Export PDF** opens the desktop print dialog; choose **Save as PDF** to save the
formatted report. Exports include all artifact categories regardless of the active
filter, with before/after entity code and diffs included, without impact prose
or validation checklists. Raw source-reference details are omitted.

Copilot tools, shell access, configuration discovery, skills, and file hooks are
disabled; permission requests are denied. Notebook code is never executed.
Every finding must reference a relevant selected change, a code-identified object
and a supplied artifact. AI citations are best-effort: their file names,
line ranges and excerpts are not checked against the supplied source and do not
block analysis when they are inaccurate. Source reference details ask users to
confirm locations before applying a fix; saved report limitations retain the
citation verification status. Basic citation field types are still checked so
the report can be rendered. Original replacement snippets remain checked against
the actual supplied live-definition or selected cell text.
AI output that fails the remaining checks receives one explicit correction attempt in a fresh
session, with the original scoped input and a new budget check, so invalid output
and conversation history cannot accumulate past the context budget. Output is
validated again; repeated invalid output, authentication failures, SDK errors,
and timeouts are reported as failures, never as successful empty analysis.

Progress is streamed while artifacts are analyzed. Reports passing those checks are saved
atomically under `output/fabric/impact/<preset-id>/<report-id>.json`; the existing
report API retains access to the structured data. Reports retain source selection, snapshot
fingerprint, live model revision, shared instructions, findings, proposed fixes,
code-generated targets and impacted lineage, AI request/cache counts, artifact
coverage, gaps, and limitations. AI explanations are cached locally under
`output/fabric/impact/<preset-id>/cache`. Repeated identical target source,
instructions, change context, prompt/schema and concrete model reuse the cached
result after response-format, target-scope and proposed-replacement validation
instead of sending another inference request. Cached citations are also unverified.
Changes to those inputs invalidate reuse. Cache failures are logged and trigger
fresh analysis. No Copilot connection is made when code finds no targets.
The live definition is fetched again
when analysis starts; if it has changed, the user must refresh the table/column
selection. Reports and cached explanations contain code excerpts and user instructions and should be
handled as repository-sensitive data.

Notebook and other-model coverage is local and static, not a guarantee that every runtime
dependency or workspace item was found. Dynamic code, ambiguous lakehouse scopes,
unreadable definitions and parser gaps are disclosed. Each artifact is limited
to 100,000 selected source characters; a request including numbered cells/model
source, scoped lineage and change context is limited to 180,000 characters.
Before **every** send, the active model's advertised prompt and context-window
limits are checked against a conservative UTF-8-byte token upper bound for the
entire request, system instructions and response JSON schema. The check reserves
8,192 output tokens (also enforced through the SDK), 4,096 protocol tokens and an
additional safety margin of at least 4,096 tokens or 5% of the context window.
This deliberately conservative bound can reject input that an exact tokenizer
would accept; it never assumes four characters per token. Automatic session
compaction is disabled so source evidence is not silently summarized.
Oversized requests/cell sets are skipped with an explicit gap and **partial**
coverage, never silently truncated. If no scoped artifact can be analyzed, the
operation fails without saving a success-shaped report.

Run the focused tests and Fabric regressions with:

```powershell
python -m unittest fabric.tests.test_impact fabric.tests.test_fabric_schema fabric.tests.test_lineage fabric.tests.test_fabric
node --check .\fabric\frontend\static\js\impact.js
node --test .\fabric\tests\impact_login.test.cjs
```

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
`legacy/backend/ai_powered_refresh.py` and is exposed through the
`/api/legacy/workspaces/<workspace_id>/models/<model_id>/ai-refresh-metadata` endpoint.
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

`legacy/backend/ai_table_precedence.py` reads the newest JSON file from
`output/ai_refresh_metadata` and asks GitHub Copilot to order every selected
table. The prompt evaluates whether each table appears as `fromTable` or
`toTable`, its corresponding cardinality, relationship activity, and filter
direction. The validated result retains the source row, column, and partition
counts and is saved under `output/ai_table_precedence`. Source partition TMDL
is excluded from the Copilot prompt. After the order is validated, each table's
partition queries are copied directly from the source metadata into that
table's precedence entry without LLM processing.

## AI batch creation

`legacy/backend/ai_batch_creation.py` reads a saved JSON file from
`output/ai_table_precedence` and asks GitHub Copilot to create an optimized
batch plan. Copilot evaluates row count, column count, partition count, and
partition TMDL complexity. Adjacent simple or moderate tables can share a
batch, while expensive tables can be isolated. The backend rejects any plan
that omits, duplicates, or reorders precedence tables. Final plans include the
workspace and model IDs and are saved under `output/ai_batch_creation`. Each
plan also carries the default hybrid refresh settings: transactional commit,
30-minute timeout, three retries, and maximum parallelism of four.

The implementation uses the official GitHub Copilot SDK and signed-in-user
authentication. It does not accept or transmit a raw Copilot token. In the UI,
after the AI refresh metadata JSON is created, the application checks for a
reusable Copilot login. If authentication is required, it runs the Copilot CLI
browser flow automatically and continues setting table precedence after sign-in
completes. The stored login is reused on later refreshes.

For command-line-only use, sign in through the Copilot CLI before running the
script:

```powershell
copilot login --web-flow
```

The model defaults to `gpt-5`; setting `COPILOT_MODEL` in `.env` is optional:

```text
COPILOT_MODEL=gpt-5
```

The SDK stages its managed runtime automatically on first use in the project-local
cache described above. Run the script against the newest metadata file:

```powershell
python -m legacy.backend.ai_table_precedence
```

Or pass a specific metadata file:

```powershell
python -m legacy.backend.ai_table_precedence output\ai_refresh_metadata\<file-name>.json
```

In the UI, **AI Powered Refresh** keeps the full-screen loading overlay active
while it performs all phases in sequence. It saves table metadata under
`output/ai_refresh_metadata`, creates validated precedence under
`output/ai_table_precedence`, then creates and applies the optimized batch plan
from `output/ai_batch_creation`. That exact saved batch JSON is handed to the
existing `BatchRefreshManager`, which uses the same grouped sub-batch and
individual retry strategy as **Start Model Refresh**. The existing refresh
progress dialog opens and remains active while the job is running. The screen
is unlocked only after all phases complete or an error is displayed.
