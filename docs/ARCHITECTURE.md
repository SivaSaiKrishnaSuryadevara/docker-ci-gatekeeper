# Architecture Reference

Technical reference for `gatekeeper.py`. Covers the run state machine, failure taxonomy, retry ceiling, patch sanitization, the staging-copy model, and the test suite.

## Components

| Component | Responsibility |
|---|---|
| `preflight_daemon()` | Pings the Docker daemon via docker-py (`docker.from_env().ping()`, 10 s timeout). Failure raises `GatekeeperError` (exit 2). |
| `BuildKitCliBackend` | Default build backend. Runs `docker build --progress=plain -f <file> -t <tag> <context>` with `DOCKER_BUILDKIT=1`; captures stdout + stderr (BuildKit progress is on stderr). 1800 s timeout. |
| `DockerPyBackend` | Optional backend (`--backend docker-py`). Uses `client.api.build(..., decode=True)`; legacy builder only, so `RUN --mount` and other BuildKit-only syntax fail. |
| `parse_buildkit_log()` / `parse_classic_log()` | Convert a failed build log into a `BuildFailure` record. |
| `build_prompt()` | Renders the `BuildFailure` and the numbered Dockerfile into a constrained fix request. |
| `ClaudeCli` | Runs `claude -p <prompt> --output-format text [--model <m>]` from an empty temporary directory. 300 s timeout. |
| `sanitize_patch()` | Converts the model response into a complete Dockerfile or raises `PatchError`. |
| `run_gatekeeper()` | Orchestrates the loop and returns a `GatekeeperReport`. |

All records (`BuildFailure`, `BuildResult`, `Attempt`, `GatekeeperReport`) are Pydantic models; `--json` emits the full report.

### Why builds use the Docker CLI

docker-py does not implement the BuildKit session protocol (gRPC over the Engine API), so `images.build()` / `api.build()` always use the legacy builder. BuildKit builds therefore go through the `docker` CLI. docker-py is used only for the daemon preflight and the optional legacy backend.

## State machine

```
            ┌──────────┐ daemon unreachable / missing input
  start ───►│ PREFLIGHT├──────────────────────────────────────► EXIT 2
            └────┬─────┘
                 ▼
            ┌──────────┐ exit 0
            │ BUILD #0 ├───────────────────────────────────────► PASSED (exit 0)
            └────┬─────┘
                 │ exit ≠ 0 → BuildFailure
                 ▼
     ┌──────►┌──────────┐ n > retries
     │       │ ATTEMPT n├──────────────────────────────────────► RETRIES_EXHAUSTED (exit 1)
     │       └────┬─────┘
     │            ▼
     │       ┌──────────┐ claude unavailable / non-zero / timeout
     │       │ PROPOSE  ├──────────────────────────────────────► EXIT 2
     │       └────┬─────┘
     │            ▼
     │       ┌──────────┐ PatchError
     │       │ SANITIZE ├──────────────────────────────────────► UNUSABLE_PATCH (exit 1)
     │       └────┬─────┘ identical to current
     │            ├────────────────────────────────────────────► NO_PROGRESS (exit 1)
     │            ▼
     │       ┌──────────┐
     │       │  STAGE   │ write <Dockerfile>.gatekeeper
     │       └────┬─────┘
     │            ▼
     │       ┌──────────┐ exit 0
     │       │ REBUILD  ├──────────────────────────────────────► PASSED (exit 0)
     │       └────┬─────┘                                         └─ --in-place: write back
     └────────────┘ exit ≠ 0 (n += 1)
```

`stop_reason` values in the report: `passed`, `retries_exhausted`, `unusable_patch: <reason>`, `no_progress`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Final build passed (original or patched). |
| `1` | Build still failing: retries exhausted, unusable patch, or no progress. |
| `2` | Environment or input error (`GatekeeperError`): Dockerfile/context missing, daemon unreachable, `docker` or `claude` not found, `claude -p` non-zero exit, timeout. Never retried. |

## Failure taxonomy

`parse_buildkit_log()` groups output lines by BuildKit step ID (`#<id>`), identifies the failing step from the first `#<id> ERROR:` line, and reads the stage and position from its header (`#<id> [<stage> <n>/<m>] <instruction>`). Only the failing step's output is retained (last 40 lines).

The Dockerfile line number is taken from BuildKit's `<file>:<line>` snippet block when present. When absent (observed on Docker 29.5.2 / buildx 0.37.2), the failing instruction is located in the Dockerfile by an indexer that joins `\` continuations and tracks `FROM ... AS <stage>` boundaries. The final-error line is matched by `^ERROR: (?:failed to build: )?failed to solve`, covering both buildx formats.

Categories are assigned by ordered regex; first match wins:

| Order | Category | Representative match |
|---|---|---|
| 1 | `cache_mount` | `unknown mount type`, `--mount option requires BuildKit`, `failed to … cache` |
| 2 | `dockerfile_syntax` | `dockerfile parse error`, `unknown instruction` |
| 3 | `missing_build_context_file` | `failed to compute cache key`, `failed to calculate checksum`, `COPY failed` |
| 4 | `package_repo_signature` | `NO_PUBKEY`, `is not signed`, `GPG error` |
| 5 | `package_repo_unavailable` | `does not have a Release file` |
| 6 | `missing_package` | `Unable to locate package`, `No matching distribution found` |
| 7 | `invalid_command_flag` | `no such option:`, `unrecognized arguments` |
| 8 | `network` | `Temporary failure resolving`, `Could not resolve host` |
| 9 | `command_failed` | `did not complete successfully`, `exit code: N` |
| — | `unknown` | no match |

Ordering rules: `cache_mount` precedes `dockerfile_syntax` because a malformed mount surfaces as a parse error; `command_failed` is last because nearly every failed `RUN` emits it. The `message` field is the first output line matching a non-generic category, falling back to the error line. ANSI escapes and carriage returns are stripped before matching.

## Retry ceiling

- `MAX_RETRIES = 2` is a module constant. `--max-retries` above it is clamped with a warning; `0` disables model calls.
- Worst case per run: 3 builds, 2 Claude calls.
- Early termination without spending the remaining budget: `unusable_patch` (sanitizer rejection) and `no_progress` (patch identical to current file).

## Patch sanitization

Input: raw model stdout. Output: complete Dockerfile text or `PatchError`.

1. Reject responses over `MAX_RESPONSE_BYTES` (200,000).
2. Strip ANSI escapes; normalize CRLF/CR to LF.
3. Prefer a unified diff (` ```diff `/` ```patch ` fence, or bare `---`/`+++`/`@@`). Headers naming any file other than the target Dockerfile are rejected.
4. Otherwise accept exactly one Dockerfile code block (` ```dockerfile `/` ```docker `, or a fence whose first instruction is `FROM`/`ARG`). Multiple candidate blocks are rejected.
5. Restore leading parser directives (`# syntax=`, `# escape=`, `# check=`) if the response dropped them.
6. Require at least one `FROM` instruction.

Content inside the diff or fence is used verbatim; `#` lines are never stripped. Comments present in the original but absent from the result are listed in `GatekeeperReport.dropped_comments`.

Hunk application (`apply_unified_diff`) treats the hunk header as a hint: it tries the stated offset, then searches for the hunk's exact old lines and applies only on a single unique match. Absent or ambiguous matches raise `PatchError`.

Processing is line-based; no AST is built.

## Staging-copy model

- The original Dockerfile is read once and never modified during the loop.
- Each candidate is written to `<Dockerfile>.gatekeeper` (e.g. `Dockerfile.broken.gatekeeper`), and rebuilds use that file.
- Write-back occurs only when all hold: final build passed, content changed, and `--in-place` was given. The staging file is then removed.
- Without `--in-place`, a passing fix remains in the staging file for review; `*.gatekeeper` is git-ignored.
- Each `Attempt` carries the unified diff of that iteration (`patch_diff`).

Known limitation: a per-Dockerfile ignore file (`<Dockerfile>.dockerignore`) does not apply to the staging copy because its name differs. A context-root `.dockerignore` applies normally.

## Claude CLI invocation

- Command: `claude -p <prompt> --output-format text`, plus `--model <m>` when `--model` or `GATEKEEPER_MODEL` is set.
- Binary: `--claude-bin` or `CLAUDE_BIN` (default `claude`).
- Working directory: a fresh empty temp directory; the CLI has no repository access. All file writes are performed by the gatekeeper after sanitization.
- Error reporting: stderr, falling back to stdout when stderr is empty (print mode reports usage-limit and auth errors on stdout).

## Verified execution (Colima)

Environment: Apple Silicon, Colima (vz, 4 CPU, 4 GiB), Docker 29.5.2, buildx 0.37.2.

```bash
brew install colima docker docker-buildx
mkdir -p ~/.docker/cli-plugins
ln -sfn "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx
colima start --cpu 4 --memory 4

python gatekeeper.py -f Dockerfile.broken . --claude-bin "$(command -v claude)" --model sonnet
```

Observed result:

```text
INFO Build failed (invalid_command_flag at line 20); asking Claude for a fix (1/2)
patched Dockerfile written to Dockerfile.broken.gatekeeper
attempt 0: FAIL [invalid_command_flag] line 20: no such option: --no-cache-dirs
attempt 1: PASS
result: PASS (passed, 1 Claude call(s))
```

Exit code 0; one-line change on line 20 (`--no-cache-dirs` → `--no-cache-dir`); all comments and the `# syntax=` directive preserved; resulting image runs. Without `--model`, the same run exited 2 because the CLI's default model required usage credits in print mode.

## Test suite

50 tests, no Docker or Claude required (backends and CLI are faked; `subprocess.run` is mocked where invoked). CI runs the suite on Python 3.10, 3.11, 3.12, and 3.13.

| Class | Tests | Scope |
|---|---|---|
| `TestBuildKitParser` | 19 | Category assignment and ordering, line/stage/step extraction, snippet fallback, ANSI/CRLF tolerance, legacy-builder logs, a captured Docker 29 log (`tests/fixtures/buildkit_invalid_flag_docker29.log`), both final-error formats |
| `TestPatchSanitization` | 16 | Fenced/bare diffs, offset tolerance, full-file responses, directive restoration, dropped-comment reporting, seven rejection classes, staging and `--in-place` behavior |
| `TestRetryCap` | 10 | Two-call ceiling, clamping, zero retries, early stops, prompt content, exit codes 1 and 2 |
| `TestClaudeCli` | 5 | Print-mode invocation in a scratch directory, missing binary, non-zero exit, stdout-only errors, `--model` pass-through |

```bash
pytest tests/ -v
```
