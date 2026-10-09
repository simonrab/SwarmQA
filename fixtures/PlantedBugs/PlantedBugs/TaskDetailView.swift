import SwiftUI

struct TaskDetailView: View {
    let taskID: String
    @Environment(TaskStore.self) private var store
    @Environment(Router.self) private var router
    @State private var confirmingDelete = false

    var body: some View {
        if let task = store.task(taskID) {
            content(task)
        } else {
            Text("This task was deleted.")
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("detail.missingLabel")
        }
    }

    private func content(_ task: TaskItem) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text(task.title)
                    .font(.title2.bold())
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("detail.title")
                    .overlay(alignment: .topLeading) {
                        Text("\(task.priority.rawValue) priority")
                            .font(.caption.bold())
                            .foregroundStyle(.white)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(badgeColor(task.priority), in: Capsule())
                            .offset(x: 96, y: 6)
                            .accessibilityIdentifier("detail.priorityBadge")
                    }

                Text(task.isDone ? "Done" : "Due \(task.due)")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("detail.dueLabel")

                VStack(alignment: .leading, spacing: 6) {
                    Text("Notes")
                        .font(.headline)
                    Text(task.notes.isEmpty ? "No notes." : task.notes)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, maxHeight: 54, alignment: .topLeading)
                        .clipped()
                        .accessibilityIdentifier("detail.notes")
                }

                Toggle("Completed", isOn: Binding(
                    get: { store.task(taskID)?.isDone ?? false },
                    set: { store.setDone(taskID, $0) }
                ))
                .accessibilityIdentifier("detail.completeToggle")

                NavigationLink(value: Route.attachments(taskID)) {
                    HStack {
                        Label("Attachments", systemImage: "paperclip")
                        Spacer()
                        Text("\(task.attachments.count)")
                            .foregroundStyle(.secondary)
                        Image(systemName: "chevron.right")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("detail.attachmentsLink")

                HStack(spacing: 12) {
                    Button {
                        _ = shareText(task)
                    } label: {
                        Label("Share", systemImage: "square.and.arrow.up")
                    }
                    .buttonStyle(.borderedProminent)
                    .accessibilityIdentifier("detail.shareButton")

                    Button(role: .destructive) {
                        confirmingDelete = true
                    } label: {
                        Label("Delete", systemImage: "trash")
                    }
                    .buttonStyle(.bordered)
                    .accessibilityIdentifier("detail.deleteButton")
                }
            }
            .padding(20)
        }
        .navigationTitle("Task")
        .confirmationDialog("Delete this task?", isPresented: $confirmingDelete, titleVisibility: .visible) {
            Button("Delete Task", role: .destructive) {
                router.path.removeLast()
                store.delete(taskID)
            }
            .accessibilityIdentifier("detail.deleteConfirmButton")
            Button("Cancel", role: .cancel) {}
                .accessibilityIdentifier("detail.deleteCancelButton")
        } message: {
            Text("This cannot be undone.")
        }
    }

    private func shareText(_ task: TaskItem) -> String {
        "\(task.title) (due \(task.due))\n\(task.notes)"
    }

    private func badgeColor(_ priority: Priority) -> Color {
        switch priority {
        case .high: return .red
        case .normal: return .blue
        case .low: return .gray
        }
    }
}

struct AttachmentsView: View {
    let taskID: String
    @Environment(TaskStore.self) private var store

    var body: some View {
        let attachments = store.task(taskID)?.attachments ?? []
        List {
            if attachments.isEmpty {
                Text("No attachments.")
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("attachments.emptyLabel")
            }
            ForEach(attachments) { attachment in
                NavigationLink(value: Route.attachment(taskID: taskID, attachmentID: attachment.id)) {
                    HStack {
                        Label(attachment.name, systemImage: "doc")
                        Spacer()
                        Text(attachment.sizeLabel)
                            .foregroundStyle(.secondary)
                    }
                }
                .accessibilityIdentifier("attachments.row.\(attachment.id)")
            }
        }
        .navigationTitle("Attachments")
    }
}

struct AttachmentPreviewView: View {
    let taskID: String
    let attachmentID: String
    @Environment(TaskStore.self) private var store

    var body: some View {
        let attachment = store.attachment(taskID: taskID, attachmentID: attachmentID)
        let text = String(decoding: attachment!.inlineData!, as: UTF8.self)
        ScrollView {
            Text(text)
                .font(.body.monospaced())
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
                .accessibilityIdentifier("attachmentPreview.text")
        }
        .navigationTitle(attachment?.name ?? "Attachment")
    }
}

struct StatsView: View {
    @State private var summary: String?

    var body: some View {
        VStack(spacing: 16) {
            if let summary {
                Text(summary)
                    .font(.title3)
                    .accessibilityIdentifier("stats.summaryLabel")
            } else {
                ProgressView("Loading weekly stats…")
                    .accessibilityIdentifier("stats.loadingSpinner")
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .navigationTitle("Weekly stats")
        .onAppear {
            StatsService.shared.fetchWeekly { result in
                summary = result
            }
        }
    }
}
