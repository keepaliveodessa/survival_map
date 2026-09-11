#!/usr/bin/env bash
# =============================================================================
# run-ci-local.sh — Local CI/CD pipeline runner (simulates GitLab CI jobs)
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PASS=0
FAIL=0
SKIP=0
TOTAL=0

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

run_job() {
    local name="$1"
    local image="$2"
    local cmd="$3"
    TOTAL=$((TOTAL + 1))

    printf "\n${BLUE}══════════════════════════════════════════════════════════════${NC}\n"
    printf "${BLUE}  Job: %s${NC}\n" "$name"
    printf "${BLUE}══════════════════════════════════════════════════════════════${NC}\n"

    if docker run --rm \
        -v "$PROJECT_DIR:/workspace" \
        -w /workspace \
        --network host \
        "$image" \
        sh -c "$cmd" 2>&1; then
        printf "${GREEN}[PASS]${NC} %s\n" "$name"
        PASS=$((PASS + 1))
        return 0
    else
        printf "${RED}[FAIL]${NC} %s\n" "$name"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1: security-scan
# ═══════════════════════════════════════════════════════════════════════════════
printf "\n${YELLOW}▶ STAGE: security-scan${NC}\n"

run_job "bandit-scan" "python:3.11-slim-bookworm" \
    "pip install --quiet bandit==1.7.10 && \
     bandit -r core/ processor/ parser/ -ll -x '*/tests/*,scripts,processor/models' && \
     bandit -ll core/utils/validators.py"

run_job "pip-audit" "python:3.11-slim-bookworm" \
    "pip install --quiet pip-audit==2.7.3 && \
     pip-audit -r requirements.txt && \
     pip-audit -r processor/requirements.txt && \
     pip-audit -r parser/requirements.txt && \
     pip-audit -r requirements-dev.txt"

run_job "hadolint" "hadolint/hadolint:v2.12.0-alpine" \
    "hadolint --failure-threshold warning Dockerfile.core && \
     hadolint --failure-threshold warning Dockerfile.processor && \
     hadolint --failure-threshold warning Dockerfile.parser && \
     hadolint --failure-threshold warning Dockerfile.web && \
     hadolint --failure-threshold warning Dockerfile.postgres"

run_job "frontend-security" "node:22-alpine" \
    "cd web && npm ci --no-audit --no-fund && \
     npx eslint js --ext .ts,.js --no-error-on-unmatched-pattern --quiet && \
     npx eslint js --ext .ts,.js --rule '{\"security/detect-unsafe-regex\": \"error\", \"security/detect-non-buffer-require\": \"error\", \"security/detect-new-buffer\": \"error\", \"security/detect-buffer-noassert\": \"error\"}' --no-error-on-unmatched-pattern --quiet"

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2: test
# ═══════════════════════════════════════════════════════════════════════════════
printf "\n${YELLOW}▶ STAGE: test${NC}\n"

run_job "backend-tests" "python:3.11-slim-bookworm" \
    "pip install --quiet -r requirements.txt -r parser/requirements.txt -r requirements-dev.txt && \
     pytest -q"

run_job "parser-length-filter" "python:3.11-slim-bookworm" \
    "pip install --quiet -r requirements.txt -r parser/requirements.txt -r requirements-dev.txt && \
     pytest tests/test_parser_length_filter.py -v"

run_job "test:settings-strict-bool" "python:3.11-slim-bookworm" \
    "pip install --quiet -r requirements-dev.txt environs==11.2.1 pytest pytest-asyncio && \
     pytest -q tests/test_settings_strict_bool.py --tb=short"

# Matrix: 4 combos
MATRIX_COMBOS=(
    'UNSET True false'
    'false False true'
    'fals True false'
    'true True false'
)

for combo in "${MATRIX_COMBOS[@]}"; do
    read -r TEST_ENV_VALUE EXPECTED_BOOL EXPECTED_LOG_WARNING <<< "$combo"
    run_job "test:core-startup-matrix [${TEST_ENV_VALUE}]" "python:3.11-slim-bookworm" \
        "pip install --quiet environs==11.2.1 && \
         python scripts/ci_check_webview_validation.py '${TEST_ENV_VALUE}' '${EXPECTED_BOOL}' '${EXPECTED_LOG_WARNING}'"
done

run_job "frontend-build" "node:22-alpine" \
    "cd web && npm ci --no-audit --no-fund && \
     npm run typecheck && \
     npm run build && \
     for f in dist/js/common.js dist/js/core/store.js dist/js/core/ui.js dist/js/core/websocket.js; do \
         test -s \"\$f\" || { echo \"Build verification failed: \$f missing or empty\"; exit 1; }; \
     done"

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3: image-security
# ═══════════════════════════════════════════════════════════════════════════════
printf "\n${YELLOW}▶ STAGE: image-security${NC}\n"

run_job "trivy-scan" "aquasec/trivy:latest" \
    "trivy fs --severity HIGH,CRITICAL --exit-code 1 --scanners vuln,config ."

# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════
printf "\n${BLUE}══════════════════════════════════════════════════════════════${NC}\n"
printf "${BLUE}  PIPELINE SUMMARY${NC}\n"
printf "${BLUE}══════════════════════════════════════════════════════════════${NC}\n"
printf "  Total:  %d\n" "$TOTAL"
printf "  Passed: ${GREEN}%d${NC}\n" "$PASS"
printf "  Failed: ${RED}%d${NC}\n" "$FAIL"
printf "${BLUE}══════════════════════════════════════════════════════════════${NC}\n"

if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
