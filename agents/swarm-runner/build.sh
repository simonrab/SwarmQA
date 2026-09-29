#!/usr/bin/env bash
# Build the swarm runner UI-test bundles for testing, without a signing team.
#
#   agents/swarm-runner/build.sh [ios|macos|all]   (default: all)
#
# Products go to $SWARM_RUNNER_DERIVED_DATA (default ~/.aqa/swarm-runner/DerivedData).
# The default is outside the repo on purpose: a checkout under ~/Documents or
# ~/Desktop synced by iCloud gets Finder metadata on every file, and codesign
# then refuses the bundles ("resource fork, Finder information, or similar
# detritus not allowed").
# On success the script prints one line per platform on stdout:
#   XCTESTRUN ios /abs/path/SwarmRunner-iOS_SwarmRunner-iOS_iphonesimulator26.5-arm64.xctestrun
#   XCTESTRUN macos /abs/path/SwarmRunner-macOS_SwarmRunner-macOS_macosx26.5-arm64.xctestrun
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DERIVED="${SWARM_RUNNER_DERIVED_DATA:-$HOME/.aqa/swarm-runner/DerivedData}"
CONFIG="${SWARM_RUNNER_CONFIGURATION:-Release}"
WHICH="${1:-all}"
LOG_DIR="$(dirname "$DERIVED")/logs"
mkdir -p "$LOG_DIR"

build() {
  local scheme="$1" destination="$2" log="$3"
  echo "building $scheme ($destination), log: $log" >&2
  # Simulator builds sign ad hoc; macOS signs to run locally ("-"), so no team is needed.
  if ! xcodebuild build-for-testing \
      -project "$HERE/SwarmRunner.xcodeproj" \
      -scheme "$scheme" \
      -configuration "$CONFIG" \
      -destination "$destination" \
      -derivedDataPath "$DERIVED" \
      CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM= \
      >"$log" 2>&1; then
    grep -E "error:|BUILD FAILED|FAILED" "$log" >&2 || tail -40 "$log" >&2
    echo "build failed; full log: $log" >&2
    exit 1
  fi
}

xctestrun_for() {
  # Newest .xctestrun for the scheme and SDK.
  ls -t "$DERIVED"/Build/Products/"$1"_*"$2"*.xctestrun 2>/dev/null | head -1
}

build_ios() {
  build SwarmRunner-iOS "generic/platform=iOS Simulator" "$LOG_DIR/build-ios.log"
  echo "XCTESTRUN ios $(xctestrun_for SwarmRunner-iOS iphonesimulator)"
}

build_macos() {
  build SwarmRunner-macOS "platform=macOS" "$LOG_DIR/build-macos.log"
  echo "XCTESTRUN macos $(xctestrun_for SwarmRunner-macOS macosx)"
}

case "$WHICH" in
  ios) build_ios ;;
  macos) build_macos ;;
  all) build_ios; build_macos ;;
  *) echo "usage: $0 [ios|macos|all]" >&2; exit 2 ;;
esac
