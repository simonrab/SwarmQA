import Foundation
import Observation

enum Priority: String, CaseIterable, Identifiable {
    case low = "Low"
    case normal = "Normal"
    case high = "High"

    var id: String { rawValue }
}

struct Attachment: Identifiable, Hashable {
    let id: String
    let name: String
    let sizeLabel: String
    /// Text content for previewable attachments. Nil for binary files.
    let inlineData: Data?
}

struct TaskItem: Identifiable, Hashable {
    let id: String
    var title: String
    var notes: String
    var due: String
    var priority: Priority
    var isDone: Bool
    var attachments: [Attachment]
}

@Observable
final class TaskStore {
    var tasks: [TaskItem] = TaskStore.seed
    var hideCompleted = false
    private var nextID = 1

    var visibleTasks: [TaskItem] {
        hideCompleted ? tasks.filter { !$0.isDone } : tasks
    }

    var doneCount: Int { tasks.filter(\.isDone).count }

    func task(_ id: String) -> TaskItem? {
        tasks.first { $0.id == id }
    }

    func attachment(taskID: String, attachmentID: String) -> Attachment? {
        task(taskID)?.attachments.first { $0.id == attachmentID }
    }

    func setDone(_ id: String, _ done: Bool) {
        guard let index = tasks.firstIndex(where: { $0.id == id }) else { return }
        tasks[index].isDone = done
    }

    func delete(_ id: String) {
        tasks.removeAll { $0.id == id }
    }

    func add(title: String, priority: Priority) {
        let item = TaskItem(
            id: "new-\(nextID)",
            title: title,
            notes: "",
            due: "No date",
            priority: priority,
            isDone: false,
            attachments: []
        )
        nextID += 1
        tasks.insert(item, at: 0)
    }

    static let seed: [TaskItem] = [
        TaskItem(
            id: "report",
            title: "Finish quarterly report",
            notes: "Pull the revenue numbers from the finance sheet, update the charts for Q3, "
                + "and add a short section on hiring. Send the draft to Priya for review "
                + "before Thursday so there is time for a second pass. Remember to attach "
                + "the scanned expense summary at the end.",
            due: "Today",
            priority: .high,
            isDone: false,
            attachments: [
                Attachment(
                    id: "summary",
                    name: "summary.txt",
                    sizeLabel: "1 KB",
                    inlineData: Data("Q3 summary\n\nRevenue up 4% on Q2. Hiring on plan. Travel spend down 12%.".utf8)
                ),
                Attachment(id: "scan", name: "expenses-scan.pdf", sizeLabel: "2.4 MB", inlineData: nil),
            ]
        ),
        TaskItem(
            id: "groceries",
            title: "Buy groceries",
            notes: "Milk, eggs, spinach.",
            due: "Today",
            priority: .normal,
            isDone: false,
            attachments: []
        ),
        TaskItem(
            id: "dentist",
            title: "Book dentist appointment",
            notes: "Ask about Saturday slots.",
            due: "Tomorrow",
            priority: .low,
            isDone: false,
            attachments: []
        ),
        TaskItem(
            id: "plants",
            title: "Water the plants",
            notes: "Balcony and kitchen.",
            due: "Yesterday",
            priority: .low,
            isDone: true,
            attachments: []
        ),
        TaskItem(
            id: "taxes",
            title: "Gather tax documents",
            notes: "Payslips and receipts.",
            due: "Next week",
            priority: .normal,
            isDone: false,
            attachments: []
        ),
    ]
}

@Observable
final class AppSettings {
    var displayName = "Alex"
    var remindersOn = true
    var syncEnabled = false
    var showTaskIDs = false
}

/// Loads the weekly stats summary.
final class StatsService {
    static let shared = StatsService()
    private var cachedWeeks: [Int] = []

    // PLANTED BUG (endless-spinner): when the cache is empty this returns
    // without ever calling `completion`, so the caller waits forever.
    func fetchWeekly(completion: @escaping (String) -> Void) {
        guard let latest = cachedWeeks.last else { return }
        completion("\(latest) tasks completed this week")
    }
}
