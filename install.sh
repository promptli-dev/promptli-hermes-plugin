#!/bin/sh
# Promptli Comms for Hermes Agent: installer.
#
#   curl -fsSL https://raw.githubusercontent.com/promptli-dev/promptli-hermes-plugin/main/install.sh | sh
#
# Installs the plugin with Hermes's own plugin manager (which prepares the promptli and promptli-gateway
# packages in Hermes's environment), enables it, then tells you to run `hermes promptli setup`.
# It never asks for or stores an API key.
#
# Environment overrides:
#   PROMPTLI_HERMES_SOURCE   what `hermes plugins install` installs (default: promptli-dev/promptli-hermes-plugin)
#   HERMES_BIN               the hermes command (default: hermes on PATH)
set -eu

SOURCE="${PROMPTLI_HERMES_SOURCE:-promptli-dev/promptli-hermes-plugin}"
HERMES="${HERMES_BIN:-hermes}"

say() { printf '\033[1;38;5;202m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$HERMES" >/dev/null 2>&1 \
  || die "Hermes Agent is not installed: see https://hermes-agent.nousresearch.com/docs/getting-started"

say "Installing the Promptli Comms plugin into Hermes ($SOURCE)"
"$HERMES" plugins install "$SOURCE" --enable --yes-deps \
  || die "install failed; try: $HERMES plugins install $SOURCE --enable"

say "Installed"
cat <<'EOF'

Next:
  hermes promptli setup      connect your agent (key in a hidden prompt), choose who may reach it
  hermes mcp login promptli  log in with your browser for the Promptli tools
  hermes gateway restart     start receiving email, texts and calls
  hermes promptli doctor     check everything is working

Anyone who learns your agent's address or number can message it. Promptli sessions only get web search
and a todo list unless you widen PROMPTLI_TOOLSETS.
EOF
