#!/bin/bash
# Minimal pre-checkout bootstrap; mirrored verbatim in the CI tools builder.
set -euo pipefail
bootstrap_ci() {
  local attempt status log
  log=$(mktemp)
  for attempt in 1 2 3 4 5 6; do
    status=0
    timeout 90 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=30 -o APT::Update::Error-Mode=any update >"$log" 2>&1 || status=$?
    if [ "$status" = 0 ]; then
      timeout 180 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=30 --download-only install -y --no-install-recommends ca-certificates curl git libstdc++6 lsb-release python3 python3-venv >>"$log" 2>&1 || status=$?
    fi
    cat "$log"
    if [ "$status" = 0 ]; then
      DEBIAN_FRONTEND=noninteractive apt-get --no-download install -y --no-install-recommends ca-certificates curl git libstdc++6 lsb-release python3 python3-venv
      rm -f "$log"
      return
    fi
    if [ "$attempt" = 6 ] || grep -Eiq 'certificate verification failed|not signed|NO_PUBKEY|invalid signature|401 Unauthorized|403 Forbidden|no space left|unable to locate package|does not have a Release file' "$log"; then
      rm -f "$log"
      return "$status"
    fi
    if [ "$status" != 124 ] && ! grep -Eiq 'bad record mac|decryption failed|unexpected EOF|connection|temporary failure|failed to fetch|hash sum mismatch|file has unexpected size' "$log"; then
      rm -f "$log"
      return "$status"
    fi
    printf 'CI bootstrap acquisition retry %s/6\n' "$attempt"
    sleep "$((5 * attempt))"
  done
}
bootstrap_ci
