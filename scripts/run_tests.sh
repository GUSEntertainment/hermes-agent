#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.hermes/...)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp_adapter/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Locate python ───────────────────────────────────────────────────────────
# Probe local venvs first; fall back to the Nix devShell's editable venv
# (HERMES_PYTHON is exported by the devShell hook and ships [dev] extras:
# pytest, pytest-asyncio, pytest-timeout, ruff, ty).
#
# A candidate must have pytest INSTALLED, not merely exist. The release venv
# at ~/.hermes/hermes-agent/venv has bin/activate but no pytest, so an
# existence-only probe selected it in checkouts/worktrees without a local
# .venv — every file then died with "No module named pytest" and the run
# reported "0 tests passed" (which reads green at a glance even though the
# exit code is 1). Skip such a venv and keep probing instead.
VENV=""
VENV_PYTHON=""
SKIPPED_VENVS=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.hermes/hermes-agent/venv"; do
  if [ -f "$candidate/bin/activate" ]; then
    if "$candidate/bin/python" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/bin/python"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
  # Native Windows venv layout: python.exe and activate live under
  # Scripts/, and there is no bin/. Anyone running this script from
  # Git Bash / MSYS with a `python -m venv`- or uv-created venv hits
  # this branch — without it the canonical runner refuses to start.
  if [ -f "$candidate/Scripts/activate" ]; then
    if "$candidate/Scripts/python.exe" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/Scripts/python.exe"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
done

if [ -n "$SKIPPED_VENVS" ]; then
  for skipped in $SKIPPED_VENVS; do
    echo "▶ skipping venv without pytest: $skipped" >&2
  done
fi

if [ -n "$VENV" ]; then
  PYTHON="$VENV_PYTHON"
elif [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ] \
    && "$HERMES_PYTHON" -c 'import pytest' 2>/dev/null; then
  # Guard with an import check: HERMES_PYTHON may point at the RELEASE
  # venv (no pytest) when inherited from a wrapped `hermes` binary rather
  # than the devShell hook.
  PYTHON="$HERMES_PYTHON"
  echo "▶ no local venv — using Nix dev venv via HERMES_PYTHON: $PYTHON"
else
  echo "error: no virtualenv with pytest found in $REPO_ROOT/.venv or $REPO_ROOT/venv," >&2
  echo "       and HERMES_PYTHON is not a python with pytest (enter the Nix devShell or create a venv)" >&2
  if [ -n "$SKIPPED_VENVS" ]; then
    echo "       (skipped for missing pytest:$SKIPPED_VENVS — install dev extras there, or create $REPO_ROOT/.venv)" >&2
  fi
  exit 1
fi

# ── Suite-scoped state boundary (before collection/import) ─────────────────
# Fixtures are too late: pytest imports conftest and test modules during
# collection. Allocate one random marker-owned HERMES_HOME here and inject it
# into the runner plus every descendant. Never honor a caller's live/custom
# HERMES_HOME as the suite root.
INHERITED_HERMES_HOME="${HERMES_HOME:-}"
DEFAULT_LIVE_HERMES_HOME="$HOME/.hermes"
ISOLATION_HELPER="$SCRIPT_DIR/test_suite_isolation.py"
CREATE_ARGS=(create --base "${TMPDIR:-/tmp}" --forbid "$DEFAULT_LIVE_HERMES_HOME")
if [ -n "$INHERITED_HERMES_HOME" ]; then
  CREATE_ARGS+=(--forbid "$INHERITED_HERMES_HOME")
fi
IFS=$'\t' read -r SUITE_HERMES_HOME SUITE_OWNER_TOKEN < <(
  "$PYTHON" "$ISOLATION_HELPER" "${CREATE_ARGS[@]}"
)
if [ -z "$SUITE_HERMES_HOME" ] || [ -z "$SUITE_OWNER_TOKEN" ]; then
  echo "error: failed to allocate marker-owned suite HERMES_HOME" >&2
  exit 1
fi

cleanup_suite_home() {
  local rc=$?
  local cleanup_args=(cleanup "$SUITE_HERMES_HOME" --token-fd 3 --forbid "$DEFAULT_LIVE_HERMES_HOME")
  if [ -n "$INHERITED_HERMES_HOME" ]; then
    cleanup_args+=(--forbid "$INHERITED_HERMES_HOME")
  fi
  # Cleanup authority stays in this parent shell.  It is neither exported to
  # test descendants nor exposed in the helper's argv; fd 3 exists only for
  # this cleanup invocation.  The suite marker contains only its digest.
  if ! "$PYTHON" "$ISOLATION_HELPER" "${cleanup_args[@]}" 3<<<"$SUITE_OWNER_TOKEN"; then
    echo "error: refusing unsafe suite-home cleanup; preserved $SUITE_HERMES_HOME" >&2
    rc=1
  fi
  trap - EXIT
  exit "$rc"
}
trap cleanup_suite_home EXIT
# Bash does not consistently run an EXIT trap when an unhandled signal arrives
# while it is waiting for a foreground child.  Convert catchable termination
# signals to ordinary exits so the authenticated cleanup path always runs.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi
TEST_PYTHONPATH="$SCRIPT_DIR/test_site"
if [ -n "$EXTRA_PYTHONPATH" ]; then
  TEST_PYTHONPATH="$TEST_PYTHONPATH:$EXTRA_PYTHONPATH"
fi


# ── Windows location variables (computed before we drop env) ───────────────
# `env -i` forwards HOME, which is enough on POSIX. Native Windows CPython
# resolves Path.home() from USERPROFILE (or HOMEDRIVE+HOMEPATH), stdlib
# platform paths come from LOCALAPPDATA/APPDATA, ssl/sockets need SYSTEMROOT,
# and tempfile needs TEMP/TMP. Dropping them breaks collection on native
# Windows (issues #67385, #70813). These are location variables, not
# credentials, so forwarding them keeps the isolation intent intact. Each is
# only forwarded when actually set, so POSIX runs are byte-for-byte unchanged.
WIN_ENV=()
for _win_var in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP; do
  if [ -n "${!_win_var:-}" ]; then
    WIN_ENV+=("$_win_var=${!_win_var}")
  fi
done

# ── Test-runner knobs (computed before we drop env) ────────────────────────
# The runner's own documented environment knobs must survive the hermetic
# `env -i` below, or they are silent no-ops for anyone invoking this script:
#
#   * HERMES_TEST_WORKERS / PATHS / FILE_TIMEOUT / FILE_RETRIES / SLICE are
#     read by run_tests_parallel.py at argparse-default time — inside the
#     stripped environment.
#   * HERMES_TEST_IMAGE is read by tests/docker/conftest.py to skip its
#     session-scoped `docker build`. CI's docker.yml sets it to the image
#     the build step just loaded; stripping it made every per-file pytest
#     subprocess rebuild the 5GB image from a cold builder cache instead
#     (~4 min per worker per run, and the rebuilt image lacked the
#     HERMES_GIT_SHA build-arg the workflow bakes in).
#   * HERMES_E2E_REQUIRE_TUI turns a missing Ink TUI build into a failure in
#     tests/e2e/core/terminal instead of a skip (set by the e2e CI job).
#   * CI / GITHUB_ACTIONS tell suites they run on a disposable runner (e.g.
#     tests/e2e/core/upgrade runs the real updater unsandboxed only there).
#
# These are test-infrastructure knobs, not credentials — same class as the
# HERMES_RUN_SLOW_PET_TESTS / HERMES_E2E_BROWSER / HERMES_RUN_E2E opt-ins
# forwarded below.
# Keep this an explicit allowlist (no HERMES_TEST_* glob) so the "no
# credential can leak" property stays auditable at a glance.
TEST_ENV=()
for _test_var in HERMES_TEST_IMAGE HERMES_TEST_WORKERS HERMES_TEST_PATHS \
  HERMES_TEST_FILE_TIMEOUT HERMES_TEST_FILE_RETRIES HERMES_TEST_SLICE \
  HERMES_GATEWAY_LOCK_DIR HERMES_E2E_REQUIRE_TUI CI GITHUB_ACTIONS; do
  if [ -n "${!_test_var:-}" ]; then
    TEST_ENV+=("$_test_var=${!_test_var}")
  fi
done

# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"
echo "  (suite HERMES_HOME=$SUITE_HERMES_HOME; service control blocked)"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ launching test runner"
TEST_NAMESPACE_HELPER=""
if [ "$(uname -s)" = "Linux" ]; then
  if command -v unshare >/dev/null 2>&1 \
      && unshare --user --map-root-user --mount true 2>/dev/null; then
    TEST_NAMESPACE_HELPER="$SCRIPT_DIR/run_tests_namespace.sh"
  elif [ -d /run/service ] || [ -d /run/s6/basedir ]; then
    echo "error: hosted service control is visible but a private test namespace cannot be created" >&2
    echo "       refusing to collect tests against the live control plane" >&2
    exit 1
  fi
fi
set +e
env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  USER="${USER:-$(id -un)}" \
  LOGNAME="${LOGNAME:-${USER:-$(id -un)}}" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  ${TEST_ENV[@]+"${TEST_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONUTF8=1 \
  HERMES_HOME="$SUITE_HERMES_HOME" \
  HERMES_TEST_ISOLATION="$SUITE_HERMES_HOME" \
  HERMES_TEST_NO_SERVICE_CONTROL=1 \
  PYTEST_ADDOPTS="--ignore=.venv --ignore=venv${PYTEST_ADDOPTS:+ $PYTEST_ADDOPTS}" \
  ${TEST_NAMESPACE_HELPER:+HERMES_TEST_NAMESPACE_HELPER="$TEST_NAMESPACE_HELPER"} \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${HERMES_E2E_BROWSER:+HERMES_E2E_BROWSER="$HERMES_E2E_BROWSER"} \
  ${HERMES_RUN_E2E:+HERMES_RUN_E2E="$HERMES_RUN_E2E"} \
  PYTHONPATH="$TEST_PYTHONPATH" \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
RUNNER_RC=$?
set -e
exit "$RUNNER_RC"
