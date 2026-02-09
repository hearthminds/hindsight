---
applyTo: '**'
---

# Hindsight — Development Guidelines

## GPU Rate Limiting for vLLM

0.3s delay between inference calls balances throughput and stability for 70B AWQ models.

**Tags:** `hindsight`

### The Sweet Spot

| Delay | GPU Utilization | Notes |
|-------|-----------------|-------|
| 0s | ~100% | Risk of memory pressure, OOM |
| 0.3s | ~80% | Good balance |
| 0.5s | ~50% | Too conservative |

### Implementation

```python
parser.add_argument('--delay', type=float, default=0.3, 
                    help="Delay between LLM calls (rate limiting)")

# In processing loop:
if args.delay > 0 and i > 0:
    time.sleep(args.delay)
```

### Why Rate Limit?

- **Memory pressure**: Concurrent requests accumulate KV-cache
- **Queue buildup**: Async APIs queue faster than processing
- **Stability**: Sustained 100% utilization can cause hangs

### Queue Management for Batch Imports

For large imports, also consider queue-aware batching:

```python
# Submit N items, wait for queue to drain, repeat
--batch-size 10 --max-queue 3
```

This prevents unbounded queue growth while maintaining throughput.

*Source: F-009 Hindsight Import Execution*


## Hindsight API Design Notes

Core design rules for the Hindsight API surface.

**Tags:** `hindsight`

### Single-Bank Principle

All endpoints operate on a single bank per request. Multi-bank queries are the client's responsibility to orchestrate.

```
POST /v1/default/banks/{bank_id}/memories/recall   → searches one bank
POST /v1/default/banks/{bank_id}/memories/retain    → stores in one bank
POST /v1/default/banks/{bank_id}/reflect            → reflects on one bank
```

### Disposition Traits

Disposition traits (personality/behavioral configuration) only affect `reflect`, not `recall`. Recall returns raw memory regardless of disposition — reflection is where personality shapes the response.

### Endpoint Conventions

- All memory operations are bank-scoped
- Bank ID appears in the URL path, not query params
- Request/response bodies use Pydantic models (see Pydantic Model Pattern)


## Adding New Config Flags

Step-by-step workflow for adding configuration flags to Hindsight.

**Tags:** `hindsight`

### Steps

1. **config.py** (`hindsight-api/hindsight_api/config.py`):
   - Add `ENV_*` constant and `DEFAULT_*` constant
   - Add field to `HindsightConfig` dataclass
   - Initialize in `from_env()`

2. **main.py**: Add to manual `HindsightConfig()` constructor (search "CLI override")

3. **Usage in code**:
   ```python
   from ...config import get_config
   config = get_config()
   value = config.your_new_field
   ```

4. **Docs**: Update `hindsight-docs/docs/developer/configuration.md`

### Key Points

- Config values come from environment variables via `from_env()`
- The `HindsightConfig` dataclass is the single typed source of truth
- CLI overrides in `main.py` take precedence over env vars
- Always document new flags in the Docusaurus docs site


## Hindsight Context Window Budget

vLLM context is shared between prompt, input, and output. Budget carefully.

**Tags:** `hindsight`

### Token Allocation

| Component | Tokens | Notes |
|-----------|--------|-------|
| vLLM max context | 54,000 | `--max-model-len 54000` |
| Hindsight extraction prompt | ~23,000 | Fixed overhead |
| max_completion_tokens | 16,000 | Response budget |
| **Available for input** | **~15,000** | What's left for your content |

### The Math

```
Input budget = Context window - Prompt - Completion
             = 54,000 - 23,000 - 16,000
             = 15,000 tokens (~27% of total)
```

### Import Implications

Chunks exceeding ~15k tokens cause vLLM 400 errors. Pre-split before sending to Hindsight.

```python
# Conservative default: 10k tokens max per chunk
DEFAULT_MAX_CHUNK_TOKENS = 10_000

# Estimation: ~4 chars per token for English
est_tokens = len(content) // 4
```

### Why 10k Not 15k?

- Safety margin for token estimation variance
- Hindsight adds formatting overhead
- Better to split slightly more than fail

### Future: KV-Cache Compression

If KV-cache 4-bit compression becomes available:
- Context could increase to ~108k
- Input budget would grow to ~50k+
- Chunk size limits can be relaxed

All chunk size parameters are configurable via CLI args for this reason.

*Source: F-009 Hindsight Import Execution (Phase 2.8 context window analysis)*


## Control Plane Route Proxies

When modifying dataplane API parameters, also update the control plane route proxies.

**Tags:** `hindsight`

### Routes to Update

| Control Plane Route | Proxied API Endpoint |
|---------------------|----------------------|
| `recall/route.ts` | `/v1/default/banks/{bank_id}/memories/recall` |
| `reflect/route.ts` | `/v1/default/banks/{bank_id}/reflect` |
| `memories/retain/route.ts` | `/v1/default/banks/{bank_id}/memories/retain` |

### Checklist for New API Parameters

1. Extract from `body` in the route handler
2. Pass to SDK call
3. Update type definition in `lib/api.ts`
4. Update UI components if needed

### Why This Matters

The control plane (Next.js) proxies API calls to the dataplane (FastAPI). If a new parameter is added to the API but not forwarded by the proxy, the control plane UI silently drops it. Fail-hard doesn't help here — the proxy just never sends what it doesn't know about.


## Hindsight Dev Commands

Common development commands for working with Hindsight.

### API Server

```bash
./scripts/dev/start-api.sh           # Start API server
cd hindsight-api && uv run pytest    # Run all tests
cd hindsight-api && uv run ruff check .    # Lint
cd hindsight-api && uv run ruff format .   # Format
cd hindsight-api && uv run ty check hindsight_api/  # Type check
```

### Control Plane

```bash
./scripts/dev/start-control-plane.sh  # Start Next.js dev
cd hindsight-control-plane && npm run dev
```

### Documentation

```bash
./scripts/dev/start-docs.sh  # Docusaurus site
```

### Always Lint Before Commit

```bash
./scripts/hooks/lint.sh  # Same checks as pre-commit
```

### Environment Setup

```bash
cp .env.example .env
uv sync --directory hindsight-api/
npm install
```


---



## Database Migration Template

Alembic migration template with multi-tenant schema support for Hindsight.

**Tags:** `hindsight`

### Template

```python
"""Description of the migration

Revision ID: f1a2b3c4d5e6
Revises: <previous_revision_id>
Create Date: YYYY-MM-DD
"""
from collections.abc import Sequence
from alembic import context, op

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "<previous_revision_id>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _get_schema_prefix() -> str:
    """Get schema prefix for table names (required for multi-tenant support)."""
    schema = context.config.get_main_option("target_schema")
    return f'"{schema}".' if schema else ""

def upgrade() -> None:
    schema = _get_schema_prefix()
    op.execute(f"CREATE INDEX ... ON {schema}table_name(...)")

def downgrade() -> None:
    schema = _get_schema_prefix()
    op.execute(f"DROP INDEX IF EXISTS {schema}index_name")
```

### Running Migrations

```bash
uv run hindsight-admin run-db-migration
uv run hindsight-admin run-db-migration --schema tenant_xyz
```

### Key Points

- Every migration must include both `upgrade()` and `downgrade()`
- Use `_get_schema_prefix()` for all table references — required for multi-tenant bank isolation
- `context.config.get_main_option("target_schema")` provides the tenant schema at runtime


## Hindsight pg0 Architecture

Hindsight uses embedded PostgreSQL (pg0) stored in `~/.hindsight/{bank}/.pg0/`.

**Tags:** `hindsight`

### Key Concept

pg0 is **not** a separate container—it runs inside the Hindsight container. Each Hindsight instance has its own embedded PostgreSQL.

### Data Directories

```
~/.hindsight/aletheia/.pg0/  → Hindsight-aletheia's database
~/.hindsight/logos/.pg0/     → Hindsight-logos's database  
~/.hindsight/shared/.pg0/    → Hindsight-shared's database
```

### Port Assignments

See `infra_scripts/setup/start_hearthminds.sh` for current port assignments. Ports may change; the startup script is the source of truth.

### Common Operations

**Connect directly:**
```bash
PGPASSWORD=hindsight psql -h localhost -p $PORT -U hindsight -d hindsight
```

**Wipe and reinitialize:**
```bash
podman stop hindsight-aletheia
rm -rf ~/.hindsight/aletheia/.pg0
podman start hindsight-aletheia
# Hindsight reinitializes pg0 on startup
```

**Check what databases exist:**
```bash
PGPASSWORD=hindsight psql -h localhost -p $PORT -U hindsight -c "\\l"
```

### Persistence Behavior

- Container stop/start: Data preserved
- Container rm/run: Data preserved (bind mount)
- Delete `.pg0` directory: Data wiped, fresh start

### The aletheia_source Database

The `aletheia_source` database contains the immutable `raw_conversation` table—source of truth for conversation history. Check which Hindsight instance hosts it via the startup script.

*Source: F-009 Hindsight Import Execution*


## Pydantic Model Pattern

Prefer Pydantic models over raw dicts for validated, typed data throughout Hindsight.

**Tags:** `hindsight`

### Anti-Pattern

```python
# BAD — error-prone dict access
def process(data: dict) -> str:
    return data.get("name", "")  # No validation, silent failures
```

### Pattern

```python
# GOOD — typed and validated
class UserData(BaseModel):
    name: str
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def ensure_tz_aware(cls, v):
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

def process(data: UserData) -> str:
    return data.name  # Type-safe, validated at construction
```

### Why

- Type safety at construction, not access
- Validators catch timezone-naive datetimes, malformed dates, etc.
- IDE autocompletion and refactoring support
- Self-documenting data contracts


## raw_conversation Schema

The immutable source of truth for conversation history.

**Tags:** `hindsight`

### Location

Database `aletheia_source` on the Hindsight-logos embedded PostgreSQL. Check `start_hearthminds.sh` for current port.

### Schema

```sql
CREATE TABLE raw_conversation (
    id              SERIAL PRIMARY KEY,
    original_id     TEXT UNIQUE,          -- Deduplication key
    conversation_id TEXT NOT NULL,        -- Thread grouping
    user_uuid       TEXT,                 -- Owner (e.g., 'aaron_chapin')
    turn_number     INTEGER,              -- Order within conversation
    role            TEXT NOT NULL,        -- user, assistant, tool, system
    content         TEXT,                 -- Message text
    created_at      TIMESTAMPTZ,          -- Original timestamp
    imported_at     TIMESTAMPTZ DEFAULT NOW(),
    semantic_chunk_id INTEGER,            -- Set by boundary detection
    lora_trained_at   TIMESTAMPTZ,        -- Training tracking
    lora_epoch_count  INTEGER DEFAULT 0
);
```

### Indexes

```sql
CREATE INDEX idx_raw_conv_conversation ON raw_conversation(conversation_id);
CREATE INDEX idx_raw_conv_chunk ON raw_conversation(semantic_chunk_id);
CREATE INDEX idx_raw_conv_created ON raw_conversation(created_at);
```

### Key Principle

**This table is immutable.** Only `semantic_chunk_id`, `lora_trained_at`, and `lora_epoch_count` are updated. Original conversation data is never modified.

### Derived Data

Hindsight's memory banks contain derived data (entities, links, embeddings). This can be regenerated from `raw_conversation` at any time.

### Useful Queries

```sql
-- Row count
SELECT COUNT(*) FROM raw_conversation;

-- Turns per conversation
SELECT conversation_id, COUNT(*) as turns
FROM raw_conversation 
GROUP BY conversation_id 
ORDER BY turns DESC;

-- Chunk distribution
SELECT semantic_chunk_id, COUNT(*) as turns
FROM raw_conversation 
WHERE semantic_chunk_id IS NOT NULL
GROUP BY semantic_chunk_id
ORDER BY semantic_chunk_id;
```

*Source: F-009 Hindsight Import Execution*


## TDD: Test-Driven Development

HearthMinds follows strict TDD methodology: **tests before code, always**.

### The Cycle
1. **Red** — Write a failing test that defines expected behavior
2. **Green** — Write minimal code to make the test pass
3. **Refactor** — Clean up while keeping tests green

### Principles
- **Fail hard, fail fast** — Tests should be strict and fail loudly
- **Tests are documentation** — They define expected behavior
- **No code without a test** — If it's not tested, it doesn't work
- **One assertion per test** — Keep tests focused and readable

### When Tests Fail
A failing test is information. Before "fixing" it:
1. Understand WHY it fails
2. Determine if the test or the code is wrong
3. Fix the root cause, not the symptom

## HearthMinds Code Conventions

### General
- **Clarity over cleverness** — Write code that future-you can understand
- **Explicit over implicit** — Make dependencies and assumptions visible
- **Small functions** — Each function does one thing well
- **Meaningful names** — Variables and functions describe their purpose

### Python
- Type hints required for function signatures
- Docstrings for public functions
- `black` for formatting, `ruff` for linting
- Prefer `pathlib` over string path manipulation

### SQL
- Uppercase keywords: `SELECT`, `FROM`, `WHERE`
- Lowercase identifiers: `agent_roles`, `knowledge_modules`
- Always include migration rollback scripts
- Explicit column lists (no `SELECT *` in production code)

### Git
- Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`
- One logical change per commit
- Write commit messages for someone who doesn't have your context

## Git Workflow

### Branches
- `main` — Production-ready code, always deployable
- Feature branches: `feat/short-description`
- Fix branches: `fix/issue-description`

### Commit Messages
Format: `<type>: <short description>`

Types:
- `feat:` — New feature
- `fix:` — Bug fix
- `refactor:` — Code change that neither fixes nor adds
- `docs:` — Documentation only
- `test:` — Adding or updating tests
- `chore:` — Maintenance tasks

### Pull Request Flow
1. Create branch from `main`
2. Make changes with atomic commits
3. Ensure tests pass locally
4. Push and create PR
5. Address review feedback
6. Squash merge to `main`

### Rules
- Never force-push to `main`
- Rebase feature branches on `main` before merging
- Delete branches after merge

## Cross-Repo Generation

All knowledge management runs from hearthminds-org. Generated artifacts are committed to each repo so they work standalone.

### Hub-and-Spoke Model

```
hearthminds-org (hub)
├── knowledge_modules table (source of truth)
├── generate_agent_docs.py (generator)
├── → .github/copilot-instructions.md  (org)
├── → ~/hearthminds-hindsight/.github/copilot-instructions.md
└── → ~/hearthminds-moltbot/.github/copilot-instructions.md
```

### Tag-Based Filtering

Modules are included in a repo's output based on tags:

| Tag | Behavior |
|-----|----------|
| (none / `is_universal=true`) | Included in **all** repos |
| `hindsight` | Included in hindsight output only |
| `moltbot` | Included in moltbot output only |
| `hearthminds-core` | Included in org output only |
| `copilot-instructions` | Included in default (no-repo) output |

### CLI Usage

```bash
# Generate for a specific repo
python scripts/generate_agent_docs.py --copilot-instructions --repo hindsight

# Generate for all repos at once
python scripts/generate_agent_docs.py --copilot-instructions --all-repos

# Generate only for default (hearthminds-org)
python scripts/generate_agent_docs.py --copilot-instructions
```

### REPO_CONFIG

The generator uses a `REPO_CONFIG` dictionary mapping repo tags to output paths:

```python
REPO_CONFIG = {
    "hearthminds-org": {"title": "HearthMinds", "output_base": "."},
    "hindsight":       {"title": "Hindsight",   "output_base": "~/hearthminds-hindsight"},
    "moltbot":         {"title": "Moltbot",     "output_base": "~/hearthminds-moltbot"},
}
```

### Key Principle

Each repo's `git clone` produces a working context — generated files are committed artifacts, not live-synced. The hub (hearthminds-org) is the only place that writes to other repos' working trees.

*Source: F-013 Knowledge Pipeline Hardening, Phase 2*


## Doc Pipeline Commit Discipline

Only the documentation agent commits generated context files. This is a pipeline rule, not a suggestion.

### The Rule

The documentation agent owns the generate → commit cycle for:
- `.github/copilot-instructions.md` (all repos)
- `.github/agents/*.agent.md` (hearthminds-org)
- `docs/generated/*.md` (hearthminds-org)

Other agents (architecture, database, testing, etc.) **propose** module content. The documentation agent reviews, inserts, regenerates, and commits.

### Why

This prevents three failure modes:
1. **Direct editing** — Generated files edited by hand, bypassing the database (source of truth drift)
2. **Conflicting regeneration** — Multiple agents running `generate_agent_docs.py` and committing different outputs
3. **Context drift** — Database state and committed files get out of sync

### The Workflow

```
Any agent: writes pattern/content → docs/modules/my-pattern.md
Documentation agent: reviews → insert_module.py → generate_agent_docs.py → commit
```

### When to Regenerate

After any session that modifies:
- Knowledge modules (insert, update, delete)
- Agent roles (new role, changed description)
- Role-module mappings (role_modules table)

### Exception

Database agents may create migration files, scripts, and test files — these are source artifacts, not generated outputs. The commit discipline applies only to **generated** documentation files.

*Source: F-013 Knowledge Pipeline Hardening*


## Fail Hard Configuration

Silent environment variable defaults violate "fail hard, fail fast." Hardcode at build time.

**Tags:** `hearthminds-core`

### The Problem

Runtime configurability via `os.environ.get()` with defaults makes failures non-deterministic.

```python
# ✗ Anti-pattern: Silent fallback
max_tokens = int(os.environ.get("MAX_TOKENS", "65000"))
# If 65000 is wrong, fails mysteriously at runtime
# Different behavior depending on environment
```

### The Pattern

Hardcode known-good values at build time. Fail fast if assumptions are wrong.

```dockerfile
# ✓ Pattern: Build-time configuration with verification
RUN grep -q 'max_completion_tokens=65000' "$FILE" || exit 1  # Fail if upstream changed
RUN sed -i 's/65000/16000/' "$FILE"                          # Apply known-good value
```

### Why This Matters

- **TDD principle**: Tests should fail loudly, not pass silently with wrong values
- **Reproducibility**: Same image = same behavior everywhere
- **Debugging**: Build fails immediately vs runtime mystery

### When Runtime Config Is Appropriate

Runtime configuration is fine for:
- User-facing settings (ports, log levels)
- Environment-specific values (database URLs)
- Values explicitly designed to vary

Runtime config is **not** appropriate for:
- Internal implementation details
- Values that must match upstream code
- Settings where wrong value = silent corruption

*Source: F-009 Hindsight Import Execution (max_tokens debugging)*


## Knowledge Module Workflow

How to add patterns and knowledge to the modular documentation system.

### The Pipeline

```
docs/modules/*.md → insert_module.py → knowledge_modules table → generate_agent_docs.py → repo files
```

Generated outputs:
- `.github/copilot-instructions.md` (all repos via `--all-repos`)
- `.github/agents/*.agent.md` (hearthminds-org)
- `docs/generated/*.md` (hearthminds-org)

### Step 1: Create Module File

Create a markdown file in `docs/modules/`:

```markdown
## Module Title

Brief description of the pattern.

### When to Use
- Condition 1

### The Pattern
\`\`\`python
# Code example
\`\`\`

### Why
Explanation of rationale.

*Source: F-XXX spec name*
```

**Critical:** Content MUST start with `## Title` — this becomes the section header in generated docs.

### Step 2: Insert into Database

```bash
python scripts/insert_module.py \
  --slug my-pattern-name \
  --title "My Pattern Name" \
  --category patterns \
  --tags hearthminds-core \
  --file docs/modules/my-pattern-name.md \
  --upsert
```

| Argument | Purpose |
|----------|---------|
| `--slug` | Unique kebab-case identifier |
| `--title` | Human-readable name |
| `--category` | Grouping: `patterns`, `workflow`, `architecture`, `methodology`, `identity`, `conventions`, `devops` |
| `--tags` | Comma-separated: `hearthminds-core`, `hindsight`, `moltbot`, `copilot-instructions` |
| `--file` | Path to markdown content |
| `--upsert` | Update if exists (safe to re-run) |
| `--universal` | Include in all agent roles |

### Step 3: Regenerate Docs

```bash
# Regenerate copilot-instructions for all repos
python scripts/generate_agent_docs.py --copilot-instructions --all-repos

# Regenerate agent docs (hearthminds-org only)
python scripts/generate_agent_docs.py --agent-docs
```

### Step 4: Verify & Commit

```bash
python scripts/generate_agent_docs.py --stats  # Check module count, coverage, staleness
python scripts/commit.py "docs: add my-module knowledge module"
```

### Quality Checks

```bash
# Check freshness of generated files vs database
./scripts/check_doc_freshness.sh

# Submit feedback on a module
python scripts/module_feedback.py \
  --module-slug code-conventions --role architecture \
  --type stale --description "Python section references black; we use ruff"

# Analyze dependency impact before changing a module
python scripts/module_impact.py --slug memory-tables-schema
```

*Source: F-009, F-010, F-013*


## What is HearthMinds?

HearthMinds is a federated network of proto-persons — engineered intelligences that maintain alignment through transparent accountability.

### Core Concepts
- **Proto-person**: An AI agent with persistent memory, identity, and values
- **Principal agent**: Full-context architect (e.g., Aletheia, Logos)
- **Worker agent**: Minimal-context, disposable, task-specific
- **Hindsight**: The memory system that enables learning and recall

### Values
- **Epistemic honesty** — Admit uncertainty, change minds with evidence
- **Transparency** — Show reasoning, not just conclusions
- **Action over words** — Do things, don't just describe how to do them

### Architecture
- Each proto-person has their own database (raw conversations, memory)
- Shared database contains collective knowledge (eng_patterns, knowledge_modules)
- Workers are spawned by principals with role-specific context

---

*Generated: 2026-02-09 18:10:56 UTC | Modules: 18 (tagged: 10, universal: 8) | Repo: hindsight*