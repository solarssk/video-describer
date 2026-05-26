import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("Usage: macos_path_picker folder|file [default_dir]\n", stderr)
    exit(1)
}

let kind = args[1]
guard kind == "folder" || kind == "file" else {
    fputs("Picker kind must be 'folder' or 'file'.\n", stderr)
    exit(1)
}

let defaultDir = args.count >= 3 ? args[2] : ""

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let panel = NSOpenPanel()
panel.canChooseFiles = kind == "file"
panel.canChooseDirectories = kind == "folder"
panel.allowsMultipleSelection = false
panel.canCreateDirectories = kind == "folder"
panel.prompt = "Choose"
panel.message = kind == "folder"
    ? "Select a folder with recordings"
    : "Select a video or photo file"

if !defaultDir.isEmpty {
    panel.directoryURL = URL(fileURLWithPath: defaultDir, isDirectory: true)
}

// Run the panel inside the event loop so NSApp.activate takes effect
// before the panel appears — fixes focus issues on repeated opens.
DispatchQueue.main.async {
    NSApp.activate(ignoringOtherApps: true)
    let response = panel.runModal()
    if response == .OK, let url = panel.url {
        print(url.path)
        exit(0)
    }
    exit(2)
}

app.run()
