#!/usr/bin/env bash
# ci-local.sh — Reproduce the CI pipeline locally on a developer laptop.
#
# Usage:
#   bash scripts/ci-local.sh              # all steps
#   bash scripts/ci-local.sh lint          # ruff check + ruff format --check
#   bash scripts/ci-local.sh type-check    # mypy src/
#   bash scripts/ci-local.sh test-fast     # pytest -m "not slow and not non_regression"
#   bash scripts/ci-local.sh ansible-lint  # ansible-lint + yamllint
#   bash scripts/ci-local.sh docker-build  # docker build
#   bash scripts/ci-local.sh coverage      # pytest with coverage
#
# Requires: uv, ffmpeg, docker (optional for docker-build step).
# The script exits with a non-zero code on the first failure.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

step() {
    echo -e "${YELLOW}[ci-local]${NC} $*"
}

pass() {
    echo -e "${GREEN}[PASS]${NC} $*"
}

fail() {
    echo -e "${RED}[FAIL]${NC} $*"
    exit 1
}

check_binary() {
    if ! command -v "$1" &>/dev/null; then
        echo -e "${RED}Missing required binary: $1${NC}"
        echo "Install it before running this script."
        exit 1
    fi
}

# --- Pre-flight checks ---
check_binary uv
check_binary ffmpeg

# Sync dependencies once upfront
step "Installing dependencies (test group)..."
uv sync --group test
echo ""

# --- Step functions ---
do_lint() {
    step "ruff check"
    uv run ruff check . || fail "ruff check failed"
    pass "ruff check"

    step "ruff format --check"
    uv run ruff format --check . || fail "ruff format --check failed"
    pass "ruff format --check"

    step "pre-commit run --all-files"
    uv run pre-commit run --all-files || fail "pre-commit failed"
    pass "pre-commit"
}

do_type_check() {
    step "mypy src/"
    uv run mypy src/ || fail "mypy failed"
    pass "mypy"
}

do_test_fast() {
    step "pytest -m 'not slow and not non_regression'"
    uv run pytest -m "not slow and not non_regression" --tb=short -q \
        || fail "test-fast failed"
    pass "test-fast"
}

do_ansible_lint() {
    step "ansible-lint"
    uv sync --group ci 2>/dev/null
    uv run ansible-lint deploy/ansible/playbooks/ \
        || fail "ansible-lint failed"
    pass "ansible-lint"

    step "yamllint"
    uv run yamllint deploy/ansible/ \
        || fail "yamllint failed"
    pass "yamllint"
}

do_docker_build() {
    if ! command -v docker &>/dev/null; then
        echo -e "${YELLOW}[SKIP]${NC} docker not available"
        exit 0
    fi
    step "docker build"
    docker build . || fail "docker build failed"
    pass "docker build"
}

do_coverage() {
    step "pytest with coverage"
    uv run coverage run -m pytest -m "not slow and not non_regression" --tb=short -q \
        || fail "coverage run failed"
    uv run coverage report --fail-under=60 \
        || fail "coverage below threshold"
    pass "coverage"
}

# --- Dispatch ---
if [ $# -eq 0 ]; then
    do_lint
    echo ""
    do_type_check
    echo ""
    do_test_fast
    echo ""
    echo -e "${GREEN}All CI steps passed.${NC}"
else
    case "$1" in
        lint)          do_lint ;;
        type-check)    do_type_check ;;
        test-fast)     do_test_fast ;;
        ansible-lint)  do_ansible_lint ;;
        docker-build)  do_docker_build ;;
        coverage)      do_coverage ;;
        *)
            echo "Unknown step: $1"
            echo "Valid: lint, type-check, test-fast, ansible-lint, docker-build, coverage"
            exit 2
            ;;
    esac
fi
