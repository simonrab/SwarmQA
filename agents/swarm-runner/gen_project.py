#!/usr/bin/env python3
"""Generate SwarmRunner.xcodeproj without xcodegen or tuist.

Run this after adding or removing a source file:

    python3 agents/swarm-runner/gen_project.py

The output (project.pbxproj plus two shared schemes) is committed, so
building does not need this script. Object ids are derived from names, so
re-running it on an unchanged file list produces an identical project.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / "SwarmRunner.xcodeproj"

SHARED_RUNNER = [
    "Sources/Runner/SwarmRunnerTests.swift",
    "Sources/Runner/HTTPServer.swift",
    "Sources/Runner/Runner.swift",
    "Sources/Runner/Snapshot.swift",
    "Sources/Runner/JSON.swift",
    "Sources/Runner/Imaging.swift",
]
HOST_APP = ["Sources/HostApp/HostApp.swift"]

PLATFORMS = {
    "iOS": {
        "sdk": "iphoneos",
        "runner_extra": ["Sources/Runner/iOS/PlatformIOS.swift"],
        "settings": {
            "SDKROOT": "iphoneos",
            "SUPPORTED_PLATFORMS": "iphoneos iphonesimulator",
            "IPHONEOS_DEPLOYMENT_TARGET": "16.0",
            "TARGETED_DEVICE_FAMILY": '"1,2"',
            "LD_RUNPATH_SEARCH_PATHS": '"$(inherited) @executable_path/Frameworks @loader_path/Frameworks"',
        },
        "app_settings": {
            "INFOPLIST_KEY_UILaunchScreen_Generation": "YES",
            "INFOPLIST_KEY_UIApplicationSceneManifest_Generation": "YES",
        },
    },
    "macOS": {
        "sdk": "macosx",
        "runner_extra": ["Sources/Runner/macOS/PlatformMacOS.swift"],
        "settings": {
            "SDKROOT": "macosx",
            "SUPPORTED_PLATFORMS": "macosx",
            "MACOSX_DEPLOYMENT_TARGET": "13.0",
            "ENABLE_HARDENED_RUNTIME": "NO",
            "LD_RUNPATH_SEARCH_PATHS": '"$(inherited) @executable_path/../Frameworks @loader_path/../Frameworks"',
        },
        "app_settings": {},
        "runner_settings": {
            "CODE_SIGN_ENTITLEMENTS": "Sources/Runner/macOS/SwarmRunnerUITests-macOS.entitlements",
        },
    },
}

COMMON_TARGET_SETTINGS = {
    "SWIFT_VERSION": "5.0",
    "CODE_SIGN_STYLE": "Manual",
    "CODE_SIGN_IDENTITY": '"-"',
    "DEVELOPMENT_TEAM": '""',
    "GENERATE_INFOPLIST_FILE": "YES",
    "CURRENT_PROJECT_VERSION": "1",
    "MARKETING_VERSION": "1.0",
    "SWIFT_EMIT_LOC_STRINGS": "NO",
}


def oid(*parts: str) -> str:
    return hashlib.md5("/".join(parts).encode()).hexdigest()[:24].upper()


def quote(value: str) -> str:
    if value.startswith('"') or re.fullmatch(r"[A-Za-z0-9_./$]+", value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def fmt_settings(settings: dict[str, str], indent: str) -> str:
    return "".join(f"{indent}{k} = {quote(v)};\n" for k, v in sorted(settings.items()))


def main() -> None:
    objects: dict[str, list[str]] = {}

    def add(section: str, text: str) -> None:
        objects.setdefault(section, []).append(text)

    all_sources = SHARED_RUNNER + HOST_APP + [f for p in PLATFORMS.values() for f in p["runner_extra"]]
    file_ids = {path: oid("file", path) for path in all_sources}
    for path, fid in file_ids.items():
        name = Path(path).name
        add(
            "PBXFileReference",
            f'\t\t{fid} /* {name} */ = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; '
            f'path = {path}; sourceTree = SOURCE_ROOT; }};\n',
        )

    targets: list[str] = []
    product_refs: list[str] = []
    config_lists: list[tuple[str, str, dict[str, str], dict[str, str]]] = []

    for plat, spec in PLATFORMS.items():
        host = f"SwarmRunnerHost-{plat}"
        tests = f"SwarmRunnerUITests-{plat}"
        base = {**COMMON_TARGET_SETTINGS, **spec["settings"]}
        for target, kind in ((host, "app"), (tests, "uitest")):
            tid = oid("target", target)
            targets.append(tid)
            product_name = target + (".app" if kind == "app" else ".xctest")
            pid = oid("product", target)
            product_refs.append(pid)
            ftype = "wrapper.application" if kind == "app" else "wrapper.cfbundle"
            add(
                "PBXFileReference",
                f'\t\t{pid} /* {product_name} */ = {{isa = PBXFileReference; explicitFileType = {ftype}; '
                f'includeInIndex = 0; path = "{product_name}"; sourceTree = BUILT_PRODUCTS_DIR; }};\n',
            )
            sources = HOST_APP if kind == "app" else SHARED_RUNNER + spec["runner_extra"]
            build_files = []
            for path in sources:
                bid = oid("build", target, path)
                build_files.append(bid)
                add(
                    "PBXBuildFile",
                    f"\t\t{bid} /* {Path(path).name} in Sources */ = {{isa = PBXBuildFile; "
                    f"fileRef = {file_ids[path]} /* {Path(path).name} */; }};\n",
                )
            src_phase = oid("sources", target)
            fw_phase = oid("frameworks", target)
            res_phase = oid("resources", target)
            add(
                "PBXSourcesBuildPhase",
                f"\t\t{src_phase} /* Sources */ = {{\n\t\t\tisa = PBXSourcesBuildPhase;\n"
                f"\t\t\tbuildActionMask = 2147483647;\n\t\t\tfiles = (\n"
                + "".join(f"\t\t\t\t{b},\n" for b in build_files)
                + "\t\t\t);\n\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t};\n",
            )
            add(
                "PBXFrameworksBuildPhase",
                f"\t\t{fw_phase} /* Frameworks */ = {{\n\t\t\tisa = PBXFrameworksBuildPhase;\n"
                f"\t\t\tbuildActionMask = 2147483647;\n\t\t\tfiles = (\n\t\t\t);\n"
                f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n",
            )
            add(
                "PBXResourcesBuildPhase",
                f"\t\t{res_phase} /* Resources */ = {{\n\t\t\tisa = PBXResourcesBuildPhase;\n"
                f"\t\t\tbuildActionMask = 2147483647;\n\t\t\tfiles = (\n\t\t\t);\n"
                f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n",
            )
            settings = dict(base)
            settings["PRODUCT_NAME"] = '"$(TARGET_NAME)"'
            deps = ""
            if kind == "app":
                settings.update(spec["app_settings"])
                settings["PRODUCT_BUNDLE_IDENTIFIER"] = f"dev.swarmqa.SwarmRunnerHost.{plat.lower()}"
                ptype = "com.apple.product-type.application"
            else:
                settings["PRODUCT_BUNDLE_IDENTIFIER"] = f"dev.swarmqa.SwarmRunnerUITests.{plat.lower()}"
                settings["TEST_TARGET_NAME"] = host
                settings.update(spec.get("runner_settings", {}))
                ptype = "com.apple.product-type.bundle.ui-testing"
                dep = oid("dep", target)
                proxy = oid("proxy", target)
                host_id = oid("target", host)
                add(
                    "PBXContainerItemProxy",
                    f"\t\t{proxy} /* PBXContainerItemProxy */ = {{\n\t\t\tisa = PBXContainerItemProxy;\n"
                    f"\t\t\tcontainerPortal = {oid('project')} /* Project object */;\n"
                    f"\t\t\tproxyType = 1;\n\t\t\tremoteGlobalIDString = {host_id};\n"
                    f'\t\t\tremoteInfo = "{host}";\n\t\t}};\n',
                )
                add(
                    "PBXTargetDependency",
                    f"\t\t{dep} /* PBXTargetDependency */ = {{\n\t\t\tisa = PBXTargetDependency;\n"
                    f'\t\t\ttarget = {host_id} /* {host} */;\n\t\t\ttargetProxy = {proxy} /* PBXContainerItemProxy */;\n\t\t}};\n',
                )
                deps = f"\t\t\t\t{dep} /* PBXTargetDependency */,\n"
            cl = oid("configlist", target)
            config_lists.append((cl, target, settings, {}))
            add(
                "PBXNativeTarget",
                f'\t\t{tid} /* {target} */ = {{\n\t\t\tisa = PBXNativeTarget;\n'
                f'\t\t\tbuildConfigurationList = {cl} /* Build configuration list for PBXNativeTarget "{target}" */;\n'
                f"\t\t\tbuildPhases = (\n\t\t\t\t{src_phase} /* Sources */,\n\t\t\t\t{fw_phase} /* Frameworks */,\n"
                f"\t\t\t\t{res_phase} /* Resources */,\n\t\t\t);\n\t\t\tbuildRules = (\n\t\t\t);\n"
                f"\t\t\tdependencies = (\n{deps}\t\t\t);\n"
                f'\t\t\tname = "{target}";\n\t\t\tproductName = "{target}";\n'
                f'\t\t\tproductReference = {pid} /* {product_name} */;\n'
                f"\t\t\tproductType = \"{ptype}\";\n\t\t}};\n",
            )

    # Groups
    main_group = oid("group", "main")
    products_group = oid("group", "products")
    sources_group = oid("group", "sources")
    add(
        "PBXGroup",
        f"\t\t{main_group} = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n"
        f"\t\t\t\t{sources_group} /* Sources */,\n\t\t\t\t{products_group} /* Products */,\n"
        f"\t\t\t);\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n",
    )
    add(
        "PBXGroup",
        f"\t\t{sources_group} /* Sources */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n"
        + "".join(f"\t\t\t\t{file_ids[p]} /* {Path(p).name} */,\n" for p in all_sources)
        + "\t\t\t);\n\t\t\tname = Sources;\n\t\t\tsourceTree = \"<group>\";\n\t\t};\n",
    )
    add(
        "PBXGroup",
        f"\t\t{products_group} /* Products */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n"
        + "".join(f"\t\t\t\t{p},\n" for p in product_refs)
        + "\t\t\t);\n\t\t\tname = Products;\n\t\t\tsourceTree = \"<group>\";\n\t\t};\n",
    )

    # Build configurations
    project_common = {
        "ALWAYS_SEARCH_USER_PATHS": "NO",
        "CLANG_ENABLE_MODULES": "YES",
        "CLANG_ENABLE_OBJC_ARC": "YES",
        "ENABLE_USER_SCRIPT_SANDBOXING": "YES",
        "SWIFT_VERSION": "5.0",
    }
    project_by_config = {
        "Debug": {
            "DEBUG_INFORMATION_FORMAT": "dwarf",
            "ONLY_ACTIVE_ARCH": "YES",
            "SWIFT_OPTIMIZATION_LEVEL": '"-Onone"',
            "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG",
            "ENABLE_TESTABILITY": "YES",
            "GCC_OPTIMIZATION_LEVEL": "0",
        },
        "Release": {
            "DEBUG_INFORMATION_FORMAT": '"dwarf-with-dsym"',
            "SWIFT_OPTIMIZATION_LEVEL": '"-O"',
            "SWIFT_COMPILATION_MODE": "wholemodule",
            "ENABLE_NS_ASSERTIONS": "NO",
        },
    }
    config_lists.insert(0, (oid("configlist", "project"), "project", project_common, project_by_config))

    for cl, owner, settings, by_config in config_lists:
        ids = []
        for config in ("Debug", "Release"):
            cid = oid("config", owner, config)
            ids.append((cid, config))
            merged = {**settings, **by_config.get(config, {})}
            add(
                "XCBuildConfiguration",
                f"\t\t{cid} /* {config} */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n"
                + fmt_settings(merged, "\t\t\t\t")
                + f"\t\t\t}};\n\t\t\tname = {config};\n\t\t}};\n",
            )
        label = "PBXProject \"SwarmRunner\"" if owner == "project" else f'PBXNativeTarget "{owner}"'
        add(
            "XCConfigurationList",
            f"\t\t{cl} /* Build configuration list for {label} */ = {{\n\t\t\tisa = XCConfigurationList;\n"
            f"\t\t\tbuildConfigurations = (\n"
            + "".join(f"\t\t\t\t{cid} /* {c} */,\n" for cid, c in ids)
            + "\t\t\t);\n\t\t\tdefaultConfigurationIsVisible = 0;\n\t\t\tdefaultConfigurationName = Release;\n\t\t};\n",
        )

    test_attrs = "".join(
        f"\t\t\t\t\t{oid('target', f'SwarmRunnerUITests-{p}')} = {{\n\t\t\t\t\t\tTestTargetID = {oid('target', f'SwarmRunnerHost-{p}')};\n\t\t\t\t\t}};\n"
        for p in PLATFORMS
    )
    add(
        "PBXProject",
        f"\t\t{oid('project')} /* Project object */ = {{\n\t\t\tisa = PBXProject;\n"
        f"\t\t\tattributes = {{\n\t\t\t\tBuildIndependentTargetsInParallel = 1;\n"
        f"\t\t\t\tLastSwiftUpdateCheck = 1600;\n\t\t\t\tLastUpgradeCheck = 1600;\n"
        f"\t\t\t\tTargetAttributes = {{\n{test_attrs}\t\t\t\t}};\n\t\t\t}};\n"
        f'\t\t\tbuildConfigurationList = {oid("configlist", "project")} /* Build configuration list for PBXProject "SwarmRunner" */;\n'
        f'\t\t\tcompatibilityVersion = "Xcode 14.0";\n\t\t\tdevelopmentRegion = en;\n'
        f"\t\t\thasScannedForEncodings = 0;\n\t\t\tknownRegions = (\n\t\t\t\ten,\n\t\t\t\tBase,\n\t\t\t);\n"
        f"\t\t\tmainGroup = {main_group};\n\t\t\tproductRefGroup = {products_group} /* Products */;\n"
        f'\t\t\tprojectDirPath = "";\n\t\t\tprojectRoot = "";\n\t\t\ttargets = (\n'
        + "".join(f"\t\t\t\t{t},\n" for t in targets)
        + "\t\t\t);\n\t\t};\n",
    )

    out = ["// !$*UTF8*$!\n{\n\tarchiveVersion = 1;\n\tclasses = {\n\t};\n\tobjectVersion = 56;\n\tobjects = {\n"]
    for section in sorted(objects):
        out.append(f"\n/* Begin {section} section */\n")
        out.extend(objects[section])
        out.append(f"/* End {section} section */\n")
    out.append(f"\t}};\n\trootObject = {oid('project')} /* Project object */;\n}}\n")
    PROJECT.mkdir(exist_ok=True)
    (PROJECT / "project.pbxproj").write_text("".join(out))

    schemes = PROJECT / "xcshareddata" / "xcschemes"
    schemes.mkdir(parents=True, exist_ok=True)
    for plat in PLATFORMS:
        (schemes / f"SwarmRunner-{plat}.xcscheme").write_text(scheme_xml(plat))
    print(f"wrote {PROJECT}")


def buildable(target: str, product: str) -> str:
    return (
        f'<BuildableReference BuildableIdentifier = "primary" BlueprintIdentifier = "{oid("target", target)}" '
        f'BuildableName = "{product}" BlueprintName = "{target}" ReferencedContainer = "container:SwarmRunner.xcodeproj">'
        f"</BuildableReference>"
    )


def scheme_xml(plat: str) -> str:
    host = f"SwarmRunnerHost-{plat}"
    tests = f"SwarmRunnerUITests-{plat}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion = "1600" version = "1.7">
   <BuildAction parallelizeBuildables = "YES" buildImplicitDependencies = "YES">
      <BuildActionEntries>
         <BuildActionEntry buildForTesting = "YES" buildForRunning = "YES" buildForProfiling = "NO" buildForArchiving = "NO" buildForAnalyzing = "NO">
            {buildable(host, host + ".app")}
         </BuildActionEntry>
         <BuildActionEntry buildForTesting = "YES" buildForRunning = "NO" buildForProfiling = "NO" buildForArchiving = "NO" buildForAnalyzing = "NO">
            {buildable(tests, tests + ".xctest")}
         </BuildActionEntry>
      </BuildActionEntries>
   </BuildAction>
   <TestAction buildConfiguration = "Release" selectedDebuggerIdentifier = "" selectedLauncherIdentifier = "Xcode.IDEFoundation.Launcher.PosixSpawn" shouldUseLaunchSchemeArgsEnv = "YES">
      <Testables>
         <TestableReference skipped = "NO">
            {buildable(tests, tests + ".xctest")}
         </TestableReference>
      </Testables>
   </TestAction>
   <LaunchAction buildConfiguration = "Release" selectedDebuggerIdentifier = "" selectedLauncherIdentifier = "Xcode.IDEFoundation.Launcher.PosixSpawn" launchStyle = "0" useCustomWorkingDirectory = "NO" ignoresPersistentStateOnLaunch = "NO" debugDocumentVersioning = "YES" debugServiceExtension = "internal" allowLocationSimulation = "YES">
      <BuildableProductRunnable runnableDebuggingMode = "0">
         {buildable(host, host + ".app")}
      </BuildableProductRunnable>
   </LaunchAction>
</Scheme>
"""


if __name__ == "__main__":
    main()
