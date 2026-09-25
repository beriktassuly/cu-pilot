#!/usr/bin/env bash
# Isolated Linux/WSL bootstrap and process lifecycle for the reference app.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
TOOLS="$ROOT/work/payout-tools"
STATE="$ROOT/artifacts/payouts"
NODE_VERSION=24.14.1
AGAVE_VERSION=3.1.10
PLATFORM_VERSION=v1.52
export NO_DNA=1
export PATH="$TOOLS/node/bin:$TOOLS/solana-release/bin:$TOOLS/cargo/bin:$PATH"
if [[ -d "$TOOLS/rustup" ]]; then export RUSTUP_HOME="$TOOLS/rustup" CARGO_HOME="$TOOLS/cargo"; fi

download() { curl --proto '=https' --tlsv1.2 -fL --retry 3 --connect-timeout 20 "$1" -o "$2"; }
check_linux() { [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || { echo 'Use Linux x86_64 or Windows WSL Ubuntu 24.04.' >&2; exit 1; }; }
node_bin() { command -v node >/dev/null || { echo 'Run bash scripts/payouts.sh bootstrap first.' >&2; exit 1; }; }

bootstrap() {
  check_linux
  mkdir -p "$TOOLS"
  if [[ ! -x "$TOOLS/node/bin/node" ]]; then
    local archive="node-v$NODE_VERSION-linux-x64.tar.xz"
    download "https://nodejs.org/dist/v$NODE_VERSION/$archive" "$TOOLS/$archive"
    download "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt" "$TOOLS/SHASUMS256.txt"
    (cd "$TOOLS"; grep "  $archive\$" SHASUMS256.txt | sha256sum -c -)
    mkdir -p "$TOOLS/node"
    tar -xJf "$TOOLS/$archive" --strip-components=1 -C "$TOOLS/node"
  fi
  if [[ ! -x "$TOOLS/solana-release/bin/cargo-build-sbf" ]]; then
    download "https://github.com/anza-xyz/agave/releases/download/v$AGAVE_VERSION/solana-release-x86_64-unknown-linux-gnu.tar.bz2" "$TOOLS/solana-release.tar.bz2"
    tar -xjf "$TOOLS/solana-release.tar.bz2" -C "$TOOLS"
  fi
  if ! command -v cargo >/dev/null; then
    download https://static.rust-lang.org/rustup/archive/1.28.2/x86_64-unknown-linux-gnu/rustup-init "$TOOLS/rustup-init"
    chmod +x "$TOOLS/rustup-init"
    export CARGO_HOME="$TOOLS/cargo" RUSTUP_HOME="$TOOLS/rustup"
    "$TOOLS/rustup-init" -y --no-modify-path --profile minimal --default-toolchain 1.95.0
  fi
  local uv_bin
  if command -v uv >/dev/null; then uv_bin=$(command -v uv)
  elif [[ -x "$HOME/.cargo/bin/uv" ]]; then uv_bin="$HOME/.cargo/bin/uv"
  else
    download https://github.com/astral-sh/uv/releases/download/0.12.7/uv-x86_64-unknown-linux-gnu.tar.gz "$TOOLS/uv.tar.gz"
    tar -xzf "$TOOLS/uv.tar.gz" -C "$TOOLS"
    uv_bin="$TOOLS/uv-x86_64-unknown-linux-gnu/uv"
  fi
  "$uv_bin" sync --dev --locked --python 3.12
  npm --prefix typescript ci
  npm --prefix tests/integration/runtime ci
  npm --prefix typescript run build
  "$TOOLS/solana-release/bin/cargo-build-sbf" --install-only --no-rustup-override --tools-version "$PLATFORM_VERSION" || {
    # Older distribution rustup cannot register the numbered custom name.
    [[ -x "$HOME/.cache/solana/$PLATFORM_VERSION/platform-tools/rust/bin/rustc" ]] || return 1
  }
  node --version
  cargo-build-sbf --version
  .venv/bin/python --version
}

build() {
  check_linux
  local compiler="$HOME/.cache/solana/$PLATFORM_VERSION/platform-tools/rust/bin/rustc"
  [[ -x "$compiler" ]] || { echo 'Missing platform-tools. Run bootstrap first.' >&2; exit 1; }
  cargo test --locked --manifest-path programs/payout_queue/Cargo.toml
  RUSTC="$compiler" cargo-build-sbf --no-rustup-override --tools-version "$PLATFORM_VERSION" \
    --manifest-path programs/payout_queue/Cargo.toml \
    --sbf-out-dir programs/payout_queue/target/deploy -- --locked
  test -s programs/payout_queue/target/deploy/payout_queue.so
}

stop() {
  local pid
  if [[ -f "$STATE/bridge.pid" ]]; then read -r pid < "$STATE/bridge.pid"
  elif [[ -f "$STATE/runtime.json" ]]; then pid=$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$STATE/runtime.json")
  else echo 'No managed payout runtime.'; return
  fi
  [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] || { echo 'Invalid managed process identity.' >&2; exit 1; }
  if kill -0 "$pid" 2>/dev/null && [[ "$(cut -d' ' -f3 "/proc/$pid/stat")" != Z ]]; then
    # A recycled PID must never terminate an unrelated process.
    [[ -r "/proc/$pid/cmdline" ]] || { echo 'Cannot verify process identity.' >&2; exit 1; }
    .venv/bin/python - "$pid" "$ROOT" "$STATE" <<'PY' || { echo 'Process identity changed; refusing to stop it.' >&2; exit 1; }
import pathlib, sys
pid, root, state = sys.argv[1:]
proc = pathlib.Path('/proc') / pid
cwd = (proc / 'cwd').resolve()
argv = (proc / 'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
valid = len(argv) in (3, 4) and argv[2] == 'serve'
valid = valid and (cwd / argv[1]).resolve() == pathlib.Path(root) / 'apps/payout-demo/bridge.mjs'
if len(argv) == 3:
    valid = valid and cwd == pathlib.Path(root)
elif len(argv) == 4:
    valid = valid and (cwd / argv[3]).resolve() == pathlib.Path(state) / 'runtime.json'
sys.exit(0 if valid else 1)
PY
    kill -TERM "$pid"
    for _ in {1..100}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    if kill -0 "$pid" 2>/dev/null && [[ "$(cut -d' ' -f3 "/proc/$pid/stat")" != Z ]]; then echo 'Runtime did not stop cleanly.' >&2; exit 1; fi
  fi
  rm -f -- "$STATE/bridge.pid" "$STATE/runtime.json"
  echo 'Local runtime stopped. The emulator state and ephemeral keys ended with it.'
}

start() {
  node_bin
  [[ -s programs/payout_queue/target/deploy/payout_queue.so ]] || { echo 'Build the program first.' >&2; exit 1; }
  mkdir -p "$STATE"
  if [[ -f "$STATE/bridge.pid" || -f "$STATE/runtime.json" ]]; then echo 'A runtime connection is already recorded; use info or stop.' >&2; exit 1; fi
  umask 077
  nohup node "$ROOT/apps/payout-demo/bridge.mjs" serve "$STATE/runtime.json" > "$STATE/bridge.log" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" > "$STATE/bridge.pid"
  for _ in {1..300}; do
    if [[ -f "$STATE/runtime.json" ]]; then echo "Local runtime ready; private connection: $STATE/runtime.json"; return; fi
    kill -0 "$pid" 2>/dev/null || { echo "Runtime failed; inspect $STATE/bridge.log" >&2; rm -f "$STATE/bridge.pid"; return 1; }
    sleep 0.1
  done
  echo 'Runtime startup timed out.' >&2
  stop
  return 1
}

case "${1:-help}" in
  bootstrap) bootstrap ;;
  build) build ;;
  start) start ;;
  serve) node_bin; mkdir -p "$STATE"; exec node "$ROOT/apps/payout-demo/bridge.mjs" serve "$STATE/runtime.json" ;;
  stop) stop ;;
  reset)
    stop
    [[ "$(realpath -m "$STATE")" == "$ROOT/artifacts/payouts" && ! -L "$ROOT/artifacts" && ! -L "$STATE" ]] || { echo 'Unexpected reset path.' >&2; exit 1; }
    rm -rf -- "$STATE"
    echo 'Only this application state was reset.'
    ;;
  test) node_bin; node tests/integration/payout_program.mjs ;;
  upgrade-test) node_bin; exec .venv/bin/python -m examples.payouts.deployment_check ;;
  info|collect|create|qualify|run|step) command=$1; shift; exec .venv/bin/python -m examples.payouts.app "$command" "$@" ;;
  train) shift; exec .venv/bin/python -m examples.payouts.model train "$STATE/observations.jsonl" --output "$STATE/candidate.json" --report "$STATE/estimates.json" --baselines "$STATE/baselines.json" "$@" ;;
  benchmark) shift; exec .venv/bin/python -m examples.payouts.benchmark "$@" ;;
  demo) shift; exec .venv/bin/python -m examples.payouts.server "$@" ;;
  *) echo 'Usage: bash scripts/payouts.sh bootstrap|build|start|serve|stop|reset|test|upgrade-test|info|collect|train|qualify|create|run|step|benchmark|demo [arguments]' ;;
esac
