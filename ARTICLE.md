# Building a Self-Healing Docker CI/CD Gatekeeper with Claude Code and the Docker SDK

> **Source code:** [github.com/SivaSaiKrishnaSuryadevara/docker-ci-gatekeeper](https://github.com/SivaSaiKrishnaSuryadevara/docker-ci-gatekeeper)
> One Python file (`gatekeeper.py`), one deliberately broken Dockerfile, and 50 pytest tests that run on Python 3.10 through 3.13 in CI.

Here is the build failure I designed this project around. It's a two-stage Python image. The `builder` stage compiles wheels, and the `runtime` stage installs them as a non-root user. Line 20 of the Dockerfile reads:

```dockerfile
RUN pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3
```

The flag is `--no-cache-dir`. One extra `s` and pip exits with code 2. BuildKit marks step `[builder 4/4]` as failed, the runtime stage never starts, and the pipeline goes red. The engineer who pushed it now has to open the job, scroll past base-image pulls and cache hits, find the one line that matters, fix it, push again, and wait for the whole build to queue and run from the top.

None of that work requires judgment. The log already contains the answer: `no such option: --no-cache-dirs`. What's missing is something that reads the log the way a person would, isolates the failing instruction, proposes the smallest possible change, and proves that change works by rebuilding. That's what this gatekeeper does. It wraps `docker build`, turns a failed build into a structured failure record, sends that record to Claude Code in non-interactive mode (`claude -p`), applies the returned patch to a staging copy of the Dockerfile, and rebuilds. It gives up after two attempts.

This article walks through the design decisions in the actual code, including the places where the obvious approach was wrong.

## The loop, end to end

```
                 ┌────────────────────────────┐
                 │ preflight_daemon()          │  docker-py: from_env().ping()
                 │ daemon down → exit 2        │
                 └─────────────┬──────────────┘
                               ▼
┌──────────────────────────────────────────────────────────┐
│ backend.build(context, Dockerfile, tag)       attempt 0  │
│   BuildKitCliBackend: docker build --progress=plain      │
└─────────────┬────────────────────────────────────────────┘
              │ exit 0 ──────────────────────────────────────► PASS (exit 0)
              │ exit ≠ 0
              ▼
   parse_buildkit_log() → BuildFailure
     category, instruction, dockerfile_line, stage, step,
     exit_code, snippet, step_output
              │
              ▼
   build_prompt() → ClaudeCli.propose_fix()      claude -p "<prompt>"
              │                                  (cwd = empty temp dir)
              ▼
   sanitize_patch()  ── PatchError ─────────────► STOP: unusable_patch
              │      ── identical to current ───► STOP: no_progress
              ▼
   write Dockerfile.broken.gatekeeper  (staging copy)
              │
              ▼
   backend.build(context, staging copy)         attempt n (n ≤ 2)
              │ pass → PASS (optionally --in-place write-back)
              │ fail → loop, or STOP: retries_exhausted (exit 1)
```

Every box in that diagram is a function or class in `gatekeeper.py`. The data passed between them is a set of Pydantic models (`BuildFailure`, `BuildResult`, `Attempt`, `GatekeeperReport`), which means the whole run can be dumped as JSON with `--json` and archived as a CI artifact.

## Decision 1: BuildKit runs through the Docker CLI, not docker-py

My first plan was to do everything through the Docker SDK for Python. `docker.from_env().images.build(...)` takes a path and a Dockerfile, streams progress, and raises `BuildError` on failure. It looks like exactly the right tool.

It isn't, because docker-py doesn't build with BuildKit. The Engine API's `/build` endpoint only runs BuildKit when the client also opens a session to it over gRPC. The session is how the daemon pulls files from the build context, forwards SSH agents, and serves secrets. The Docker CLI implements that session protocol. docker-py does not, so its `build()` call always uses the legacy builder.

That matters for this project specifically, because the fixture's builder stage uses a cache mount:

```dockerfile
# Keep the pip cache in a BuildKit cache mount so rebuilds stay fast.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip wheel
```

The legacy builder rejects `RUN --mount` outright. If the gatekeeper used docker-py to build, it would report a "failure" that has nothing to do with the real bug, and Claude would be asked to remove a perfectly good cache mount. The fix would make the pipeline green and the builds slower, which is worse than useless.

So the default backend calls the CLI directly and forces BuildKit with plain-text progress:

```python
class BuildKitCliBackend:
    def build(self, context: Path, dockerfile: Path, tag: str) -> BuildResult:
        if shutil.which(self.docker_bin) is None:
            raise GatekeeperError(f"'{self.docker_bin}' CLI not found; install Docker or use --backend docker-py")
        cmd = [self.docker_bin, "build", "--progress=plain", "-f", str(dockerfile), "-t", tag, str(context)]
        env = {**os.environ, "DOCKER_BUILDKIT": "1"}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise GatekeeperError(f"docker build timed out after {self.timeout}s") from exc
        output = (proc.stdout or "") + (proc.stderr or "")  # BuildKit progress goes to stderr
        ...
```

Two details are easy to get wrong here. `--progress=plain` matters because the default TTY progress renderer redraws lines in place, and a captured log of it is unreadable. And BuildKit writes its progress to stderr, not stdout, so capturing only stdout gives you an empty log on a failed build.

docker-py still has a job. Before any build starts, it answers a cheaper question: is the daemon there at all?

```python
def preflight_daemon(timeout: int = 10) -> None:
    import docker
    from docker.errors import DockerException

    try:
        docker.from_env(timeout=timeout).ping()
    except DockerException as exc:
        raise GatekeeperError(f"Docker daemon unreachable: {exc}") from exc
```

A missing daemon is an environment problem, not a Dockerfile problem, and it must never reach Claude. `GatekeeperError` maps to exit code 2, which a CI system can treat differently from exit code 1 ("the build is broken and couldn't be fixed"). This is the actual output on a machine with no Docker installed:

```text
$ python gatekeeper.py -f Dockerfile.broken .
ERROR Docker daemon unreachable: Error while fetching server API version: ('Connection aborted.', FileNotFoundError(2, 'No such file or directory'))
$ echo $?
2
```

The docker-py build path does still exist as `--backend docker-py`, for runners that have a daemon socket but no CLI. It uses the low-level `client.api.build(..., decode=True)` stream and a separate parser (`parse_classic_log`) for the legacy builder's `Step 5/9 : ...` format. I'd only use it for Dockerfiles that don't use BuildKit-only syntax.

## Decision 2: Turn the log into a typed failure before anyone reads it

A raw build log is the wrong thing to send to a language model. It's long, most of it is irrelevant, and the relevant part is spread across several places. This is the real `docker build --progress=plain` output for the line-20 failure, captured on Docker 29.5.2 with buildx 0.37.2 under Colima (trimmed to the last cached step and the failure):

```text
#8 [builder 3/4] RUN --mount=type=cache,target=/root/.cache/pip     pip install --upgrade pip wheel
#8 CACHED

#9 [builder 4/4] RUN pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3
#9 0.146 
#9 0.146 Usage:   
#9 0.146   pip wheel [options] <requirement specifier> ...
#9 0.146   pip wheel [options] -r <requirements file> ...
#9 0.146   pip wheel [options] [-e] <vcs project url> ...
#9 0.146   pip wheel [options] [-e] <local project path> ...
#9 0.146   pip wheel [options] <archive url/path> ...
#9 0.146 
#9 0.146 no such option: --no-cache-dirs
#9 ERROR: process "/bin/sh -c pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3" did not complete successfully: exit code: 2
------
 > [builder 4/4] RUN pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3:
0.146 no such option: --no-cache-dirs
------
ERROR: failed to build: failed to solve: process "/bin/sh -c pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3" did not complete successfully: exit code: 2
```

Every BuildKit step has a numeric ID, and every line it prints is prefixed with `#<id>`. The step header carries the stage name and position (`[builder 4/4]`). The first `#<id> ERROR:` line identifies which step failed. `parse_buildkit_log` keys everything off those two patterns:

```python
STEP_HEADER_RE = re.compile(r"^#(?P<id>\d+) \[(?:(?P<stage>[^\s\]]+) )?(?P<pos>\d+/\d+)\] (?P<instr>.+)$")
STEP_LINE_RE = re.compile(r"^#(?P<id>\d+) (?:\d+\.\d+(?: |$))?(?P<text>.*)$")
```

Output is grouped by step ID, so the `CACHED` lines from step 8 and the context-transfer noise from steps 1 to 5 never end up in the failure record. Only step 9's output does.

The real log taught me two things the fixture I wrote from memory had wrong. Some BuildKit versions follow the failure with a `Dockerfile:<line>` header and a source snippet that marks the failing line with `>>>`. This Docker version printed no snippet at all. And the final line now starts with `ERROR: failed to build: failed to solve:` rather than `ERROR: failed to solve:`. The parser handles both. When a snippet is present, it reads the line number from it. When it isn't, it finds the failing instruction's text in the Dockerfile with a small indexer that joins `\` continuation lines and tracks which `FROM ... AS <stage>` each instruction belongs to. The final-line check accepts either prefix:

```python
FINAL_ERROR_RE = re.compile(r"^ERROR: (?:failed to build: )?failed to solve")
```

The captured log is checked into `tests/fixtures/` and parsed by `test_real_docker29_log_without_snippet_block`, next to the older snippet-format fixture, so both formats stay covered.

For both log formats the result is the same record. These are the values the tests assert:

```python
BuildFailure(
    category=FailureCategory.INVALID_FLAG,
    message="no such option: --no-cache-dirs",
    instruction="RUN pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3",
    dockerfile_line=20,
    stage="builder",
    step="4/4",
    exit_code=2,
    ...
)
```

### The taxonomy, and why order matters

`category` comes from an ordered list of regexes. The first match wins:

```python
CATEGORY_PATTERNS: list[tuple[FailureCategory, re.Pattern[str]]] = [
    (FailureCategory.CACHE_MOUNT, re.compile(
        r"--mount option requires BuildKit|invalid mount config|failed to (?:create|prepare|mount) .*cache"
        r"|cache mount|unknown mount (?:type|option)|mount type=cache", re.IGNORECASE)),
    (FailureCategory.SYNTAX, re.compile(
        r"unknown instruction|dockerfile parse error|failed to parse dockerfile|unexpected end of statement", re.IGNORECASE)),
    (FailureCategory.MISSING_FILE, re.compile(
        r"failed to compute cache key|failed to calculate checksum|COPY failed|ADD failed", re.IGNORECASE)),
    (FailureCategory.REPO_SIGNATURE, ...),   # NO_PUBKEY, "is not signed", GPG error
    (FailureCategory.REPO_UNAVAILABLE, ...), # "does not have a Release file" (EOL Debian suites)
    (FailureCategory.MISSING_PACKAGE, ...),  # apt "Unable to locate package", pip "No matching distribution"
    (FailureCategory.INVALID_FLAG, ...),     # "no such option:", "unrecognized arguments"
    (FailureCategory.NETWORK, ...),          # DNS and TLS timeouts
    (FailureCategory.COMMAND_FAILED, re.compile(
        r"did not complete successfully|returned a non-zero code|exit code: \d+", re.IGNORECASE)),
]
```

The order encodes which explanation is most specific. Two cases show why:

- A misspelled cache mount type (`--mount=type=cahce`) makes BuildKit fail with `dockerfile parse error on line 2: unknown mount type "cahce"`. That line matches both `CACHE_MOUNT` and `SYNTAX`. "Syntax error" is true but tells the fixer nothing. "Cache mount" points it at the right flag. So `CACHE_MOUNT` is checked first, and `test_broken_cache_mount_parse_error` pins that order.
- Nearly every failed `RUN` produces `did not complete successfully: exit code: N`. If `COMMAND_FAILED` came early, every apt and pip failure would collapse into it. It's last on purpose: it means "something in a RUN failed, and I can't say what."

The `message` field gets the same treatment. `_best_message` walks the step's output and returns the first line that matches a *specific* category, skipping lines that match only the generic one. For an apt failure, that means the record says `E: Unable to locate package libpq-devv`, not `exit code: 100`.

The parser also strips ANSI color codes and carriage returns first, because CI runners love to inject both. An unrecognized log produces `UNKNOWN` instead of an exception: a parser crash would take down the gatekeeper on exactly the builds it's meant to help with.

## Decision 3: Hand Claude a narrow, structured prompt

The structured record is what makes the prompt small. `build_prompt` sends the category, stage, failing instruction and line, exit code, key error, the last 25 lines of that step's output, and the current Dockerfile with line numbers. The rules section is short and specific:

```text
Rules:
- Change only what is needed to fix this failure. Do not restructure stages or upgrade unrelated things.
- Keep every comment and parser directive (lines starting with "# syntax=", "# escape=", "# check=").
- Respond with EITHER a unified diff against Dockerfile.broken (--- a/Dockerfile.broken / +++ b/Dockerfile.broken)
  OR the complete corrected file in a single ```dockerfile code block. No other files.
- Line numbers above are for reference only; do not include them in your answer.
```

Claude Code is invoked in print mode, from an empty temporary directory:

```python
with tempfile.TemporaryDirectory(prefix="gatekeeper-claude-") as scratch:
    proc = subprocess.run(
        [self.claude_bin, "-p", prompt, "--output-format", "text"],
        capture_output=True, text=True, timeout=self.timeout, cwd=scratch,
    )
```

The empty working directory is a deliberate containment choice. In `-p` mode nobody is present to approve file edits, and I don't want Claude editing the repository directly anyway. The gatekeeper owns every write. Running from a scratch directory means the model's only output channel is stdout, which then goes through the sanitizer. A non-zero exit or a timeout raises `GatekeeperError` (exit 2), the same as a missing daemon.

The binary path is configurable with `--claude-bin` or the `CLAUDE_BIN` environment variable. That matters on machines where Claude Code is installed somewhere other than `PATH`, such as inside an editor extension. On a CI runner, Claude Code must be installed and authenticated before the gatekeeper can call it.

The model is configurable too, with `--model` or `GATEKEEPER_MODEL`, and I added that only after my first real run failed. My Claude Code default model was one that print mode refused without extra usage credits. `claude -p` exited 1 with an empty stderr, so the gatekeeper's error read `claude -p exited 1:` and nothing else. The explanation was on stdout:

```text
Fable 5.1 requires usage credits. Switch to another model, or manage usage credits at claude.ai/settings/usage?from=cc_cli_limit_message, to continue.
```

Two fixes came out of that. The error path now falls back to stdout when stderr is empty, and `--model` is passed straight through to `claude --model`:

```python
cmd = [self.claude_bin, "-p", prompt, "--output-format", "text"]
if self.model:
    cmd += ["--model", self.model]
...
if proc.returncode != 0:
    # Print mode reports some errors (auth, usage limits) on stdout, not stderr.
    detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    raise GatekeeperError(f"claude -p exited {proc.returncode}: {detail[:500]}")
```

Pinning the model in CI is a good idea regardless. The interactive default on a developer's machine is whatever they picked last, and a pipeline shouldn't inherit that.

## Decision 4: Sanitize the patch without destroying the Dockerfile

Model output is untrusted input. It may be a diff, a whole file, prose with a code block in the middle, or a confident explanation with no code at all. `sanitize_patch` turns all of those into either a complete Dockerfile or a `PatchError`.

The order of operations:

1. **Reject anything over 200 KB** (`MAX_RESPONSE_BYTES`).
2. **Strip ANSI codes and normalize line endings.**
3. **Look for a diff first**, either in a ```` ```diff ```` fence or bare in the response. Diff headers are checked so a patch to `.github/workflows/ci.yml` (or any other file) is rejected. The hunks are applied by `apply_unified_diff`.
4. **Otherwise look for exactly one Dockerfile code block.** Two blocks means the model is hedging, and the sanitizer refuses to pick one.
5. **Restore parser directives, then require a `FROM`.**

### Why "keep the comments" isn't a formatting nicety

The obvious way to clean a model response is to strip markdown: drop headings, drop the prose. In a Dockerfile, though, every `#` line looks like a markdown heading. A cleaner that strips `# ...` lines would delete every comment in the file, and one of those "comments" isn't a comment:

```dockerfile
# syntax=docker/dockerfile:1.7
```

That's a BuildKit parser directive. It selects the Dockerfile frontend version and has to appear before any other line. Remove it and the build falls back to the daemon's built-in frontend, which can change how newer syntax like `RUN --mount` is parsed. A "fix" that silently drops it changes the build in a way no reviewer will see in the diff.

So the sanitizer never edits inside the code it extracts. Everything outside the fence or diff is discarded. Everything inside is kept verbatim. Then, because models sometimes drop the first line of a file they reprint, the directives are put back explicitly:

```python
def _restore_parser_directives(original: str, patched: str) -> str:
    directives = []
    for ln in original.splitlines():
        if PARSER_DIRECTIVE_RE.match(ln):
            directives.append(ln)
        else:
            break  # directives are only valid before the first non-directive line
    missing = [d for d in directives if d not in patched.splitlines()[: len(directives) + 1]]
    return ("\n".join(missing) + "\n" + patched) if missing else patched
```

Stage names (`AS builder`, `AS runtime`) and ordinary comments survive because nothing in the pipeline rewrites them. If a model's full-file response *does* drop a comment, `dropped_comments()` lists it in the report and on the console as a warning, so a reviewer finds out.

This is line-level handling, not an AST. The gatekeeper doesn't parse the Dockerfile into a syntax tree. It indexes instructions by line, and that's enough for the guarantees it makes: directives first, comments untouched, at least one `FROM`.

### Applying diffs strictly

Models write unified diffs with wrong line numbers more often than they write wrong content. `apply_unified_diff` uses the hunk header as a hint, not an address. It tries the stated position first, then searches the whole file for the hunk's exact old lines. It applies the hunk only if there is exactly one match:

```python
def _find_block(lines: list[str], block: list[str], expected: int) -> int | None:
    if not block:
        return expected
    n = len(block)
    if lines[expected:expected + n] == block:
        return expected
    matches = [p for p in range(len(lines) - n + 1) if lines[p:p + n] == block]
    return matches[0] if len(matches) == 1 else None  # ambiguous or absent: refuse
```

Zero matches means the diff was written against a file that doesn't exist. Two or more means the hunk is ambiguous: a Dockerfile with two identical `RUN apt-get update` lines in two stages is a realistic example. In both cases the gatekeeper stops instead of guessing.

## Decision 5: A hard cap of two attempts

Each loop iteration costs a model call plus a full image build. On a multi-stage image with a cold cache, the build is usually the bigger cost. A self-healing loop without a ceiling is a cost incident waiting for the right bad Dockerfile: a fix that moves the failure from step 4 to step 5, then back.

The cap is enforced in code, not configuration:

```python
MAX_RETRIES = 2  # hard ceiling on Claude calls per run — not overridable upward

def run_gatekeeper(..., max_retries: int = MAX_RETRIES, ...):
    if max_retries > MAX_RETRIES:
        log.warning("max_retries=%d exceeds hard ceiling; capping at %d", max_retries, MAX_RETRIES)
    retries = max(0, min(max_retries, MAX_RETRIES))
```

`--max-retries 50` gets you 2 and a warning. `--max-retries 0` is allowed: it turns the gatekeeper into a build-and-classify step with no model calls.

Why two? One attempt fixes the common case: a single typo, flag, or package name. The second covers the most frequent follow-on, where fixing the first error lets the build get further and expose a second one. Past two, I'd rather a person look at it. Three failed fixes in a row usually means the build is broken in a way a one-line change won't fix.

The cap is the outer limit. Two more checks inside the loop stop it *earlier*:

- **`unusable_patch`**: if the sanitizer rejects Claude's response, the loop stops immediately. It doesn't spend the second call hoping for better output.
- **`no_progress`**: if the "fix" is identical to the current Dockerfile, rebuilding it would just reproduce the same failure, so the loop stops.

Together these mean the worst case is fixed and small: three builds and two Claude calls per gatekeeper run. A test pins exactly that:

```python
def test_never_converging_fix_stops_after_two_claude_calls(self, dockerfile):
    backend, claude = FakeBackend(), FakeClaude()
    report = gk.run_gatekeeper(dockerfile, dockerfile.parent, backend, claude)
    assert not report.success
    assert report.claude_calls == 2 == len(claude.prompts)
    assert len(backend.calls) == 3  # initial build + one rebuild per patch
    assert report.stop_reason == "retries_exhausted"
```

`FakeClaude` returns a new, valid, *different* Dockerfile on every call, so the only thing stopping that loop is the cap.

## Decision 6: Stage fixes, and write back only on a passing build

Candidate fixes never touch the original Dockerfile. Each one is written to a sibling file, `Dockerfile.broken.gatekeeper`, and that file is what gets rebuilt:

```python
current = proposed
working.write_text(current)
build_path = working
result = backend.build(context, build_path, tag)
```

The original is overwritten only when three things are true at once: the last build passed, the content actually changed, and the operator passed `--in-place`:

```python
if result.success and current != original and in_place:
    dockerfile.write_text(current)
    working.unlink(missing_ok=True)
```

Without `--in-place`, a successful run leaves the original alone and the fix in the staging file. That's the mode I'd use in CI: the job can upload the staging file and the `--json` report (which includes each attempt's unified diff) as artifacts, or open a pull request from them. A model-written change gets reviewed before it lands on `main`, and the original file is never left half-patched by a run that failed or was cancelled midway.

Two tests cover both sides: `test_original_dockerfile_untouched_until_build_passes` and `test_in_place_writes_back_on_success`.

One caveat I hit while writing this: BuildKit supports per-Dockerfile ignore files named `<Dockerfile>.dockerignore`. The staging copy has a different name, so a `Dockerfile.broken.dockerignore` won't apply to the rebuild. A plain `.dockerignore` at the context root works as usual.

## Testing it without Docker or a model

The suite has 50 tests and needs neither Docker nor Claude. The build backend and Claude are replaced with small fakes that follow the same interfaces:

```python
class FakeBackend:
    """Fails while `fail_while(dockerfile_text)` is true (default: always)."""

    def __init__(self, fail_while=lambda text: True):
        self.fail_while = fail_while
        self.calls: list[Path] = []

    def build(self, context, dockerfile, tag):
        self.calls.append(dockerfile)
        if self.fail_while(dockerfile.read_text()):
            return BuildResult(success=False, exit_code=1, failure=FAILURE)
        return BuildResult(success=True, exit_code=0)
```

`FakeBackend(fail_while=lambda text: BROKEN_RUN in text)` behaves like a real build of the fixture: it fails until the bad `RUN` line is gone. That lets the end-to-end tests run the real parser, prompt builder, sanitizer, and staging logic against a scripted model response.

The fake's first version taught me something. It originally checked whether `--no-cache-dirs` appeared *anywhere* in the file, and three tests failed with correct fixes. The fixture's header comment explains the bug, and the explanation names the bad flag, so the fake kept seeing it. The predicate now matches the full broken `RUN` line. A real gatekeeper doesn't have this problem because Docker doesn't execute comments, but it's a reminder that a test harness that pattern-matches over a whole file will match the comments too.

The suite is split into the three guarantees the gatekeeper makes:

| Group | Tests | What it proves |
|---|---|---|
| `TestBuildKitParser` | 19 | Categories for invalid flags, repo signatures, EOL repos, missing apt and pip packages, network failures, cache mounts, missing COPY sources; line, stage, and step extraction; ANSI/CRLF tolerance; legacy-builder logs; a real Docker 29 log |
| `TestPatchSanitization` | 16 | Fenced and bare diffs, wrong hunk offsets, full-file responses, `# syntax=` restoration, dropped-comment reporting, and seven classes of rejected response |
| `TestRetryCap` + `TestClaudeCli` | 15 | The two-call ceiling, early stops, exit codes 1 and 2, prompt contents, the `claude -p` invocation, `--model`, and stdout-only errors |

```text
$ pytest tests/ -v
...
============================== 50 passed in 0.11s ==============================
```

GitHub Actions runs the same suite on Python 3.10, 3.11, 3.12, and 3.13 on every push.

## Running it

```bash
git clone https://github.com/SivaSaiKrishnaSuryadevara/docker-ci-gatekeeper.git
cd docker-ci-gatekeeper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Build, diagnose, and attempt up to two fixes. The original file is untouched;
# a passing fix is left in Dockerfile.broken.gatekeeper.
python gatekeeper.py -f Dockerfile.broken . --claude-bin "$(command -v claude)" --model sonnet

# Same, with the full report (each attempt's failure record and diff) as JSON.
python gatekeeper.py -f Dockerfile.broken . --json

# Write the fix back to Dockerfile.broken only if the patched build passes.
python gatekeeper.py -f Dockerfile.broken . --in-place
```

| Exit code | Meaning | What CI should do |
|---|---|---|
| `0` | Build passed (as-is or after a fix) | Continue; review the staging file if a fix was made |
| `1` | Still failing after the attempts allowed | Fail the job; the report shows what was tried |
| `2` | Environment problem: no Dockerfile, no daemon, no `claude`, timeout | Fail the job as infrastructure, not code |

## A real run

With Docker running under Colima (Docker 29.5.2, buildx 0.37.2, a 4-CPU VM) and Sonnet as the model, this is the complete console output:

```text
$ python gatekeeper.py -f Dockerfile.broken . --claude-bin "$CLAUDE" --model sonnet
INFO Build failed (invalid_command_flag at line 20); asking Claude for a fix (1/2)
patched Dockerfile written to Dockerfile.broken.gatekeeper
attempt 0: FAIL [invalid_command_flag] line 20: no such option: --no-cache-dirs
attempt 1: PASS
result: PASS (passed, 1 Claude call(s))
$ echo $?
0
```

The whole run took about 12 seconds with the base image already pulled. Status and results go to stdout, and log lines and the staging-file notice go to stderr, so `--json` output stays clean for a pipeline to parse.

Claude's change, from `diff -u Dockerfile.broken Dockerfile.broken.gatekeeper`, was one character:

```diff
@@ -17,7 +17,7 @@
     pip install --upgrade pip wheel
 
 # BROKEN: invalid pip flag (should be --no-cache-dir)
-RUN pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3
+RUN pip wheel --no-cache-dir --wheel-dir /wheels requests==2.32.3
 
 # ---- runtime: install prebuilt wheels only ---------------------------------
 FROM python:${PYTHON_VERSION}-slim AS runtime
```

The `# syntax=` directive, every comment, the cache mount, and both stage names came through untouched, and `dropped_comments` was empty. `Dockerfile.broken` itself was not modified, because I didn't pass `--in-place`. The rebuilt image runs:

```text
$ docker run --rm gatekeeper-build:latest
2.32.3
```

That's one fixture and one model, so it shows the loop works, not that it fixes every failure. But every boundary in it is real: a live daemon, a real BuildKit log, a real model response, and a real rebuild.

## What I'd be careful about

- **One real run is one data point.** The unit tests prove the parser, sanitizer, cap, and staging logic, and the run above proves the loop closes on a live daemon. Neither proves that Claude will fix *your* failure. That's what the rebuild step is for: treat a passing rebuild as the evidence, not the model's confidence.
- **Log formats move.** The Docker 29 log above already differed from what I expected, in two places. Capture a real failing log from your own Docker version, drop it in `tests/fixtures/`, and assert on it.
- **"Passes" is not "correct."** A rebuild that exits 0 proves the image builds. It doesn't prove the image works. Keeping fixes in a staging file for review exists for that gap.
- **The taxonomy is regex, and regexes drift.** BuildKit and package managers reword their errors over time. An unknown message degrades to `COMMAND_FAILED` or `UNKNOWN`, which still produces a usable prompt, just a less specific one. When a category starts missing, add the new phrasing and a test with the real log line.
- **Don't let the gatekeeper hide flakiness.** A `NETWORK` failure isn't something a Dockerfile edit should fix. If your registry or mirror is flaky, a model may "fix" it by pinning a different mirror. The category in the report makes that easy to spot. Whether to skip the model for that category is a policy decision I've left to the pipeline.

The design comes down to a few rules: the log gets parsed before the model sees it, the model never writes files, every patch is checked before it's used, nothing replaces the original without a passing build, and there are never more than two attempts.
