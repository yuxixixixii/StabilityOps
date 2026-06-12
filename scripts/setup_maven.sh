#!/usr/bin/env bash
set -euo pipefail

MAVEN_VERSION="${MAVEN_VERSION:-3.8.8}"
TOOLS_DIR="${TOOLS_DIR:-tools}"
MAVEN_DIR="${MAVEN_DIR:-$TOOLS_DIR/apache-maven-$MAVEN_VERSION}"

cd "$(dirname "$0")/.."

if [[ -x "$MAVEN_DIR/bin/mvn" ]]; then
  "$MAVEN_DIR/bin/mvn" -version
  exit 0
fi

mkdir -p "$TOOLS_DIR"
archive="$TOOLS_DIR/apache-maven-$MAVEN_VERSION-bin.tar.gz"
url="${MAVEN_URL:-https://archive.apache.org/dist/maven/maven-3/$MAVEN_VERSION/binaries/apache-maven-$MAVEN_VERSION-bin.tar.gz}"

echo "downloading Maven $MAVEN_VERSION"
echo "url=$url"
curl -L "$url" -o "$archive"
tar -xzf "$archive" -C "$TOOLS_DIR"
"$MAVEN_DIR/bin/mvn" -version
