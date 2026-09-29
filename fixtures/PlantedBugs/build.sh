#!/usr/bin/env bash
# Build the PlantedBugs fixture for the iOS Simulator and macOS (Debug).
#
# Usage: ./build.sh [ios|macos|all]   (default: all)
#
# Outputs:
#   build/ios/PlantedBugs.app     iOS Simulator build
#   build/macos/PlantedBugs.app   macOS build, signed to run locally
# The last lines printed are the absolute .app paths, one per platform,
# as "<platform>=<path>".
#
# DerivedData lives outside the checkout (default: Xcode's DerivedData folder,
# keyed by this checkout's path; override with PLANTEDBUGS_DERIVED_DATA).
# Checkouts under an iCloud-synced ~/Documents get Finder and file-provider
# extended attributes that make codesign fail, so the build must not happen
# in place.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$HERE/PlantedBugs.xcodeproj"
SCHEME="PlantedBugs"
OUT="$HERE/build"
KEY="$(printf '%s' "$HERE" | shasum | cut -c1-10)"
DERIVED="${PLANTEDBUGS_DERIVED_DATA:-$HOME/Library/Developer/Xcode/DerivedData/PlantedBugs-$KEY}"
ARCH="$(uname -m)"
WHICH="${1:-all}"

build_one() {
  local platform="$1" destination="$2" products_subdir="$3"
  local log="$OUT/$platform-build.log"
  mkdir -p "$OUT/$platform"
  echo "Building $platform (log: $log)" >&2
  if ! xcodebuild \
      -project "$PROJECT" \
      -scheme "$SCHEME" \
      -configuration Debug \
      -destination "$destination" \
      -derivedDataPath "$DERIVED" \
      ARCHS="$ARCH" \
      ONLY_ACTIVE_ARCH=YES \
      CODE_SIGN_IDENTITY=- \
      CODE_SIGN_STYLE=Manual \
      DEVELOPMENT_TEAM= \
      build >"$log" 2>&1; then
    tail -n 40 "$log" >&2
    echo "error: $platform build failed; see $log" >&2
    exit 1
  fi
  local src="$DERIVED/Build/Products/$products_subdir/PlantedBugs.app"
  rm -rf "$OUT/$platform/PlantedBugs.app"
  ditto --noextattr --noqtn "$src" "$OUT/$platform/PlantedBugs.app"
  xattr -cr "$OUT/$platform/PlantedBugs.app" 2>/dev/null || true
}

case "$WHICH" in
  ios|macos|all) ;;
  *) echo "usage: $0 [ios|macos|all]" >&2; exit 2 ;;
esac

mkdir -p "$OUT"
if [[ "$WHICH" == ios || "$WHICH" == all ]]; then
  build_one ios "generic/platform=iOS Simulator" "Debug-iphonesimulator"
fi
if [[ "$WHICH" == macos || "$WHICH" == all ]]; then
  build_one macos "generic/platform=macOS" "Debug"
fi

if [[ "$WHICH" == ios || "$WHICH" == all ]]; then
  echo "ios=$OUT/ios/PlantedBugs.app"
fi
if [[ "$WHICH" == macos || "$WHICH" == all ]]; then
  echo "macos=$OUT/macos/PlantedBugs.app"
fi
