import SwiftUI

// Stub host app. UI-test bundles need a target application to build against,
// but the runner never launches this app: it drives whatever bundle id
// `POST /launch` names.
@main
struct SwarmRunnerHostApp: App {
    var body: some Scene {
        WindowGroup {
            Text("Swarm runner host")
                .padding()
        }
    }
}
