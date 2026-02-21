---
applyTo: '**'
---

# Hindsight — Development Guidelines

### Architecture & Design

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


## raw_conversation Schema

The immutable source of truth for conversation history.

**Tags:** `hindsight`

### Location

Database `aletheia_source` on the Hindsight-aletheia embedded PostgreSQL (port 5433). A corresponding `logos_source` database will exist on Hindsight-logos (port 5432). See `infra_scripts/config/ports.env` for current port assignments.

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


### Patterns & Practices

## FastAPI Sync vs Async Route Handlers

Don't use `async def` for route handlers that call `subprocess.run()` or other blocking I/O.

**Tags:** `hindsight`, `hearthminds-core`

### The Problem

```python
# ✗ Anti-pattern: async handler + blocking subprocess
@app.post("/api/service/{name}/restart")
async def restart_service(name: str):
    result = subprocess.run(["podman", "restart", name], capture_output=True)
    return {"status": "restarted"}
```

FastAPI treats `async def` as "already async" and executes it **directly on the
asyncio event loop**. `subprocess.run()` blocks the thread. Result: the entire
server freezes until the subprocess completes. All concurrent requests queue behind
it. Buttons appear to do nothing because HTTP responses never come back.

### The Pattern

```python
# ✓ Pattern: plain def — FastAPI auto-dispatches to threadpool
@app.post("/api/service/{name}/restart")
def restart_service(name: str):
    result = subprocess.run(["podman", "restart", name], capture_output=True)
    return {"status": "restarted"}
```

When a route handler is plain `def` (not `async def`), FastAPI automatically runs
it in a thread pool via `run_in_executor`. Blocking calls work correctly without
stalling the event loop.

### When to Use `async def`

Only when the handler body uses `await` on truly async operations:

```python
# ✓ Correct use of async def — all I/O is awaited
@app.get("/api/data")
async def get_data():
    async with httpx.AsyncClient() as client:
        response = await client.get("http://upstream/data")
    return response.json()
```

### The Rule

| Handler body contains... | Use |
|--------------------------|-----|
| `subprocess.run()`, `time.sleep()`, file I/O | `def` (plain) |
| `await` on async libraries (httpx, aiohttp, asyncpg) | `async def` |
| No I/O (pure computation, dict lookup) | Either works |

### Why This Works

FastAPI's architecture:
- `async def` → runs on the main asyncio event loop thread
- `def` → runs in `asyncio.get_event_loop().run_in_executor(None, handler)`

The threadpool approach gives you concurrent request handling without writing any
async code. This is the correct default for control plane dashboards that shell out
to `podman`, `nvidia-smi`, `pg_dump`, etc.

*Source: F-015 Infrastructure Control Plane (Dashboard fix — buttons not responding)*


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


### DevOps & Infrastructure

## FP8 KV-Cache Quantization

Enable FP8 KV-cache in vLLM to double effective context window.

**Tags:** `infrastructure`, `vllm`, `hindsight`

### The Problem

vLLM's default FP16 KV-cache uses 2 bytes per element, limiting context window to ~54k tokens on our GPU.

### The Solution

FP8 KV-cache uses 1 byte per element, roughly doubling available context:

| Metric | FP16 (default) | FP8 |
|--------|----------------|-----|
| Context window | ~54,000 | ~108,000 |
| Memory per token | 2 bytes | 1 byte |
| Quality impact | Baseline | Negligible for extraction |

### Prerequisites: CUDA Toolkit

FP8 requires `nvcc` to compile custom CUDA kernels at runtime. This is **separate from the NVIDIA driver**.

```bash
# Error without toolkit:
# "Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist"

# Solution (Fedora/Nobara):
sudo dnf install cuda-nvcc cuda-devel cuda-cudart-devel
```

### Configuration

Set environment variables before starting vLLM:

```bash
export HEARTHMINDS_MAX_MODEL_LEN=108000
export HEARTHMINDS_KV_CACHE_DTYPE=fp8
python -m hearthminds_ctl start --vllm
```

### When to Use FP8

- **Good for**: Extraction, summarization, RAG — tasks where slight precision loss doesn't matter
- **Caution for**: Creative generation, math — tasks sensitive to precision

For Hindsight's extraction pipeline, FP8 quality is equivalent to FP16.

### Verification

```bash
# Monitor GPU memory during startup
nvidia-smi -l 1

# If OOM, try lower context:
export HEARTHMINDS_MAX_MODEL_LEN=80000  # or 65000
```

*Source: F-010 Infrastructure Hardening*


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



### Workflow & Governance

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


### Conventions

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

### Committing (hearthminds-org)

**Use `scripts/commit.py` instead of raw `git commit`.** The script auto-detects staged
spec files and:
- Prefixes commit messages with the spec ID (e.g., `F-019: description`)
- Updates the backlog registry with completion date and commit hash
- Can archive completed specs with `--archive`

```bash
# Preferred: auto-detects spec from staged files
python scripts/commit.py "description of change"

# Explicit spec
python scripts/commit.py "description" --spec F-019

# Non-spec commit
python scripts/commit.py "fix typo" --no-spec
```

**Anti-pattern (F-019):** Using `git commit -m` directly when spec files are staged
causes the registry update to be missed, requiring a manual follow-up commit for the
hash. The script does this atomically.

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

## Skill Authoring Guidelines

Guidelines for creating VS Code Agent Skills from knowledge modules.

**Tags:** `hearthminds-core`

### When to Create a Skill

| Content Type | Output | Rationale |
|-------------|--------|-----------|
| Identity, values, mission | Agent doc (inline) | Must always be in context |
| Conventions (code style, git) | Agent doc (inline) | Always-on guardrails |
| Principles (TDD, fail-hard) | Agent doc (inline) | Shape every decision |
| Architecture overview | Agent doc (inline) | Foundational context |
| Procedural workflows | **Skill** | Only needed when doing that task |
| Checklists & step-by-step | **Skill** | Only needed when doing that task |
| Migration templates | **Skill** | Only needed when writing migrations |
| Config flag workflows | **Skill** | Only needed when adding config |

**Rule of thumb:** If content is only needed when performing a specific task, it should be a skill.

### SKILL.md Format

Skills live at `.github/skills/{name}/SKILL.md`:

```yaml
---
name: my-skill-name
description: "One-line summary of when to use this skill. Quoted string, max 1024 chars."
---
```

Body is Markdown — the procedure, steps, examples, etc.

### Critical Format Rules

- `name` must match the parent directory name exactly (kebab-case)
- `description` **must** be a quoted single-line string
- **Never** use YAML block scalars (`>-`, `>`, `|`, `|-`) for description — VS Code reads the literal scalar indicator text instead of the folded content
- Content should be under ~5000 tokens for efficient loading
- Skills are globally available (not scoped to a specific agent mode)

### Database Integration

Skills are tracked in `knowledge_modules` with `output_type = 'skill'`:
- The DB tracks which modules are skills (for exclusion from agent.md)
- SKILL.md files are authored directly, not generated from DB content
- Use `insert_module.py --output-type skill` when inserting
- The `role_context` view automatically excludes skill-type modules

### Progressive Loading Levels

1. **Discovery** — Only `name` + `description` from YAML frontmatter (~2 lines)
2. **Instructions** — Full SKILL.md body loaded when relevant to the task
3. **Resources** — Additional files in the skill directory loaded on reference

The `description` field is critical — it's how VS Code decides whether to load the skill. Write descriptions that clearly state **when** to use it, not just **what** it does.

*Source: F-019 Agent Context Ordering (Phase 5)*


### Testing & Methodology

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

### Red Phase Discipline

**Complete the full Red phase before moving to Green.** This is non-negotiable.

When writing tests hits friction — mocking complexity, unclear interfaces, tests
that won't fail the right way — that friction is design feedback. The Red phase
is where architectural problems surface cheaply.

**The rule:** All planned tests must be written and confirmed failing (for the
right reason) before writing any production code. If a test can't be written cleanly,
that's a signal to reconsider the interface, not a reason to skip ahead.

**Anti-pattern observed (F-015):** After several tests failed to achieve clean Red
phase due to mock complexity, the implementing agent attempted to skip remaining
tests and begin writing production code. Working through the full Red phase instead
caused a reevaluation of the approach, saving significant wasted effort. The tests
that were hardest to write revealed the design problems.

**When Red phase is difficult:**
1. Stop and examine **why** the test is hard to write
2. Consider if the interface is wrong (too coupled, too complex, wrong abstraction)
3. Ask for clarification from the architecture agent if the design feels off
4. **Never** start Green phase with un-written Red tests — this erases the primary
   benefit of TDD (design feedback before commitment)

The hard Red tests are the valuable ones. If all tests are trivial to write, you
probably aren't testing the interesting behavior.

### When Tests Fail
A failing test is information. Before "fixing" it:
1. Understand WHY it fails
2. Determine if the test or the code is wrong
3. Fix the root cause, not the symptom

### Skip Markers Over Test Deletion

When tests fail due to known, temporary conditions (decommed scripts, missing
infrastructure, English-only LLM), use `pytest.mark.skip(reason="...")` with a
reason referencing the relevant spec or future work. This preserves intent and
enables `grep skip` to audit what's deferred.

- Module-level `pytestmark = pytest.mark.skip(reason="...")` for entire files
- Per-test `@pytest.mark.skip(reason="...")` for individual cases
- Always include a reason string — "why is this skipped" is as important as "why did it fail"

**Anti-pattern:** Deleting failing tests removes the specification of expected
behavior. When the underlying issue is resolved, nobody remembers to re-implement
the test. Skip markers are a promise to return; deletion is forgetting.

*Source: F-016 Pre-Import Security Hardening (Phase 4 pre-existing failure triage)*

### Regression Baseline Tracking

Document exact test counts at each phase of multi-phase work:

```
Phase 2: 494 passed, 1 failed (pre-existing), 33 skipped
Phase 3: 494 passed, 1 failed (pre-existing), 33 skipped
Phase 4: 505 passed, 0 failed, 33 skipped (upstream)
         334 passed, 7 failed (pre-existing), 0 skipped (infra)
```

This makes regressions immediately visible — new failures stand out against the
established baseline. Cheap to record, saves significant triage time.

*Source: F-016 Pre-Import Security Hardening (Phases 1–8)*

### Cast LLM Tool Parameters at the Boundary

LLMs are unreliable type-coercers. Always cast tool call arguments to expected
types immediately upon receipt:

```python
# ✗ Anti-pattern: trust LLM to send correct type
max_tokens = args.get("max_tokens")  # Could be str "2048" or int 2048

# ✓ Pattern: cast at the boundary
max_tokens = int(args.get("max_tokens") or 2048)
```

*Source: F-016 Pre-Import Security Hardening (Phase 3 reflect agent fix)*

*Source: F-015 Infrastructure Control Plane (Red phase discipline observation)*


### Patterns & Practices

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


---

*Generated: 2026-02-17 21:34:40 UTC | Modules: 16 (tagged: 9, universal: 7) | Repo: hindsight*