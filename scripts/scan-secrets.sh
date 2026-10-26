#!/usr/bin/env bash
set -euo pipefail

umask 077
scan_base="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
scan_dir="$(mktemp -d "$scan_base/lra-secrets.XXXXXX")"
trap 'rm -rf "$scan_dir"' EXIT
printf '[extend]\nuseDefault = true\n' >"$scan_dir/config.toml"
touch "$scan_dir/empty.gitleaksignore"
scan() {
  if ! mise --no-config --no-env --no-hooks exec aqua:gitleaks/gitleaks@8.30.1 -- \
    gitleaks "$@" --config "$scan_dir/config.toml" \
    --gitleaks-ignore-path "$scan_dir/empty.gitleaksignore" \
    --redact=100 --ignore-gitleaks-allow --max-archive-depth=3 \
    --max-decode-depth=5 --no-banner --no-color --log-level=error \
    >"$scan_dir/result.log" 2>&1; then
    echo '::error::Secret scan failed. Review the candidate with a private local scan.'
    exit 1
  fi
}
if (($#)); then
  for target in "$@"; do
    scan dir "$target"
  done
else
  scan dir .
  scan git . --log-opts='--all --full-history'
fi
echo 'No secrets detected.'
