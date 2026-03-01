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

*Generated: 2026-03-01 19:01:03 UTC | Modules: 8 (tagged: 3, universal: 5) | Repo: hindsight*