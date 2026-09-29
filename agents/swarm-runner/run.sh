#!/usr/bin/env bash
# Start the swarm runner from a build made by build.sh. Runs in the foreground
# until killed; the runner serves on 127.0.0.1:<port>.
#
#   agents/swarm-runner/run.sh ios <simulator-udid> [port]
#   agents/swarm-runner/run.sh macos [port]
#
# Environment:
#   SWARM_RUNNER_DERIVED_DATA  where build.sh put the products (default ~/.aqa/swarm-runner/DerivedData)
#   SWARM_RUNNER_XCTESTRUN     explicit .xctestrun path (skips the lookup)
#   SWARM_RUNNER_RESULT_DIR    where xcodebuild writes result bundles (default <derived>/../results)
#
# The port reaches the test process as SWARM_RUNNER_PORT through xcodebuild's
# TEST_RUNNER_ prefix: every TEST_RUNNER_<NAME> variable in xcodebuild's
# environment is passed to the test runner as <NAME>, on the simulator too.
# Wait for readiness by polling GET /health, or watch stdout for the line
# "SWARM_RUNNER_READY port=<port>".
set -euo pipefail

DERIVED="${SWARM_RUNNER_DERIVED_DATA:-$HOME/.aqa/swarm-runner/DerivedData}"
PLATFORM="${1:-}"

usage() {
  echo "usage: $0 ios <simulator-udid> [port] | $0 macos [port]" >&2
  exit 2
}

case "$PLATFORM" in
  ios)
    UDID="${2:-}"; [ -n "$UDID" ] || usage
    PORT="${3:-8765}"
    SCHEME=SwarmRunner-iOS; SDK=iphonesimulator; BUNDLE=SwarmRunnerUITests-iOS
    DESTINATION="platform=iOS Simulator,id=$UDID"
    ;;
  macos)
    PORT="${2:-8765}"
    SCHEME=SwarmRunner-macOS; SDK=macosx; BUNDLE=SwarmRunnerUITests-macOS
    DESTINATION="platform=macOS,arch=$(uname -m)"
    ;;
  *) usage ;;
esac

XCTESTRUN="${SWARM_RUNNER_XCTESTRUN:-$(ls -t "$DERIVED"/Build/Products/"$SCHEME"_*"$SDK"*.xctestrun 2>/dev/null | head -1)}"
if [ -z "$XCTESTRUN" ] || [ ! -f "$XCTESTRUN" ]; then
  echo "no .xctestrun for $SCHEME under $DERIVED/Build/Products; run build.sh $PLATFORM first" >&2
  exit 1
fi

RESULTS="${SWARM_RUNNER_RESULT_DIR:-$(dirname "$DERIVED")/results}"
mkdir -p "$RESULTS"
RESULT_BUNDLE="$RESULTS/$PLATFORM-$PORT-$(date +%Y%m%d-%H%M%S)-$$.xcresult"

echo "swarm runner: $PLATFORM on port $PORT ($XCTESTRUN)" >&2
export TEST_RUNNER_SWARM_RUNNER_PORT="$PORT"
exec xcodebuild test-without-building \
  -xctestrun "$XCTESTRUN" \
  -destination "$DESTINATION" \
  -only-testing "$BUNDLE/SwarmRunnerTests/testRunServer" \
  -resultBundlePath "$RESULT_BUNDLE" \
  -test-timeouts-enabled NO \
  -collect-test-diagnostics never
