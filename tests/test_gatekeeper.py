"""Unit tests for gatekeeper.py.

Docker and the Claude Code CLI are never invoked: build backends are fakes
or have subprocess.run mocked, and Claude is a scripted stub.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gatekeeper as gk  # noqa: E402
from gatekeeper import BuildFailure, BuildResult, FailureCategory  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BROKEN = (ROOT / "Dockerfile.broken").read_text()
BROKEN_RUN = "RUN pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3"
FIXED_RUN = "RUN pip wheel --no-cache-dir --wheel-dir /wheels requests==2.32.3"


def comment_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip().startswith("#")]


# Captured shape of `docker build --progress=plain` output for Dockerfile.broken.
BUILDKIT_INVALID_FLAG_LOG = f"""\
#0 building with "desktop-linux" instance using docker driver

#1 [internal] load build definition from Dockerfile.broken
#1 transferring dockerfile: 1.21kB done
#1 DONE 0.0s

#5 [builder 1/4] FROM docker.io/library/python:3.12-slim
#5 CACHED

#7 [builder 3/4] RUN --mount=type=cache,target=/root/.cache/pip     pip install --upgrade pip wheel
#7 CACHED

#8 [builder 4/4] {BROKEN_RUN}
#8 0.412
#8 0.412 Usage:
#8 0.412   pip wheel [options] <requirement specifier> ...
#8 0.412
#8 0.412 no such option: --no-cache-dirs
#8 ERROR: process "/bin/sh -c pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3" did not complete successfully: exit code: 2
------
 > [builder 4/4] {BROKEN_RUN}:
0.412 no such option: --no-cache-dirs
------
Dockerfile.broken:20
--------------------
  18 |
  19 |     # BROKEN: invalid pip flag (should be --no-cache-dir)
  20 | >>> {BROKEN_RUN}
  21 |
  22 |     # ---- runtime: install prebuilt wheels only ---------------------------------
--------------------
ERROR: failed to solve: process "/bin/sh -c pip wheel --no-cache-dirs --wheel-dir /wheels requests==2.32.3" did not complete successfully: exit code: 2
"""

APT_DOCKERFILE = """\
FROM debian:buster-slim AS base
RUN apt-get update && apt-get install -y curl
"""


def step_log(instr: str, body: list[str], stage: str = "base", line: int = 2, code: int = 100) -> str:
    out = [f"#6 [{stage} 2/2] {instr}"]
    out += [f"#6 1.{i:03d} {b}" for i, b in enumerate(body)]
    out.append(f'#6 ERROR: process "/bin/sh -c {instr[4:]}" did not complete successfully: exit code: {code}')
    out += ["------", f"Dockerfile:{line}", "--------------------", f"   {line} | >>> {instr}", "--------------------"]
    out.append(f'ERROR: failed to solve: process "/bin/sh -c {instr[4:]}" did not complete successfully: exit code: {code}')
    return "\n".join(out)


# =============================================================================
# 1. BuildKit error parser categorizes layer failures
# =============================================================================

class TestBuildKitParser:
    def test_invalid_pip_flag_extracts_instruction_line_stage(self):
        f = gk.parse_buildkit_log(BUILDKIT_INVALID_FLAG_LOG, BROKEN)
        assert f.category is FailureCategory.INVALID_FLAG
        assert f.instruction == BROKEN_RUN
        assert f.dockerfile_line == 20
        assert f.stage == "builder"
        assert f.step == "4/4"
        assert f.exit_code == 2
        assert f.message == "no such option: --no-cache-dirs"
        assert any(">>>" in s for s in f.snippet)

    def test_output_from_other_steps_does_not_leak(self):
        f = gk.parse_buildkit_log(BUILDKIT_INVALID_FLAG_LOG, BROKEN)
        assert not any("CACHED" in ln or "transferring" in ln for ln in f.step_output)

    @pytest.mark.parametrize(
        "body, expected",
        [
            (["W: GPG error: http://deb.debian.org buster InRelease: The following signatures couldn't be verified"
              " because the public key is not available: NO_PUBKEY 648ACFD622F3D138",
              "E: The repository 'http://deb.debian.org/debian buster InRelease' is not signed."],
             FailureCategory.REPO_SIGNATURE),
            (["E: The repository 'http://deb.debian.org/debian buster Release' does not have a Release file."],
             FailureCategory.REPO_UNAVAILABLE),
            (["Reading package lists...", "E: Unable to locate package libpq-devv"],
             FailureCategory.MISSING_PACKAGE),
            (["ERROR: Could not find a version that satisfies the requirement reqests==2.32.3",
              "ERROR: No matching distribution found for reqests==2.32.3"],
             FailureCategory.MISSING_PACKAGE),
            (["Err:1 http://deb.debian.org/debian bookworm InRelease",
              "  Temporary failure resolving 'deb.debian.org'"],
             FailureCategory.NETWORK),
            (["/bin/sh: 1: make: not found"], FailureCategory.COMMAND_FAILED),
        ],
        ids=["repo-signature", "repo-eol", "apt-missing", "pip-missing", "network", "generic-run"],
    )
    def test_run_failure_categories(self, body, expected):
        log = step_log("RUN apt-get update && apt-get install -y curl", body)
        f = gk.parse_buildkit_log(log, APT_DOCKERFILE)
        assert f.category is expected
        assert f.dockerfile_line == 2
        assert f.stage == "base"
        assert f.exit_code == 100

    def test_specific_line_beats_generic_exit_message(self):
        log = step_log("RUN apt-get update && apt-get install -y curl",
                       ["Reading package lists...", "E: Unable to locate package libpq-devv"])
        f = gk.parse_buildkit_log(log, APT_DOCKERFILE)
        assert f.message == "E: Unable to locate package libpq-devv"

    def test_broken_cache_mount_parse_error(self):
        dockerfile = "FROM python:3.12-slim AS app\nRUN --mount=type=cahce,target=/root/.cache pip install x\n"
        log = "\n".join([
            "#1 [internal] load build definition from Dockerfile",
            "#1 DONE 0.0s",
            "Dockerfile:2",
            "--------------------",
            "   1 |     FROM python:3.12-slim AS app",
            "   2 | >>> RUN --mount=type=cahce,target=/root/.cache pip install x",
            "--------------------",
            'ERROR: failed to solve: dockerfile parse error on line 2: unknown mount type "cahce"',
        ])
        f = gk.parse_buildkit_log(log, dockerfile)
        # Must win over the generic "dockerfile parse error" syntax category.
        assert f.category is FailureCategory.CACHE_MOUNT
        assert f.dockerfile_line == 2
        assert f.instruction.startswith("RUN --mount=type=cahce")
        assert f.stage == "app"

    def test_missing_copy_source(self):
        dockerfile = "FROM alpine AS final\nCOPY app.py /app/\n"
        log = "\n".join([
            "#6 [final 2/2] COPY app.py /app/",
            '#6 ERROR: failed to calculate checksum of ref abc::xyz: "/app.py": not found',
            "------",
            "Dockerfile:2",
            "--------------------",
            "   2 | >>> COPY app.py /app/",
            "--------------------",
            'ERROR: failed to solve: failed to compute cache key: failed to calculate checksum of ref abc::xyz: "/app.py": not found',
        ])
        f = gk.parse_buildkit_log(log, dockerfile)
        assert f.category is FailureCategory.MISSING_FILE
        assert f.instruction == "COPY app.py /app/"

    def test_ansi_and_crlf_are_stripped(self):
        noisy = "\r\n".join("\x1b[31m" + ln + "\x1b[0m" if "ERROR" in ln else ln
                            for ln in BUILDKIT_INVALID_FLAG_LOG.splitlines())
        f = gk.parse_buildkit_log(noisy, BROKEN)
        assert f.category is FailureCategory.INVALID_FLAG
        assert f.dockerfile_line == 20

    def test_unrecognized_log_is_unknown_not_crash(self):
        f = gk.parse_buildkit_log("something odd happened", BROKEN)
        assert f.category is FailureCategory.UNKNOWN
        assert f.dockerfile_line is None

    def test_line_falls_back_to_instruction_lookup_when_no_snippet(self):
        log = "\n".join(BUILDKIT_INVALID_FLAG_LOG.splitlines()[:19])  # cut before the snippet block
        f = gk.parse_buildkit_log(log, BROKEN)
        assert f.dockerfile_line == 20
        assert f.stage == "builder"

    def test_classic_builder_log(self):
        log = "\n".join([
            "Step 3/9 : WORKDIR /build",
            " ---> Running in 1a2b",
            f"Step 5/9 : {BROKEN_RUN}",
            " ---> Running in 3c4d",
            "no such option: --no-cache-dirs",
            "ERROR: The command '/bin/sh -c pip wheel --no-cache-dirs ...' returned a non-zero code: 2",
        ])
        f = gk.parse_classic_log(log, BROKEN)
        assert f.category is FailureCategory.INVALID_FLAG
        assert f.dockerfile_line == 20
        assert f.stage == "builder"
        assert f.exit_code == 2

    def test_real_docker29_log_without_snippet_block(self):
        # Captured from `docker build --progress=plain` on Docker 29.5.2 / buildx 0.37.2 (Colima).
        # This version prints no ">>>" snippet block and prefixes the final line with "failed to build:".
        log = (ROOT / "tests" / "fixtures" / "buildkit_invalid_flag_docker29.log").read_text()
        f = gk.parse_buildkit_log(log, BROKEN)
        assert f.category is FailureCategory.INVALID_FLAG
        assert f.message == "no such option: --no-cache-dirs"
        assert f.instruction == BROKEN_RUN
        assert f.dockerfile_line == 20  # recovered from the instruction text, not a snippet
        assert (f.stage, f.step, f.exit_code) == ("builder", "4/4", 2)
        assert f.snippet == []
        assert not any("CACHED" in ln for ln in f.step_output)

    def test_final_error_line_matches_both_buildx_formats(self):
        assert gk.FINAL_ERROR_RE.match("ERROR: failed to solve: process ...")
        assert gk.FINAL_ERROR_RE.match("ERROR: failed to build: failed to solve: process ...")

    def test_dockerfile_index_handles_continuations_and_stages(self):
        idx = gk.index_dockerfile(BROKEN)
        mount = next(i for i in idx if "--mount" in i.text)
        assert mount.line == 16
        assert mount.text.endswith("pip install --upgrade pip wheel")
        assert {i.stage for i in idx} >= {"builder", "runtime"}

    def test_buildkit_backend_parses_failed_build(self, tmp_path):
        df = tmp_path / "Dockerfile.broken"
        df.write_text(BROKEN)
        proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=BUILDKIT_INVALID_FLAG_LOG)
        with mock.patch.object(gk.shutil, "which", return_value="/usr/bin/docker"), \
             mock.patch.object(gk.subprocess, "run", return_value=proc) as run:
            result = gk.BuildKitCliBackend().build(tmp_path, df, "t:1")
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["docker", "build", "--progress=plain"]
        assert run.call_args.kwargs["env"]["DOCKER_BUILDKIT"] == "1"
        assert not result.success
        assert result.failure.category is FailureCategory.INVALID_FLAG


# =============================================================================
# 2. Patch application sanitizes diffs without destroying comments
# =============================================================================

FIX_DIFF = f"""\
--- a/Dockerfile.broken
+++ b/Dockerfile.broken
@@ -18,4 +18,4 @@

 # BROKEN: invalid pip flag (should be --no-cache-dir)
-{BROKEN_RUN}
+{FIXED_RUN}

"""


class TestPatchSanitization:
    def test_fenced_diff_with_prose_applies_and_keeps_all_comments(self):
        response = (
            "# Root cause\n"  # markdown heading in prose — must not end up in the Dockerfile
            "The flag is misspelled. Here is the fix:\n\n"
            f"```diff\n{FIX_DIFF}```\n\nThis keeps the cache mount intact."
        )
        patched = gk.sanitize_patch(response, BROKEN, "Dockerfile.broken")
        assert FIXED_RUN in patched
        assert BROKEN_RUN not in patched
        assert comment_lines(patched) == comment_lines(BROKEN)
        assert "# Root cause" not in patched
        assert patched.splitlines()[0] == "# syntax=docker/dockerfile:1.7"
        assert gk.dropped_comments(BROKEN, patched) == []

    def test_bare_diff_without_fence(self):
        patched = gk.sanitize_patch(FIX_DIFF, BROKEN, "Dockerfile.broken")
        assert FIXED_RUN in patched
        assert len(patched.splitlines()) == len(BROKEN.splitlines())

    def test_diff_tolerates_wrong_hunk_line_numbers(self):
        shifted = FIX_DIFF.replace("@@ -18,4 +18,4 @@", "@@ -3,4 +3,4 @@")
        patched = gk.sanitize_patch(shifted, BROKEN, "Dockerfile.broken")
        assert FIXED_RUN in patched

    def test_full_file_response_keeps_hash_lines_verbatim(self):
        fixed = BROKEN.replace(BROKEN_RUN, FIXED_RUN)
        response = f"Corrected file:\n\n```dockerfile\n{fixed}```\n"
        patched = gk.sanitize_patch(response, BROKEN, "Dockerfile.broken")
        assert patched == fixed
        assert comment_lines(patched) == comment_lines(BROKEN)

    def test_parser_directive_restored_if_model_drops_it(self):
        fixed = BROKEN.replace(BROKEN_RUN, FIXED_RUN).split("\n", 1)[1]  # drop "# syntax=" line
        patched = gk.sanitize_patch(f"```dockerfile\n{fixed}```", BROKEN, "Dockerfile.broken")
        assert patched.startswith("# syntax=docker/dockerfile:1.7\n")

    def test_ansi_and_crlf_in_response_are_cleaned(self):
        dirty = "\x1b[32m" + FIX_DIFF.replace("\n", "\r\n") + "\x1b[0m"
        patched = gk.sanitize_patch(dirty, BROKEN, "Dockerfile.broken")
        assert "\r" not in patched and "\x1b" not in patched
        assert FIXED_RUN in patched

    def test_dropped_comments_are_reported(self):
        stripped = "\n".join(ln for ln in BROKEN.splitlines() if not ln.startswith("# BROKEN"))
        stripped = stripped.replace(BROKEN_RUN, FIXED_RUN)
        patched = gk.sanitize_patch(f"```dockerfile\n{stripped}\n```", BROKEN, "Dockerfile.broken")
        assert gk.dropped_comments(BROKEN, patched) == ["# BROKEN: invalid pip flag (should be --no-cache-dir)"]

    @pytest.mark.parametrize(
        "response, reason",
        [
            ("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n", "targets"),
            ("Just change --no-cache-dirs to --no-cache-dir.", "neither"),
            ("```dockerfile\nFROM a\n```\n```dockerfile\nFROM b\n```", "multiple"),
            ("```dockerfile\nRUN echo no base image\n```", "no FROM"),
            ("```dockerfile\n# only a comment\nRUN echo hi\n```", "no FROM"),
            (FIX_DIFF.replace("--no-cache-dirs", "--something-else"), "does not match"),
            ("x" * (gk.MAX_RESPONSE_BYTES + 1), "size"),
        ],
        ids=["other-file", "prose-only", "two-files", "no-from", "comment-only", "stale-hunk", "oversized"],
    )
    def test_unsafe_responses_rejected(self, response, reason):
        with pytest.raises(gk.PatchError, match=reason):
            gk.sanitize_patch(response, BROKEN, "Dockerfile.broken")

    def test_original_dockerfile_untouched_until_build_passes(self, tmp_path):
        df = tmp_path / "Dockerfile.broken"
        df.write_text(BROKEN)
        backend = FakeBackend(fail_while=lambda text: BROKEN_RUN in text)
        claude = FakeClaude([f"```diff\n{FIX_DIFF}```"])
        report = gk.run_gatekeeper(df, tmp_path, backend, claude)
        assert report.success
        assert df.read_text() == BROKEN  # not --in-place
        working = tmp_path / "Dockerfile.broken.gatekeeper"
        assert FIXED_RUN in working.read_text()
        assert comment_lines(working.read_text()) == comment_lines(BROKEN)

    def test_in_place_writes_back_on_success(self, tmp_path):
        df = tmp_path / "Dockerfile.broken"
        df.write_text(BROKEN)
        backend = FakeBackend(fail_while=lambda text: BROKEN_RUN in text)
        report = gk.run_gatekeeper(df, tmp_path, backend, FakeClaude([FIX_DIFF]), in_place=True)
        assert report.success
        assert FIXED_RUN in df.read_text()
        assert not (tmp_path / "Dockerfile.broken.gatekeeper").exists()


# =============================================================================
# 3. Max retry exhaustion caps Claude calls
# =============================================================================

FAILURE = BuildFailure(category=FailureCategory.INVALID_FLAG, message="no such option: --no-cache-dirs",
                       instruction=BROKEN_RUN, dockerfile_line=20, stage="builder")


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


class FakeClaude:
    """Returns scripted responses; generates a fresh valid patch if it runs out."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.prompts: list[str] = []

    def propose_fix(self, prompt):
        self.prompts.append(prompt)
        if self.responses:
            return self.responses.pop(0)
        n = len(self.prompts)
        return f"```dockerfile\n{BROKEN.rstrip()}\n# attempt {n}\n```"


@pytest.fixture
def dockerfile(tmp_path):
    df = tmp_path / "Dockerfile.broken"
    df.write_text(BROKEN)
    return df


class TestRetryCap:
    def test_never_converging_fix_stops_after_two_claude_calls(self, dockerfile):
        backend, claude = FakeBackend(), FakeClaude()
        report = gk.run_gatekeeper(dockerfile, dockerfile.parent, backend, claude)
        assert not report.success
        assert report.claude_calls == 2 == len(claude.prompts)
        assert len(backend.calls) == 3  # initial build + one rebuild per patch
        assert report.stop_reason == "retries_exhausted"
        assert [a.number for a in report.attempts] == [0, 1, 2]

    def test_requested_retries_above_ceiling_are_capped(self, dockerfile, caplog):
        claude = FakeClaude()
        report = gk.run_gatekeeper(dockerfile, dockerfile.parent, FakeBackend(), claude, max_retries=50)
        assert report.claude_calls == gk.MAX_RETRIES == 2
        assert "capping" in caplog.text

    def test_zero_retries_never_calls_claude(self, dockerfile):
        claude = FakeClaude()
        report = gk.run_gatekeeper(dockerfile, dockerfile.parent, FakeBackend(), claude, max_retries=0)
        assert claude.prompts == []
        assert not report.success

    def test_passing_build_never_calls_claude(self, dockerfile):
        claude = FakeClaude()
        report = gk.run_gatekeeper(dockerfile, dockerfile.parent, FakeBackend(lambda t: False), claude)
        assert report.success and report.claude_calls == 0 and report.stop_reason == "passed"

    def test_success_on_first_retry_stops_spending(self, dockerfile):
        claude = FakeClaude([FIX_DIFF])
        report = gk.run_gatekeeper(dockerfile, dockerfile.parent,
                                   FakeBackend(lambda t: BROKEN_RUN in t), claude)
        assert report.success and report.claude_calls == 1
        assert FIXED_RUN in report.attempts[1].patch_diff

    def test_unusable_patch_aborts_without_second_call(self, dockerfile):
        claude = FakeClaude(["I'm not sure, try checking pip docs."])
        report = gk.run_gatekeeper(dockerfile, dockerfile.parent, FakeBackend(), claude)
        assert report.claude_calls == 1
        assert report.stop_reason.startswith("unusable_patch")

    def test_identical_patch_aborts_as_no_progress(self, dockerfile):
        claude = FakeClaude([f"```dockerfile\n{BROKEN}```"])
        report = gk.run_gatekeeper(dockerfile, dockerfile.parent, FakeBackend(), claude)
        assert report.claude_calls == 1 and report.stop_reason == "no_progress"

    def test_prompt_contains_failure_context(self, dockerfile):
        claude = FakeClaude()
        gk.run_gatekeeper(dockerfile, dockerfile.parent, FakeBackend(), claude, max_retries=1)
        prompt = claude.prompts[0]
        assert "invalid_command_flag" in prompt
        assert "line 20" in prompt and "builder" in prompt
        assert BROKEN_RUN in prompt
        assert "Keep every comment" in prompt

    def test_main_exits_1_when_retries_exhausted(self, dockerfile, monkeypatch, capsys):
        monkeypatch.setattr(gk, "preflight_daemon", lambda: None)
        monkeypatch.setattr(gk, "BuildKitCliBackend", lambda: FakeBackend())
        monkeypatch.setattr(gk, "ClaudeCli", lambda _bin, **_kw: FakeClaude())
        code = gk.main(["-f", str(dockerfile), str(dockerfile.parent)])
        assert code == 1
        assert "2 Claude call(s)" in capsys.readouterr().out

    def test_main_exits_2_when_daemon_down(self, dockerfile, monkeypatch):
        def down():
            raise gk.GatekeeperError("Docker daemon unreachable")
        monkeypatch.setattr(gk, "preflight_daemon", down)
        assert gk.main(["-f", str(dockerfile), str(dockerfile.parent)]) == 2


class TestClaudeCli:
    def test_invokes_print_mode_in_scratch_dir(self):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=FIX_DIFF, stderr="")
        with mock.patch.object(gk.shutil, "which", return_value="/usr/local/bin/claude"), \
             mock.patch.object(gk.subprocess, "run", return_value=proc) as run:
            out = gk.ClaudeCli().propose_fix("fix it")
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["claude", "-p", "fix it"]
        assert run.call_args.kwargs["cwd"] != str(Path.cwd())
        assert out == FIX_DIFF

    def test_missing_binary_is_environment_error(self):
        with mock.patch.object(gk.shutil, "which", return_value=None):
            with pytest.raises(gk.GatekeeperError, match="not found"):
                gk.ClaudeCli("definitely-not-claude").propose_fix("x")

    def test_nonzero_exit_is_environment_error(self):
        proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Invalid API key")
        with mock.patch.object(gk.shutil, "which", return_value="/x/claude"), \
             mock.patch.object(gk.subprocess, "run", return_value=proc):
            with pytest.raises(gk.GatekeeperError, match="Invalid API key"):
                gk.ClaudeCli().propose_fix("x")

    def test_error_reported_on_stdout_is_surfaced(self):
        # Print mode reports usage-limit and auth errors on stdout with an empty stderr.
        msg = "Fable 5.1 requires usage credits. Switch to another model"
        proc = subprocess.CompletedProcess(args=[], returncode=1, stdout=msg, stderr="")
        with mock.patch.object(gk.shutil, "which", return_value="/x/claude"), \
             mock.patch.object(gk.subprocess, "run", return_value=proc):
            with pytest.raises(gk.GatekeeperError, match="requires usage credits"):
                gk.ClaudeCli().propose_fix("x")

    def test_model_flag_is_passed_through(self):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        with mock.patch.object(gk.shutil, "which", return_value="/x/claude"), \
             mock.patch.object(gk.subprocess, "run", return_value=proc) as run:
            gk.ClaudeCli(model="sonnet").propose_fix("x")
            assert run.call_args.args[0][-2:] == ["--model", "sonnet"]
            gk.ClaudeCli().propose_fix("x")
            assert "--model" not in run.call_args.args[0]
