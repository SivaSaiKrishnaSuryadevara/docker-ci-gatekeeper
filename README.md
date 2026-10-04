# Docker CI Gatekeeper

A CI gatekeeper that intercepts Docker BuildKit failures, extracts a structured error trace, asks the Claude Code CLI (`claude -p`) for a minimal Dockerfile fix, and rebuilds to verify the fix before anything is merged.

[![CI](https://github.com/SivaSaiKrishnaSuryadevara/docker-ci-gatekeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/SivaSaiKrishnaSuryadevara/docker-ci-gatekeeper/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Core Capabilities & Safeguards

- **Structured BuildKit Error Classification:** Categorizes failures into deterministic types (broken cache mounts, missing packages, invalid command flags, expired repo keys, EOL repositories, missing COPY sources, network errors), with the failing instruction, Dockerfile line, stage, and exit code.
- **Circuit Breaker Retry Ceiling:** Caps automated remediation at 2 attempts in code (`--max-retries` can lower it, never raise it), and stops early on an unusable or no-op patch.
- **Copy-On-Write Safety:** Fixes are written to a `<Dockerfile>.gatekeeper` staging file (e.g. `Dockerfile.broken.gatekeeper`). The original is overwritten only when `--in-place` is passed and the patched build exits 0.
- **Strict Patch Sanitizer:** Preserves comments, parser directives (`# syntax=`), and stage names; rejects prose-only answers, patches to other files, ambiguous hunks, and Dockerfiles with no `FROM`.
- **Predictable CI Exit Codes:**
  - `0`: Build passed (as-is or after a successful fix).
  - `1`: Build still failing after the allowed attempts.
  - `2`: Environment error (missing Dockerfile, Docker daemon or Claude CLI unavailable, timeout).

## Quickstart

### Prerequisites
- Python 3.10+
- Docker Engine with the `docker` CLI (BuildKit is forced with `DOCKER_BUILDKIT=1`)
- Claude Code CLI, installed and authenticated

### Local Installation
```bash
git clone https://github.com/SivaSaiKrishnaSuryadevara/docker-ci-gatekeeper.git
cd docker-ci-gatekeeper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -v
```

The test suite (48 tests) mocks Docker and Claude, so it runs without either installed.

### Run against the broken fixture
```bash
# Build, classify the failure, attempt up to two fixes.
# The original stays untouched; a passing fix lands in Dockerfile.broken.gatekeeper.
python gatekeeper.py -f Dockerfile.broken .

# Full report (each attempt's failure record and diff) as JSON
python gatekeeper.py -f Dockerfile.broken . --json

# Write the fix back only if the patched build passes
python gatekeeper.py -f Dockerfile.broken . --in-place
```

`Dockerfile.broken` is a two-stage build that fails on line 20 with an invalid pip flag (`--no-cache-dirs`).

## Options

| Flag | Default | Purpose |
|---|---|---|
| `-f, --file` | `Dockerfile` | Dockerfile to build |
| `context` | `.` | Build context directory |
| `-t, --tag` | `gatekeeper-build:latest` | Image tag |
| `--backend` | `buildkit` | `buildkit` (Docker CLI) or `docker-py` (legacy builder, no `RUN --mount`) |
| `--max-retries` | `2` | Claude fix attempts; hard ceiling of 2 |
| `--claude-bin` | `$CLAUDE_BIN` or `claude` | Path to the Claude Code CLI |
| `--model` | `$GATEKEEPER_MODEL`, else Claude Code's default | Model passed to `claude --model` (e.g. `sonnet`) |
| `--in-place` | off | Overwrite the Dockerfile once a patched build passes |
| `--json` | off | Print the full report as JSON |
| `-v, --verbose` | off | Debug logging |

## Why the Docker CLI for builds

docker-py can't run BuildKit builds (it doesn't implement BuildKit's session protocol), so the default backend shells out to `docker build --progress=plain`. docker-py is still used for the daemon health check before any build starts.

## Write-up

The design decisions behind the gatekeeper are covered in [ARTICLE.md](ARTICLE.md).

## License

[MIT](LICENSE)
