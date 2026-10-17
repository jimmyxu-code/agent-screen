import AppKit
import ApplicationServices

// Physical-screen window placement only. Single-screen work stays on that screen.
struct ScreenError: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}

func json(_ value: Any) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), encoding: .utf8)!
}

func rectJSON(_ rect: CGRect) -> [String: Double] {
    ["x": rect.minX, "y": rect.minY, "width": rect.width, "height": rect.height]
}

func screenID(_ screen: NSScreen) -> CGDirectDisplayID {
    (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as! NSNumber).uint32Value
}

func screens() throws -> [NSScreen] {
    RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    let result = NSScreen.screens.filter {
        let id = screenID($0)
        // SpaceOKit/Stage.swift identifies its virtual displays with these IDs.
        let isSpaceO = CGDisplayVendorNumber(id) == 0x1AF2 && CGDisplayModelNumber(id) == 0x0001
        return CGDisplayIsActive(id) != 0 && !isSpaceO
    }
    guard !result.isEmpty else { throw ScreenError("没有可用显示屏。") }
    guard !result.contains(where: { CGDisplayIsInMirrorSet(screenID($0)) != 0 }) else {
        throw ScreenError("显示屏处于镜像模式，请先改成扩展显示。")
    }
    let primary = CGMainDisplayID()
    return result.sorted {
        if screenID($0) == screenID($1) { return false }
        if screenID($0) == primary { return true }
        if screenID($1) == primary { return false }
        return screenID($0) < screenID($1)
    }
}

func destination() throws -> NSScreen {
    let connected = try screens()
    return connected[connected.count > 1 ? 1 : 0]
}

func prepare() throws -> [String: Any] {
    return try status()
}

func axAttribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func windows(_ pid: pid_t) throws -> [AXUIElement] {
    guard pid > 0, NSRunningApplication(processIdentifier: pid) != nil else { throw ScreenError("应用 PID 不存在。") }
    guard AXIsProcessTrusted() else { throw ScreenError("需要辅助功能权限才能读取或移动窗口。") }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 2)
    guard let result = axAttribute(app, kAXWindowsAttribute) as? [AXUIElement] else {
        throw ScreenError("无法读取这个应用的窗口。")
    }
    return result
}

func windowRect(_ window: AXUIElement) throws -> CGRect {
    guard let p = axAttribute(window, kAXPositionAttribute), CFGetTypeID(p) == AXValueGetTypeID(),
          let s = axAttribute(window, kAXSizeAttribute), CFGetTypeID(s) == AXValueGetTypeID() else {
        throw ScreenError("无法读取窗口的位置和大小。")
    }
    var point = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point), AXValueGetValue(s as! AXValue, .cgSize, &size) else {
        throw ScreenError("窗口位置或大小无效。")
    }
    return CGRect(origin: point, size: size)
}

func status(pid: pid_t? = nil) throws -> [String: Any] {
    let connected = try screens()
    let target = connected[connected.count > 1 ? 1 : 0]
    var result: [String: Any] = [
        "mode": connected.count > 1 ? "second_physical_screen" : "single_physical_screen",
        "placement_required": connected.count > 1,
        "instruction": connected.count > 1
            ? "只将当前任务的具体工作窗口放到第二块物理屏，再用电脑操作工具操作。控制端主界面留在用户主屏。"
            : "直接用电脑操作工具在当前物理屏操作任务窗口；无需 SpaceO、额外屏幕隔离或健康检查。",
        "target_display_id": screenID(target),
        "target_screen": target.localizedName,
        "accessibility": AXIsProcessTrusted(),
        "screens": connected.enumerated().map { index, screen in
            ["number": index + 1, "id": screenID(screen), "name": screen.localizedName,
             "bounds": rectJSON(CGDisplayBounds(screenID(screen)))] as [String: Any]
        }
    ]
    if let pid {
        result["pid"] = pid
        result["windows"] = try windows(pid).enumerated().map { index, window in
            ["index": index, "title": axAttribute(window, kAXTitleAttribute) as? String ?? "",
             "bounds": rectJSON(try windowRect(window))] as [String: Any]
        }
    } else {
        result["apps"] = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }.map {
            ["pid": $0.processIdentifier, "name": $0.localizedName ?? "", "bundle_id": $0.bundleIdentifier ?? ""] as [String: Any]
        }
    }
    return result
}

func move(pid: pid_t, index: Int) throws -> [String: Any] {
    let screen = try destination()
    let appWindows = try windows(pid)
    guard appWindows.indices.contains(index) else { throw ScreenError("窗口序号不存在，请重新读取 status(pid)。") }
    let window = appWindows[index]
    if (axAttribute(window, "AXFullScreen") as? Bool) == true {
        throw ScreenError("请先退出目标窗口的全屏模式再放置到目标屏。")
    }
    if (axAttribute(window, kAXMinimizedAttribute) as? Bool) == true {
        throw ScreenError("请先恢复目标窗口再放置。")
    }
    let original = try windowRect(window)
    let mainHeight = CGDisplayBounds(CGMainDisplayID()).height
    let visible = screen.visibleFrame
    let available = CGRect(x: visible.minX, y: mainHeight - visible.maxY,
                           width: visible.width, height: visible.height).insetBy(dx: 12, dy: 12)
    var size = CGSize(width: min(original.width, available.width), height: min(original.height, available.height))
    if size != original.size {
        guard let value = AXValueCreate(.cgSize, &size),
              AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, value) == .success else {
            throw ScreenError("窗口太大且应用拒绝调整大小，无法完整放入目标屏。")
        }
    }
    let actualSize = try windowRect(window).size
    guard actualSize.width <= available.width, actualSize.height <= available.height else {
        throw ScreenError("应用的最小窗口尺寸大于目标屏幕。")
    }
    var point = CGPoint(x: available.midX - actualSize.width / 2, y: available.midY - actualSize.height / 2)
    guard let value = AXValueCreate(.cgPoint, &point),
          AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, value) == .success else {
        throw ScreenError("应用拒绝移动这个窗口。")
    }
    let deadline = Date().addingTimeInterval(2)
    repeat {
        Thread.sleep(forTimeInterval: 0.1)
        let actual = try windowRect(window)
        if available.insetBy(dx: -2, dy: -2).contains(actual) {
            return ["moved": true, "pid": pid, "window_index": index, "target_display_id": screenID(screen),
                    "target_screen": screen.localizedName, "bounds": rectJSON(actual)]
        }
    } while Date() < deadline
    throw ScreenError("移动后窗口仍未完整位于目标屏，无法确认放置成功。")
}

let emptySchema: [String: Any] = ["type": "object", "properties": [:], "additionalProperties": false]
let tools: [[String: Any]] = [
    ["name": "agent_screen_status", "description": "Read physical screens and app PIDs. With pid, list indexed windows. Multi-screen target is the second physical screen. With one physical screen, operate task windows directly there using computer-use tools; no SpaceO session or health check is needed.",
     "inputSchema": ["type": "object", "properties": ["pid": ["type": "integer", "minimum": 1]], "additionalProperties": false]],
    ["name": "agent_screen_prepare", "description": "Read the physical-screen route without changing windows or the pointer. On multiple physical screens, open a task window, then place it with agent_screen_move. On one physical screen, operate directly there; no placement, SpaceO, isolation, or health check is required. Keep controller main windows on the user's primary screen.", "inputSchema": emptySchema],
    ["name": "agent_screen_move", "description": "Move only one specified task window onto the target physical screen and verify its bounds. Get window_index from agent_screen_status(pid). Only move a window opened for this task or explicitly designated by the user. Never sweep an app's windows or move a controller's normal main UI. Single-screen placement is optional. Use computer-use tools afterward.",
     "inputSchema": ["type": "object", "properties": ["pid": ["type": "integer", "minimum": 1], "window_index": ["type": "integer", "minimum": 0]], "required": ["pid", "window_index"], "additionalProperties": false]]
]

func integerArgument(_ arguments: [String: Any], _ name: String, minimum: Int) throws -> Int {
    guard let number = arguments[name] as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite,
          number.doubleValue.rounded() == number.doubleValue,
          number.doubleValue >= Double(minimum), number.doubleValue <= Double(Int32.max) else {
        throw ScreenError("需要有效整数参数：\(name)。")
    }
    return number.intValue
}

func emit(_ response: [String: Any]) {
    print(json(response))
    fflush(stdout)
}

func processRequest(_ data: Data) {
    let object: Any
    do { object = try JSONSerialization.jsonObject(with: data) }
    catch {
        emit(["jsonrpc": "2.0", "id": NSNull(), "error": ["code": -32700, "message": "Parse error"]])
        return
    }
    guard let request = object as? [String: Any], request["jsonrpc"] as? String == "2.0",
          let method = request["method"] as? String else {
        emit(["jsonrpc": "2.0", "id": NSNull(), "error": ["code": -32600, "message": "Invalid request"]])
        return
    }
    // Notifications, including id-less tool calls, must never mutate a window.
    guard let id = request["id"] else { return }
    guard id is String || id is NSNull ||
          ((id as? NSNumber).map { CFGetTypeID($0) != CFBooleanGetTypeID() } ?? false) else {
        emit(["jsonrpc": "2.0", "id": NSNull(), "error": ["code": -32600, "message": "Invalid id"]])
        return
    }
    guard request["params"] == nil || request["params"] is [String: Any] else {
        emit(["jsonrpc": "2.0", "id": id, "error": ["code": -32602, "message": "Expected object params"]])
        return
    }
    let params = request["params"] as? [String: Any] ?? [:]
    var response: [String: Any] = ["jsonrpc": "2.0", "id": id]
    switch method {
    case "initialize":
        let requested = params["protocolVersion"] as? String ?? "2025-06-18"
        let supported = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]
        response["result"] = ["protocolVersion": supported.contains(requested) ? requested : "2025-11-25",
                              "capabilities": ["tools": [:]], "serverInfo": ["name": "agent-screen", "version": "1.1.0"]]
    case "ping": response["result"] = [:] as [String: Any]
    case "tools/list": response["result"] = ["tools": tools]
    case "tools/call":
        do {
            guard params["arguments"] == nil || params["arguments"] is [String: Any] else {
                throw ScreenError("工具参数必须为对象。")
            }
            let arguments = params["arguments"] as? [String: Any] ?? [:]
            let name = params["name"] as? String ?? ""
            let allowed: Set<String>
            switch name {
            case "agent_screen_status": allowed = ["pid"]
            case "agent_screen_prepare": allowed = []
            case "agent_screen_move": allowed = ["pid", "window_index"]
            default: throw ScreenError("未知工具。")
            }
            guard Set(arguments.keys).isSubset(of: allowed) else {
                throw ScreenError("包含未支持的工具参数。")
            }
            let result: [String: Any]
            switch name {
            case "agent_screen_status":
                let pid = try arguments["pid"].map { _ in try integerArgument(arguments, "pid", minimum: 1) }
                result = try status(pid: pid.map { pid_t($0) })
            case "agent_screen_prepare": result = try prepare()
            case "agent_screen_move":
                let pid = try integerArgument(arguments, "pid", minimum: 1)
                let index = try integerArgument(arguments, "window_index", minimum: 0)
                result = try move(pid: pid_t(pid), index: index)
            default: throw ScreenError("未知工具。")
            }
            response["result"] = ["content": [["type": "text", "text": json(result)]]]
        } catch {
            response["result"] = ["isError": true, "content": [["type": "text", "text": String(describing: error)]]]
        }
    default: response["error"] = ["code": -32601, "message": "Method not found"]
    }
    emit(response)
}

func serve() {
    var line = Data()
    var dropping = false
    var buffer = [UInt8](repeating: 0, count: 4096)
    let maximumBytes = 65_536
    while true {
        let count = buffer.withUnsafeMutableBytes { read(STDIN_FILENO, $0.baseAddress, $0.count) }
        if count < 0 && errno == EINTR { continue }
        guard count > 0 else { break }
        for byte in buffer.prefix(count) {
            if byte == 10 {
                if dropping {
                    emit(["jsonrpc": "2.0", "id": NSNull(), "error": ["code": -32700, "message": "Request exceeds 64 KiB"]])
                } else { processRequest(line) }
                line.removeAll(keepingCapacity: true)
                dropping = false
            } else if !dropping {
                if line.count == maximumBytes {
                    line.removeAll(keepingCapacity: true)
                    dropping = true
                } else { line.append(byte) }
            }
        }
    }
    if dropping {
        emit(["jsonrpc": "2.0", "id": NSNull(), "error": ["code": -32700, "message": "Request exceeds 64 KiB"]])
    } else if !line.isEmpty { processRequest(line) }
}

do {
    let args = Array(CommandLine.arguments.dropFirst())
    switch args.first ?? "status" {
    case "mcp": serve()
    case "status":
        guard args.count <= 2 else { throw ScreenError("用法：agent-screen status [PID]") }
        let pid: pid_t?
        if args.count == 2 {
            guard let value = pid_t(args[1]), value > 0 else { throw ScreenError("PID 无效。") }
            pid = value
        } else { pid = nil }
        print(json(try status(pid: pid)))
    case "prepare": print(json(try prepare()))
    case "move":
        guard args.count == 3, let pid = pid_t(args[1]), pid > 0,
              let index = Int(args[2]), index >= 0, index <= Int(Int32.max) else {
            throw ScreenError("用法：agent-screen move PID WINDOW_INDEX")
        }
        print(json(try move(pid: pid, index: index)))
    default: throw ScreenError("用法：agent-screen status [PID] | prepare | move PID WINDOW_INDEX | mcp")
    }
} catch {
    FileHandle.standardError.write(Data((String(describing: error) + "\n").utf8))
    exit(1)
}
