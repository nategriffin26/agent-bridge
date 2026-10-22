#!/bin/sh
# Model-check the inbound wake (formal/InboundWake.tla) with TLC. Exit 0 = the property holds in the model.
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
exec java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -workers auto -lncheck final -cleanup -metadir local/tla/states \
  -config formal/InboundWake.cfg formal/InboundWake.tla
