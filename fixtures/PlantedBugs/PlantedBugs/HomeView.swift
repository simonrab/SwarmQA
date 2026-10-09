import SwiftUI

struct HomeView: View {
    @Environment(TaskStore.self) private var store
    @Environment(AppSettings.self) private var settings
    @Environment(Router.self) private var router
    @State private var showingAdd = false

    var body: some View {
        @Bindable var store = store
        List {
            Section {
                ForEach(store.visibleTasks) { task in
                    NavigationLink(value: Route.task(task.id)) {
                        TaskRow(task: task, showID: settings.showTaskIDs)
                    }
                    .accessibilityIdentifier("home.row.\(task.id)")
                }
                if store.visibleTasks.isEmpty {
                    Text("Nothing to do. Add a task with the + button.")
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("home.emptyLabel")
                }
            } header: {
                Text(store.hideCompleted ? "Open tasks" : "All tasks")
                    .accessibilityIdentifier("home.sectionHeader")
            } footer: {
                Text("\(store.doneCount) of \(store.tasks.count) done")
                    .accessibilityIdentifier("home.summaryLabel")
            }

            Section("Insights") {
                NavigationLink(value: Route.stats) {
                    Label("Weekly stats", systemImage: "chart.bar")
                }
                .accessibilityIdentifier("home.statsLink")
            }

            Section {
                Text("Tip: open a task to mark it done or delete it.")
                    .font(.footnote)
                    .foregroundStyle(Color(red: 0.80, green: 0.80, blue: 0.82))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                    .background(Color(red: 0.96, green: 0.96, blue: 0.97), in: RoundedRectangle(cornerRadius: 8))
                    .accessibilityIdentifier("home.tipLabel")
            }
        }
        .navigationTitle("Tasks")
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Button {
                    store.hideCompleted.toggle()
                } label: {
                    FilterGlyph(active: store.hideCompleted)
                }
                .accessibilityIdentifier("home.filterButton")

                Button {
                    showingAdd = true
                } label: {
                    Label("Add Task", systemImage: "plus")
                }
                .accessibilityIdentifier("home.addButton")

                Button {
                    router.path.append(.settings)
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
                .accessibilityIdentifier("home.settingsButton")
            }
        }
        .sheet(isPresented: $showingAdd) {
            AddTaskView()
        }
    }
}

struct TaskRow: View {
    let task: TaskItem
    let showID: Bool

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: task.isDone ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(task.isDone ? Color.green : Color.secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(task.title)
                    .strikethrough(task.isDone)
                Text(task.isDone ? "Done" : "Due \(task.due)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if showID {
                    Text("ID: \(task.id)")
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

/// Three stacked bars, like a filter icon, drawn from shapes.
struct FilterGlyph: View {
    let active: Bool

    var body: some View {
        VStack(spacing: 3) {
            Capsule().frame(width: 18, height: 2.5)
            Capsule().frame(width: 12, height: 2.5)
            Capsule().frame(width: 6, height: 2.5)
        }
        .foregroundStyle(active ? Color.accentColor : Color.primary)
        .frame(width: 24, height: 24)
        .contentShape(Rectangle())
    }
}

struct AddTaskView: View {
    @Environment(TaskStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var priority: Priority = .normal

    var body: some View {
        NavigationStack {
            Form {
                TextField("Title", text: $title)
                    .accessibilityIdentifier("add.titleField")
                Picker("Priority", selection: $priority) {
                    ForEach(Priority.allCases) { Text($0.rawValue).tag($0) }
                }
                .accessibilityIdentifier("add.priorityPicker")
            }
            .formStyle(.grouped)
            .navigationTitle("New Task")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .accessibilityIdentifier("add.cancelButton")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        store.add(title: title.trimmingCharacters(in: .whitespaces), priority: priority)
                        dismiss()
                    }
                    .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty)
                    .accessibilityIdentifier("add.saveButton")
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 360, minHeight: 240)
        #endif
    }
}
