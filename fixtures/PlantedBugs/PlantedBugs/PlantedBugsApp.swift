// PlantedBugs: the SwarmQA benchmark app.
//
// This app contains INTENTIONAL bugs. Each one is listed, by accessibility
// identifier, in ../bugs.json. Do not fix them: the benchmark scores the
// swarm's findings against that list. Everything not listed there should
// work correctly.

import SwiftUI

@main
struct PlantedBugsApp: App {
    @State private var store = TaskStore()
    @State private var settings = AppSettings()
    @State private var router = Router()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(store)
                .environment(settings)
                .environment(router)
        }
        #if os(macOS)
        .defaultSize(width: 520, height: 760)
        #endif
    }
}

enum Route: Hashable {
    case task(String)
    case attachments(String)
    case attachment(taskID: String, attachmentID: String)
    case stats
    case settings
    case advanced
    case dataStorage
    case cloudSync
    case about
}

@Observable
final class Router {
    var path: [Route] = []
}

struct RootView: View {
    @Environment(Router.self) private var router

    var body: some View {
        @Bindable var router = router
        NavigationStack(path: $router.path) {
            HomeView()
                .navigationDestination(for: Route.self) { route in
                    destination(for: route)
                }
        }
    }

    @ViewBuilder
    private func destination(for route: Route) -> some View {
        switch route {
        case .task(let id):
            TaskDetailView(taskID: id)
        case .attachments(let id):
            AttachmentsView(taskID: id)
        case .attachment(let taskID, let attachmentID):
            AttachmentPreviewView(taskID: taskID, attachmentID: attachmentID)
        case .stats:
            StatsView()
        case .settings:
            SettingsView()
        case .advanced:
            AdvancedSettingsView()
        case .dataStorage:
            DataStorageView()
        case .cloudSync:
            CloudSyncView()
        case .about:
            AboutView()
        }
    }
}
