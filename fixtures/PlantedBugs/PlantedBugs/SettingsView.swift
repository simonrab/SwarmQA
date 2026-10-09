import SwiftUI

struct SettingsView: View {
    @Environment(AppSettings.self) private var settings

    var body: some View {
        @Bindable var settings = settings
        Form {
            Section("Profile") {
                TextField("Display name", text: $settings.displayName)
                    .accessibilityIdentifier("settings.nameField")
            }
            Section("Notifications") {
                Toggle("Due date reminders", isOn: $settings.remindersOn)
                    .accessibilityIdentifier("settings.remindersToggle")
            }
            Section("Sync") {
                // Read-only status. The control lives under
                // Advanced > Data & Storage > Cloud (see CloudSyncView).
                LabeledContent("Cloud sync", value: settings.syncEnabled ? "On" : "Off")
                    .accessibilityIdentifier("settings.syncStatus")
            }
            Section {
                NavigationLink(value: Route.advanced) {
                    Text("Advanced")
                }
                .accessibilityIdentifier("settings.advancedLink")
                NavigationLink(value: Route.about) {
                    Text("About")
                }
                .accessibilityIdentifier("settings.aboutLink")
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Settings")
    }
}

struct AdvancedSettingsView: View {
    @Environment(AppSettings.self) private var settings

    var body: some View {
        @Bindable var settings = settings
        Form {
            Toggle("Show task IDs", isOn: $settings.showTaskIDs)
                .accessibilityIdentifier("advanced.showIDsToggle")
            NavigationLink(value: Route.dataStorage) {
                Text("Data & Storage")
            }
            .accessibilityIdentifier("advanced.dataStorageLink")
        }
        .formStyle(.grouped)
        .navigationTitle("Advanced")
    }
}

struct DataStorageView: View {
    @State private var cacheCleared = false

    var body: some View {
        Form {
            LabeledContent("Storage used", value: cacheCleared ? "0.3 MB" : "1.2 MB")
                .accessibilityIdentifier("dataStorage.usageLabel")
            Button("Clear cache") {
                cacheCleared = true
            }
            .disabled(cacheCleared)
            .accessibilityIdentifier("dataStorage.clearCacheButton")
            NavigationLink(value: Route.cloudSync) {
                Text("Cloud")
            }
            .accessibilityIdentifier("dataStorage.cloudLink")
        }
        .formStyle(.grouped)
        .navigationTitle("Data & Storage")
    }
}

struct CloudSyncView: View {
    @Environment(AppSettings.self) private var settings
    @State private var pauseDraft = true
    @State private var confirming = false
    @State private var loaded = false

    var body: some View {
        Form {
            Section {
                Toggle("Pause sync", isOn: $pauseDraft)
                    .accessibilityIdentifier("settings.sync.pauseToggle")
            } footer: {
                Text("Changes are not saved until applied.")
            }
            Section {
                Button("Apply") {
                    confirming = true
                }
                .accessibilityIdentifier("settings.sync.applyButton")
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Cloud")
        .onAppear {
            if !loaded {
                pauseDraft = !settings.syncEnabled
                loaded = true
            }
        }
        .alert("Are you sure?", isPresented: $confirming) {
            Button("Yes") {
                settings.syncEnabled = !pauseDraft
            }
            .accessibilityIdentifier("settings.sync.confirmYes")
            Button("No", role: .cancel) {}
                .accessibilityIdentifier("settings.sync.confirmNo")
        }
    }
}

struct AboutView: View {
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "checklist")
                .font(.system(size: 44))
                .foregroundStyle(Color.accentColor)
                .accessibilityHidden(true)
            Text("Planted Bugs")
                .font(.title2.bold())
                .accessibilityIdentifier("about.nameLabel")
            Text("Version 1.0 (1)")
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("about.versionLabel")
            Text("A small task list used as the SwarmQA benchmark app.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
                .accessibilityIdentifier("about.descriptionLabel")
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .navigationTitle("About")
    }
}
