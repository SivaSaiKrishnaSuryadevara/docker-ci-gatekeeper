#!/usr/bin/env python3
"""Self-healing Docker CI gatekeeper.

Builds a Dockerfile, and on failure:
  1. parses the build log into a structured failure (category, failing
     instruction, Dockerfile line, stage, surrounding snippet),
  2. asks the Claude Code CLI (`claude -p`, non-interactive print mode) for a
     minimal fix as a unified diff or a complete corrected Dockerfile,
  3. sanitizes and applies that patch to a working copy,
  4. rebuilds — at most MAX_RETRIES times, so a fix that never converges
     can't turn into an unbounded loop of paid model calls.

Build backends:
  * ``buildkit`` (default): ``docker build --progress=plain`` with
    DOCKER_BUILDKIT=1. docker-py cannot drive BuildKit itself — the Engine
    only exposes BuildKit through a gRPC session protocol docker-py doesn't
    implement, so its ``images.build()`` always uses the legacy builder.
    docker-py is still used here for the daemon preflight and to verify the
    built image exists.
  * ``docker-py``: the legacy builder via the docker-py low-level API, for
    environments without the docker CLI.

Exit codes: 0 build passed, 1 build still failing after retries, 2 usage or
environment error (missing Dockerfile, daemon down, claude unavailable).
"""

from __future__ import annotations

import argparse
import difflib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

log = logging.getLogger("gatekeeper")

MAX_RETRIES = 2  # hard ceiling on Claude calls per run — not overridable upward
MAX_RESPONSE_BYTES = 200_000
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PARSER_DIRECTIVE_RE = re.compile(r"^#\s*(syntax|escape|check)\s*=", re.IGNORECASE)


# =============================================================================
# Models
# =============================================================================

class FailureCategory(str, Enum):
    CACHE_MOUNT = "cache_mount"
    SYNTAX = "dockerfile_syntax"
    MISSING_FILE = "missing_build_context_file"
    REPO_SIGNATURE = "package_repo_signature"
    REPO_UNAVAILABLE = "package_repo_unavailable"
    MISSING_PACKAGE = "missing_package"
    INVALID_FLAG = "invalid_command_flag"
    NETWORK = "network"
    COMMAND_FAILED = "command_failed"
    UNKNOWN = "unknown"


# Checked in this order; the first category whose pattern matches wins.
# Specific causes come before COMMAND_FAILED, which matches almost any failed RUN.
CATEGORY_PATTERNS: list[tuple[FailureCategory, re.Pattern[str]]] = [
    (FailureCategory.CACHE_MOUNT, re.compile(
        r"--mount option requires BuildKit|invalid mount config|failed to (?:create|prepare|mount) .*cache"
        r"|cache mount|unknown mount (?:type|option)|mount type=cache", re.IGNORECASE)),
    (FailureCategory.SYNTAX, re.compile(
        r"unknown instruction|dockerfile parse error|failed to parse dockerfile|unexpected end of statement", re.IGNORECASE)),
    (FailureCategory.MISSING_FILE, re.compile(
        r"failed to compute cache key|failed to calculate checksum|COPY failed|ADD failed", re.IGNORECASE)),
    (FailureCategory.REPO_SIGNATURE, re.compile(
        r"NO_PUBKEY|is not signed|GPG error|EXPKEYSIG|signatures (?:were invalid|couldn't be verified)", re.IGNORECASE)),
    (FailureCategory.REPO_UNAVAILABLE, re.compile(
        r"does not have a Release file|no longer has a Release file|Release' is not valid yet"
        r"|404\s+Not Found\s+\[IP", re.IGNORECASE)),
    (FailureCategory.MISSING_PACKAGE, re.compile(
        r"Unable to locate package|has no installation candidate|No matching distribution found"
        r"|Could not find a version that satisfies|no such package", re.IGNORECASE)),
    (FailureCategory.INVALID_FLAG, re.compile(
        r"no such option:|unrecognized arguments?:|invalid option|unknown (?:flag|option|shorthand flag)", re.IGNORECASE)),
    (FailureCategory.NETWORK, re.compile(
        r"Temporary failure resolving|Could not resolve host|Connection timed out|Network is unreachable"
        r"|TLS handshake timeout", re.IGNORECASE)),
    (FailureCategory.COMMAND_FAILED, re.compile(
        r"did not complete successfully|returned a non-zero code|exit code: \d+", re.IGNORECASE)),
]


class BuildFailure(BaseModel):
    category: FailureCategory
    message: str
    instruction: str | None = None
    dockerfile_line: int | None = None
    stage: str | None = None
    step: str | None = None
    exit_code: int | None = None
    snippet: list[str] = Field(default_factory=list)
    step_output: list[str] = Field(default_factory=list)

    def signature(self) -> tuple:
        return (self.category, self.dockerfile_line, self.instruction)


class BuildResult(BaseModel):
    success: bool
    exit_code: int
    log: str = ""
    failure: BuildFailure | None = None


class Attempt(BaseModel):
    number: int
    success: bool
    failure: BuildFailure | None = None
    patch_diff: str | None = None


class GatekeeperReport(BaseModel):
    success: bool
    attempts: list[Attempt]
    claude_calls: int
    stop_reason: str
    patched_dockerfile: str | None = None
    dropped_comments: list[str] = Field(default_factory=list)


class GatekeeperError(Exception):
    """Environment or input problem that should stop the run (exit code 2)."""


class PatchError(Exception):
    """A model response that can't be safely turned into a Dockerfile."""


# =============================================================================
# Dockerfile structure
# =============================================================================

@dataclass(frozen=True)
class Instruction:
    line: int  # 1-based line where the instruction starts
    text: str  # continuation lines joined, whitespace normalized
    stage: str


def _normalize(text: str) -> str:
    return " ".join(text.split())


def index_dockerfile(text: str) -> list[Instruction]:
    """Split a Dockerfile into instructions, tracking start lines and stage names."""
    instructions: list[Instruction] = []
    stage, stage_count = "", 0
    buf: list[str] = []
    start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if buf and stripped.startswith("#"):  # comments inside a continuation are dropped by Docker
            continue
        if not buf:
            start = lineno
        continued = stripped.endswith("\\")
        buf.append(stripped[:-1] if continued else stripped)
        if continued:
            continue
        joined = _normalize(" ".join(buf))
        buf = []
        if joined.upper().startswith("FROM "):
            m = re.search(r"\s+AS\s+(\S+)\s*$", joined, re.IGNORECASE)
            stage = m.group(1) if m else f"stage-{stage_count}"
            stage_count += 1
        instructions.append(Instruction(start, joined, stage))
    return instructions


def locate_instruction(text: str, instruction: str) -> Instruction | None:
    target = _normalize(instruction)
    for inst in index_dockerfile(text):
        if inst.text == target or inst.text.startswith(target) or target.startswith(inst.text):
            return inst
    return None


def stage_for_line(text: str, line: int) -> str | None:
    current = None
    for inst in index_dockerfile(text):
        if inst.line > line:
            break
        current = inst.stage
    return current


# =============================================================================
# Log parsing
# =============================================================================

def categorize(text: str) -> FailureCategory:
    for category, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return category
    return FailureCategory.UNKNOWN


def _best_message(lines: list[str], fallback: str) -> str:
    """The most specific line: the first that matches a non-generic category."""
    for line in lines:
        cat = categorize(line)
        if cat not in (FailureCategory.UNKNOWN, FailureCategory.COMMAND_FAILED):
            return line.strip()
    return fallback.strip()


STEP_HEADER_RE = re.compile(r"^#(?P<id>\d+) \[(?:(?P<stage>[^\s\]]+) )?(?P<pos>\d+/\d+)\] (?P<instr>.+)$")
STEP_LINE_RE = re.compile(r"^#(?P<id>\d+) (?:\d+\.\d+(?: |$))?(?P<text>.*)$")
SNIPPET_LINE_RE = re.compile(r"^\s*(?P<num>\d+) \|( >>>)?\s?(?P<code>.*)$")
EXIT_CODE_RE = re.compile(r"exit code: (\d+)|returned a non-zero code: (\d+)")


def parse_buildkit_log(log_text: str, dockerfile_text: str) -> BuildFailure:
    lines = [ANSI_RE.sub("", ln).rstrip("\r") for ln in log_text.splitlines()]

    headers: dict[str, re.Match[str]] = {}
    step_output: dict[str, list[str]] = {}
    failing_id: str | None = None
    error_line = ""
    for ln in lines:
        if m := STEP_HEADER_RE.match(ln):
            headers[m.group("id")] = m
            continue
        if m := STEP_LINE_RE.match(ln):
            sid, body = m.group("id"), m.group("text")
            if body.startswith("ERROR:") and failing_id is None:
                failing_id, error_line = sid, body
            else:
                step_output.setdefault(sid, []).append(body)
        elif ln.startswith("ERROR:") and not error_line:
            error_line = ln

    # BuildKit prints "<dockerfile>:<line>" then a dashed snippet with ">>>" on failing lines.
    dockerfile_line: int | None = None
    snippet: list[str] = []
    for i, ln in enumerate(lines):
        m = re.match(r"^\S+:(\d+)$", ln)
        if m and i + 1 < len(lines) and lines[i + 1].startswith("-----"):
            dockerfile_line = int(m.group(1))
            for sn in lines[i + 2:]:
                if sn.startswith("-----"):
                    break
                snippet.append(sn)
            break
    if final := next((ln for ln in lines if ln.startswith("ERROR: failed to solve")), None):
        error_line = final

    header = headers.get(failing_id) if failing_id else None
    instruction = header.group("instr") if header else None
    stage = header.group("stage") if header else None
    step = header.group("pos") if header else None
    if instruction is None and snippet:
        marked = [SNIPPET_LINE_RE.match(s) for s in snippet if " >>>" in s]
        instruction = _normalize(" ".join(m.group("code").rstrip("\\") for m in marked if m)) or None

    if dockerfile_line is None and instruction:
        if inst := locate_instruction(dockerfile_text, instruction):
            dockerfile_line = inst.line
    if stage is None and dockerfile_line:
        stage = stage_for_line(dockerfile_text, dockerfile_line)

    output = step_output.get(failing_id, []) if failing_id else []
    exit_code = None
    if m := EXIT_CODE_RE.search(error_line):
        exit_code = int(m.group(1) or m.group(2))

    evidence = "\n".join([*output, error_line])
    return BuildFailure(
        category=categorize(evidence),
        message=_best_message(output, error_line or "Build failed"),
        instruction=instruction,
        dockerfile_line=dockerfile_line,
        stage=stage,
        step=step,
        exit_code=exit_code,
        snippet=snippet,
        step_output=output[-40:],
    )


CLASSIC_STEP_RE = re.compile(r"^Step (?P<pos>\d+/\d+) : (?P<instr>.+)$")


def parse_classic_log(log_text: str, dockerfile_text: str) -> BuildFailure:
    """Parse legacy-builder output (docker-py stream + error chunks)."""
    lines = [ANSI_RE.sub("", ln).rstrip("\r") for ln in log_text.splitlines()]
    step = instruction = None
    output: list[str] = []
    for ln in lines:
        if m := CLASSIC_STEP_RE.match(ln):
            step, instruction, output = m.group("pos"), m.group("instr"), []
        elif not ln.startswith(" ---> ") and not ln.startswith("Removing intermediate"):
            output.append(ln)
    error_line = next((ln for ln in reversed(lines) if "returned a non-zero code" in ln or ln.startswith("ERROR")), "")
    inst = locate_instruction(dockerfile_text, instruction) if instruction else None
    exit_code = None
    if m := EXIT_CODE_RE.search(error_line):
        exit_code = int(m.group(1) or m.group(2))
    return BuildFailure(
        category=categorize("\n".join(output)),
        message=_best_message(output, error_line or "Build failed"),
        instruction=instruction,
        dockerfile_line=inst.line if inst else None,
        stage=inst.stage if inst else None,
        step=step,
        exit_code=exit_code,
        step_output=output[-40:],
    )


# =============================================================================
# Build backends
# =============================================================================

class BuildBackend(Protocol):
    def build(self, context: Path, dockerfile: Path, tag: str) -> BuildResult: ...


def preflight_daemon(timeout: int = 10) -> None:
    import docker
    from docker.errors import DockerException

    try:
        docker.from_env(timeout=timeout).ping()
    except DockerException as exc:
        raise GatekeeperError(f"Docker daemon unreachable: {exc}") from exc


class BuildKitCliBackend:
    def __init__(self, docker_bin: str = "docker", timeout: int = 1800):
        self.docker_bin, self.timeout = docker_bin, timeout

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
        if proc.returncode == 0:
            return BuildResult(success=True, exit_code=0, log=output)
        failure = parse_buildkit_log(output, dockerfile.read_text())
        return BuildResult(success=False, exit_code=proc.returncode, log=output, failure=failure)


class DockerPyBackend:
    def __init__(self, timeout: int = 1800):
        self.timeout = timeout

    def build(self, context: Path, dockerfile: Path, tag: str) -> BuildResult:
        import docker
        from docker.errors import DockerException

        try:
            client = docker.from_env(timeout=self.timeout)
            stream = client.api.build(
                path=str(context), dockerfile=str(dockerfile.resolve()), tag=tag, rm=True, decode=True
            )
            lines: list[str] = []
            error = None
            for chunk in stream:
                if "stream" in chunk:
                    lines.extend(chunk["stream"].splitlines())
                if "error" in chunk:
                    error = chunk["error"].strip()
                    lines.append(f"ERROR: {error}")
        except DockerException as exc:
            raise GatekeeperError(f"docker-py build failed to run: {exc}") from exc
        output = "\n".join(lines)
        if error is None:
            return BuildResult(success=True, exit_code=0, log=output)
        failure = parse_classic_log(output, dockerfile.read_text())
        return BuildResult(success=False, exit_code=1, log=output, failure=failure)


# =============================================================================
# Claude Code CLI
# =============================================================================

class ClaudeCli:
    def __init__(self, claude_bin: str = "claude", timeout: int = 300):
        self.claude_bin, self.timeout = claude_bin, timeout

    def propose_fix(self, prompt: str) -> str:
        if shutil.which(self.claude_bin) is None and not Path(self.claude_bin).is_file():
            raise GatekeeperError(f"Claude Code CLI '{self.claude_bin}' not found (pass --claude-bin)")
        # Run from an empty scratch dir: print mode can't get edit permissions
        # approved anyway, and this way there's nothing in its cwd to touch.
        with tempfile.TemporaryDirectory(prefix="gatekeeper-claude-") as scratch:
            try:
                proc = subprocess.run(
                    [self.claude_bin, "-p", prompt, "--output-format", "text"],
                    capture_output=True, text=True, timeout=self.timeout, cwd=scratch,
                )
            except subprocess.TimeoutExpired as exc:
                raise GatekeeperError(f"claude -p timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise GatekeeperError(f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        return proc.stdout


def build_prompt(failure: BuildFailure, dockerfile_text: str, dockerfile_name: str) -> str:
    numbered = "\n".join(f"{i:4} | {ln}" for i, ln in enumerate(dockerfile_text.splitlines(), start=1))
    output = "\n".join(failure.step_output[-25:]) or "(no step output captured)"
    return f"""A Docker build failed. Propose the smallest change to {dockerfile_name} that fixes it.

Failure category: {failure.category.value}
Failing stage: {failure.stage or "unknown"}
Failing instruction (line {failure.dockerfile_line or "unknown"}): {failure.instruction or "unknown"}
Exit code: {failure.exit_code if failure.exit_code is not None else "unknown"}
Key error: {failure.message}

Last output from the failing step:
{output}

Current {dockerfile_name}:
{numbered}

Rules:
- Change only what is needed to fix this failure. Do not restructure stages or upgrade unrelated things.
- Keep every comment and parser directive (lines starting with "# syntax=", "# escape=", "# check=").
- Respond with EITHER a unified diff against {dockerfile_name} (--- a/{dockerfile_name} / +++ b/{dockerfile_name})
  OR the complete corrected file in a single ```dockerfile code block. No other files.
- Line numbers above are for reference only; do not include them in your answer."""


# =============================================================================
# Patch sanitization and application
# =============================================================================

FENCE_RE = re.compile(r"```(?P<lang>[\w.+-]*)[ \t]*\n(?P<body>.*?)```", re.DOTALL)
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _clean(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _diff_target(path: str) -> str:
    path = path.strip().split("\t")[0]
    return path[2:] if path[:2] in ("a/", "b/") else path


def apply_unified_diff(original: str, diff: str, expected_name: str) -> str:
    lines = original.split("\n")
    diff_lines = diff.split("\n")
    hunks: list[tuple[int, list[str], list[str]]] = []
    seen_header = False
    i = 0
    while i < len(diff_lines):
        ln = diff_lines[i]
        if ln.startswith("--- ") and i + 1 < len(diff_lines) and diff_lines[i + 1].startswith("+++ "):
            for target in (_diff_target(ln[4:]), _diff_target(diff_lines[i + 1][4:])):
                if target != "/dev/null" and Path(target).name != Path(expected_name).name:
                    raise PatchError(f"Diff targets '{target}', expected only {expected_name}")
            seen_header = True
            i += 2
            continue
        if m := HUNK_RE.match(ln):
            old_start = int(m.group(1))
            old_block: list[str] = []
            new_block: list[str] = []
            i += 1
            while i < len(diff_lines) and not HUNK_RE.match(diff_lines[i]) and not diff_lines[i].startswith("--- "):
                h = diff_lines[i]
                if h.startswith("\\"):
                    pass  # "\ No newline at end of file"
                elif h == "":
                    old_block.append("")  # models often drop the leading space on blank context lines
                    new_block.append("")
                elif h[0] == " ":
                    old_block.append(h[1:])
                    new_block.append(h[1:])
                elif h[0] == "-":
                    old_block.append(h[1:])
                elif h[0] == "+":
                    new_block.append(h[1:])
                else:
                    break
                i += 1
            while old_block and new_block and old_block[-1] == "" and new_block[-1] == "":
                old_block.pop()
                new_block.pop()
            hunks.append((old_start, old_block, new_block))
            continue
        i += 1
    if not hunks:
        raise PatchError("No hunks found in diff")
    if not seen_header:
        raise PatchError("Diff is missing ---/+++ file headers")

    offset = 0
    for old_start, old_block, new_block in hunks:
        expected = max(old_start - 1 + offset, 0)
        pos = _find_block(lines, old_block, expected)
        if pos is None:
            raise PatchError(f"Hunk at line {old_start} does not match the current Dockerfile")
        lines[pos:pos + len(old_block)] = new_block
        offset += len(new_block) - len(old_block)
    return "\n".join(lines)


def _find_block(lines: list[str], block: list[str], expected: int) -> int | None:
    if not block:
        return expected
    n = len(block)
    if lines[expected:expected + n] == block:
        return expected
    matches = [p for p in range(len(lines) - n + 1) if lines[p:p + n] == block]
    return matches[0] if len(matches) == 1 else None  # ambiguous or absent: refuse


def _looks_like_dockerfile(text: str) -> bool:
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        return bool(re.match(r"^(FROM|ARG)\b", s, re.IGNORECASE))
    return False


def _restore_parser_directives(original: str, patched: str) -> str:
    directives = []
    for ln in original.splitlines():
        if PARSER_DIRECTIVE_RE.match(ln):
            directives.append(ln)
        else:
            break  # directives are only valid before the first non-directive line
    missing = [d for d in directives if d not in patched.splitlines()[: len(directives) + 1]]
    return ("\n".join(missing) + "\n" + patched) if missing else patched


def sanitize_patch(response: str, original: str, dockerfile_name: str) -> str:
    """Turn a model response into a full Dockerfile, preserving comments.

    Accepts a unified diff (bare or fenced) or a complete Dockerfile in a
    code fence. Everything outside the diff/fence (prose, explanations) is
    discarded; everything inside is kept verbatim — including `#` lines,
    which a naive markdown cleaner would strip as headings.
    """
    if len(response.encode()) > MAX_RESPONSE_BYTES:
        raise PatchError("Response exceeds size limit")
    text = _clean(response)

    fences = list(FENCE_RE.finditer(text))
    diff_body = None
    for f in fences:
        if f.group("lang").lower() in ("diff", "patch") or ("\n@@ " in "\n" + f.group("body") and "--- " in f.group("body")):
            diff_body = f.group("body")
            break
    if diff_body is None and re.search(r"^--- .*\n\+\+\+ .*\n@@ ", text, re.MULTILINE):
        diff_body = text

    if diff_body is not None:
        patched = apply_unified_diff(original, diff_body, dockerfile_name)
    else:
        docker_fences = [f for f in fences if f.group("lang").lower() in ("dockerfile", "docker")]
        chosen = docker_fences or [f for f in fences if _looks_like_dockerfile(f.group("body"))]
        if len(chosen) > 1:
            raise PatchError("Response contains multiple Dockerfile blocks; refusing to guess")
        if chosen:
            patched = chosen[0].group("body")
        elif _looks_like_dockerfile(text):
            patched = text
        else:
            raise PatchError("Response contains neither a unified diff nor a Dockerfile code block")

    patched = _restore_parser_directives(original, patched)
    if not patched.endswith("\n"):
        patched += "\n"
    if not any(inst.text.upper().startswith("FROM ") for inst in index_dockerfile(patched)):
        raise PatchError("Patched Dockerfile has no FROM instruction")
    return patched


def dropped_comments(original: str, patched: str) -> list[str]:
    kept = {ln.strip() for ln in patched.splitlines()}
    return [ln.strip() for ln in original.splitlines() if ln.strip().startswith("#") and ln.strip() not in kept]


# =============================================================================
# Orchestration
# =============================================================================

def run_gatekeeper(
    dockerfile: Path,
    context: Path,
    backend: BuildBackend,
    claude: ClaudeCli,
    tag: str = "gatekeeper-build:latest",
    max_retries: int = MAX_RETRIES,
    in_place: bool = False,
) -> GatekeeperReport:
    if max_retries > MAX_RETRIES:
        log.warning("max_retries=%d exceeds hard ceiling; capping at %d", max_retries, MAX_RETRIES)
    retries = max(0, min(max_retries, MAX_RETRIES))

    original = dockerfile.read_text()
    current = original
    working = dockerfile.with_name(dockerfile.name + ".gatekeeper")
    attempts: list[Attempt] = []
    claude_calls = 0
    build_path = dockerfile

    result = backend.build(context, build_path, tag)
    attempts.append(Attempt(number=0, success=result.success, failure=result.failure))

    stop_reason = "passed" if result.success else "retries_exhausted"
    for n in range(1, retries + 1):
        if result.success:
            break
        assert result.failure is not None
        log.info("Build failed (%s at line %s); asking Claude for a fix (%d/%d)",
                 result.failure.category.value, result.failure.dockerfile_line, n, retries)
        prompt = build_prompt(result.failure, current, dockerfile.name)
        claude_calls += 1
        try:
            proposed = sanitize_patch(claude.propose_fix(prompt), current, dockerfile.name)
        except PatchError as exc:
            log.warning("Unusable patch from Claude: %s", exc)
            stop_reason = f"unusable_patch: {exc}"
            break
        if proposed == current:
            stop_reason = "no_progress"
            break
        diff = "".join(difflib.unified_diff(
            current.splitlines(keepends=True), proposed.splitlines(keepends=True),
            fromfile=f"a/{dockerfile.name}", tofile=f"b/{dockerfile.name}",
        ))
        current = proposed
        working.write_text(current)
        build_path = working
        result = backend.build(context, build_path, tag)
        attempts.append(Attempt(number=n, success=result.success, failure=result.failure, patch_diff=diff))
        stop_reason = "passed" if result.success else "retries_exhausted"

    if result.success and current != original and in_place:
        dockerfile.write_text(current)
        working.unlink(missing_ok=True)

    return GatekeeperReport(
        success=result.success,
        attempts=attempts,
        claude_calls=claude_calls,
        stop_reason=stop_reason,
        patched_dockerfile=current if current != original else None,
        dropped_comments=dropped_comments(original, current),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Self-healing Docker build gatekeeper")
    parser.add_argument("-f", "--file", default="Dockerfile", help="Dockerfile to build")
    parser.add_argument("context", nargs="?", default=".", help="Build context directory")
    parser.add_argument("-t", "--tag", default="gatekeeper-build:latest")
    parser.add_argument("--backend", choices=["buildkit", "docker-py"], default="buildkit")
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES, help=f"Claude fix attempts (hard cap {MAX_RETRIES})")
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    parser.add_argument("--in-place", action="store_true", help="Overwrite the Dockerfile once a patched build passes")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    dockerfile, context = Path(args.file), Path(args.context)
    try:
        if not dockerfile.is_file():
            raise GatekeeperError(f"Dockerfile not found: {dockerfile}")
        if not context.is_dir():
            raise GatekeeperError(f"Build context not found: {context}")
        preflight_daemon()
        backend: BuildBackend = BuildKitCliBackend() if args.backend == "buildkit" else DockerPyBackend()
        report = run_gatekeeper(
            dockerfile, context, backend, ClaudeCli(args.claude_bin),
            tag=args.tag, max_retries=args.max_retries, in_place=args.in_place,
        )
    except GatekeeperError as exc:
        log.error("%s", exc)
        return 2

    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        for a in report.attempts:
            status = "PASS" if a.success else f"FAIL [{a.failure.category.value}] line {a.failure.dockerfile_line}: {a.failure.message}"
            print(f"attempt {a.number}: {status}")
        print(f"result: {'PASS' if report.success else 'FAIL'} ({report.stop_reason}, {report.claude_calls} Claude call(s))")
        if report.dropped_comments:
            print("warning: patch removed comments: " + "; ".join(report.dropped_comments))
    if report.patched_dockerfile and not args.in_place:
        print(f"patched Dockerfile written to {dockerfile.with_name(dockerfile.name + '.gatekeeper')}", file=sys.stderr)
    return 0 if report.success else 1


if __name__ == "__main__":
    sys.exit(main())
