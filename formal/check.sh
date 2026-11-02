#!/bin/sh
# Model-check inbound delivery with TLC: formal/InboundWake.tla (one session is woken for its mail) and
# formal/InboundRouting.tla (shared-alias mail reaches a live carrier). Exit 0 = every property holds in the models.
# Usage: formal/check.sh [MODEL...]   (default: InboundWake InboundRouting)
# First run fetches the pinned TLA+ tools jar into local/ (gitignored). Needs java 11+ and curl.
set -eu
cd "$(dirname "$0")/.."
VER=1.7.4
SHA=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
JAR=local/tla/tla2tools-$VER.jar
if [ ! -f "$JAR" ]; then
  mkdir -p local/tla
  curl -fsSL -o "$JAR.part" "https://github.com/tlaplus/tlaplus/releases/download/v$VER/tla2tools.jar"
  mv "$JAR.part" "$JAR"
fi
if ! echo "$SHA  $JAR" | shasum -a 256 -c - >/dev/null 2>&1; then
  echo "formal/check.sh: $JAR does not match the pinned sha256; delete it and rerun" >&2
  exit 2
fi
[ $# -gt 0 ] || set -- InboundWake InboundRouting
failed=""
for model in "$@"; do
  echo "== $model"
  java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -workers auto -lncheck final -cleanup -metadir "local/tla/states/$model" \
    -config "formal/$model.cfg" "formal/$model.tla" || failed="$failed $model"
done
if [ -n "$failed" ]; then
  echo "formal/check.sh: failed:$failed" >&2
  exit 1
fi
echo "formal/check.sh: all models pass: $*"
