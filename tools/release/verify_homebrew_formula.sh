#!/usr/bin/env bash
# Prove a generated Homebrew formula installs and runs before anyone pulls it.
#
#   tools/release/verify_homebrew_formula.sh path/to/sibyl.rb
#
# It installs the formula from a throwaway local tap with the runner's
# Homebrew (Linuxbrew on GitHub's Ubuntu images), runs the formula's own
# test block, and runs the installed CLI.
set -euo pipefail

formula="${1:?usage: verify_homebrew_formula.sh path/to/sibyl.rb}"
if [[ ! -f "$formula" ]]; then
  echo "::error::formula not found: $formula" >&2
  exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
  for prefix in /home/linuxbrew/.linuxbrew /opt/homebrew /usr/local; do
    if [[ -x "$prefix/bin/brew" ]]; then
      eval "$("$prefix/bin/brew" shellenv)"
      break
    fi
  done
fi
command -v brew >/dev/null 2>&1 || { echo "::error::Homebrew is not installed" >&2; exit 1; }

export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_ENV_HINTS=1

tap="sibyl-verify/local"
brew untap "$tap" >/dev/null 2>&1 || true
brew tap-new --no-git "$tap" >/dev/null
cp "$formula" "$(brew --repository "$tap")/Formula/sibyl.rb"

brew install --formula "$tap/sibyl"
brew test "$tap/sibyl"
"$(brew --prefix)/bin/sibyl" --version
