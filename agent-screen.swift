import AppKit
import ApplicationServices

// agent-screen: where a GUI agent's windows live, enforced in code.
//
//   * Browser tasks: managed headless Chromium through agent-browser, never the user's browser.
//   * One physical screen: native GUI work uses a SpaceO virtual screen, not the user's desktop.
//   * Several physical screens: the agent screen is the built-in panel, chosen by hardware and
//     never by "whichever display macOS currently calls main", so hot-plug, wake and display-id
//     changes cannot flip the route onto the user's screen.
//       - claim:  hold one application; every window it creates is adopted onto the agent screen
//                 on the window-created event. Windows that existed before the claim belong to
//                 the user and are never moved. release closes what the agent opened.
//       - park:   borrow one window the user already had; restore hands it back.
//   * Leases expire on their own, so a forgotten release/restore heals itself.
//
// The engine also owns a separate visible agent cursor. Its mouse events go directly to the
// target process; the user's real pointer is never warped, hidden, or borrowed. Keyboard input
// remains outside this tool.

let version = "1.7.0"
let defaultLeaseMinutes = 30.0
let maximumLeaseMinutes = 1440.0

// Seconds left on a lease, safe for any stored value (Int(Double) traps on overflow).
func secondsLeft(_ expiresAt: Double, _ now: Double) -> Int {
    Int(min(max(0, expiresAt - now), maximumLeaseMinutes * 60))
}

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

// MARK: - Physical screens

func screenID(_ screen: NSScreen) -> CGDirectDisplayID {
    (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as! NSNumber).uint32Value
}

// SpaceOKit/Stage.swift identifies its virtual displays with these IDs.
func isSpaceODisplay(_ id: CGDirectDisplayID) -> Bool {
    CGDisplayVendorNumber(id) == 0x1AF2 && CGDisplayModelNumber(id) == 0x0001
}

// Active physical screens, main screen first.
func screens() throws -> [NSScreen] {
    // A CLI process has no running loop, so give AppKit a moment to load the current screen list.
    // Inside the MCP/engine process the main loop is already running and must not be re-entered.
    if RunLoop.current.currentMode == nil {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    let result = NSScreen.screens.filter {
        let id = screenID($0)
        return CGDisplayIsActive(id) != 0 && !isSpaceODisplay(id)
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

// The screen agent windows go to: the built-in panel when several physical screens are attached,
// otherwise the only screen.
func destination() throws -> NSScreen {
    let connected = try screens()
    guard connected.count > 1 else { return connected[0] }
    if let builtIn = connected.first(where: { CGDisplayIsBuiltin(screenID($0)) != 0 }) { return builtIn }
    let primary = CGMainDisplayID()
    return connected.first(where: { screenID($0) != primary }) ?? connected[0]
}

// The agent screen only when placement is meaningful (several physical screens).
func agentScreen() -> NSScreen? {
    guard let connected = try? screens(), connected.count > 1 else { return nil }
    return try? destination()
}

func displayContaining(_ rect: CGRect) -> CGDirectDisplayID? {
    let center = CGPoint(x: rect.midX, y: rect.midY)
    for screen in NSScreen.screens where CGDisplayBounds(screenID(screen)).contains(center) {
        return screenID(screen)
    }
    return nil
}

func isOn(_ rect: CGRect, _ screen: NSScreen) -> Bool {
    displayContaining(rect) == screenID(screen)
}

// Visible area of a screen in Accessibility coordinates (top-left origin), with a small margin.
func usableFrame(_ screen: NSScreen) -> CGRect {
    let mainHeight = CGDisplayBounds(CGMainDisplayID()).height
    let visible = screen.visibleFrame
    return CGRect(x: visible.minX, y: mainHeight - visible.maxY, width: visible.width, height: visible.height)
        .insetBy(dx: 12, dy: 12)
}

// MARK: - Accessibility windows

// Private but long-stable HIServices call (used by Rectangle, yabai, Hammerspoon) that returns the
// CGWindowID behind an AX window. The id survives title changes ("Untitled" -> "report.txt").
@_silgen_name("_AXUIElementGetWindow") @discardableResult
func _AXUIElementGetWindow(_ element: AXUIElement, _ identifier: inout CGWindowID) -> AXError

func axAttribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func windowID(_ window: AXUIElement) -> UInt32? {
    var identifier: CGWindowID = 0
    guard _AXUIElementGetWindow(window, &identifier) == .success, identifier != 0 else { return nil }
    return identifier
}

func windowTitle(_ window: AXUIElement) -> String {
    axAttribute(window, kAXTitleAttribute) as? String ?? ""
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

@discardableResult
func setSize(_ window: AXUIElement, _ size: CGSize) -> Bool {
    var size = size
    guard let value = AXValueCreate(.cgSize, &size) else { return false }
    return AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, value) == .success
}

@discardableResult
func setPosition(_ window: AXUIElement, _ point: CGPoint) -> Bool {
    var point = point
    guard let value = AXValueCreate(.cgPoint, &point) else { return false }
    return AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, value) == .success
}

func isFullScreen(_ window: AXUIElement) -> Bool {
    (axAttribute(window, "AXFullScreen") as? Bool) == true
}

func isMinimized(_ window: AXUIElement) -> Bool {
    (axAttribute(window, kAXMinimizedAttribute) as? Bool) == true
}

// Switches full screen and waits for the animation to finish.
@discardableResult
func setFullScreen(_ window: AXUIElement, _ on: Bool) -> Bool {
    if isFullScreen(window) == on { return true }
    _ = AXUIElementSetAttributeValue(window, "AXFullScreen" as CFString, on ? kCFBooleanTrue : kCFBooleanFalse)
    let deadline = Date().addingTimeInterval(2)
    repeat {
        Thread.sleep(forTimeInterval: 0.1)
        if isFullScreen(window) == on {
            Thread.sleep(forTimeInterval: 0.3)
            return true
        }
    } while Date() < deadline
    return false
}

func closeWindow(_ window: AXUIElement) -> Bool {
    guard let raw = axAttribute(window, kAXCloseButtonAttribute), CFGetTypeID(raw) == AXUIElementGetTypeID() else {
        return false
    }
    return AXUIElementPerformAction(raw as! AXUIElement, kAXPressAction as CFString) == .success
}

// Center a window on a screen, shrinking it to fit, and confirm from the resulting bounds.
func place(_ window: AXUIElement, on screen: NSScreen) throws -> CGRect {
    let original = try windowRect(window)
    let available = usableFrame(screen)
    let size = CGSize(width: min(original.width, available.width), height: min(original.height, available.height))
    if size != original.size, !setSize(window, size) {
        throw ScreenError("窗口太大且应用拒绝调整大小，无法完整放入目标屏。")
    }
    let actualSize = try windowRect(window).size
    guard actualSize.width <= available.width, actualSize.height <= available.height else {
        throw ScreenError("应用的最小窗口尺寸大于目标屏幕。")
    }
    guard setPosition(window, CGPoint(x: available.midX - actualSize.width / 2,
                                      y: available.midY - actualSize.height / 2)) else {
        throw ScreenError("应用拒绝移动这个窗口。")
    }
    let deadline = Date().addingTimeInterval(2)
    repeat {
        Thread.sleep(forTimeInterval: 0.1)
        let actual = try windowRect(window)
        if available.insetBy(dx: -2, dy: -2).contains(actual) { return actual }
    } while Date() < deadline
    throw ScreenError("移动后窗口仍未完整位于目标屏，无法确认放置成功。")
}

// Same placement without the verification loop, for event callbacks: the engine's run loop stays
// responsive and the window lands on the agent screen before it finishes rendering.
func placeFast(_ window: AXUIElement, on screen: NSScreen) {
    guard let original = try? windowRect(window) else { return }
    let available = usableFrame(screen)
    if original.width > 1, original.height > 1 {
        let size = CGSize(width: min(original.width, available.width), height: min(original.height, available.height))
        if size != original.size { setSize(window, size) }
    }
    let actual = (try? windowRect(window).size) ?? original.size
    setPosition(window, CGPoint(x: available.midX - actual.width / 2, y: available.midY - actual.height / 2))
}

// MARK: - State files
//
// ~/.local/state/agent-screen/{parked,claims}.json. Writes are atomic renames, so readers never
// need the lock; every read-modify-write goes through withStateLock so the CLI and the engine in
// a supervised watch or MCP process cannot overwrite each other's changes.

let stateDirectory = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".local/state/agent-screen", isDirectory: true)
let logFile = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Logs/AgentScreen/engine.log")

func ensureStateDirectory() throws {
    try FileManager.default.createDirectory(at: stateDirectory, withIntermediateDirectories: true,
                                            attributes: [.posixPermissions: 0o700])
}

// All state work happens on one thread per process (the main thread), so a depth counter is
// enough to make nested use re-entrant instead of deadlocking on our own flock.
var stateLockDepth = 0

func withStateLock<T>(_ body: () throws -> T) throws -> T {
    if stateLockDepth > 0 { return try body() }
    try ensureStateDirectory()
    let fd = open(stateDirectory.appendingPathComponent("ledger.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { throw ScreenError("无法打开台账锁。") }
    defer { close(fd) }
    guard flock(fd, LOCK_EX) == 0 else { throw ScreenError("无法锁定台账。") }
    stateLockDepth += 1
    defer { stateLockDepth -= 1 }
    return try body()
}

func readLedger<T: Decodable>(_ name: String) throws -> [T] {
    let url = stateDirectory.appendingPathComponent(name)
    guard let data = try? Data(contentsOf: url) else { return [] }
    do { return try JSONDecoder().decode([T].self, from: data) }
    catch { throw ScreenError("台账 \(url.path) 无法解析，请检查后删除：\(error)") }
}

func writeLedger<T: Encodable>(_ name: String, _ list: [T]) throws {
    try ensureStateDirectory()
    let url = stateDirectory.appendingPathComponent(name)
    if list.isEmpty {
        try? FileManager.default.removeItem(at: url)
        return
    }
    try JSONEncoder().encode(list).write(to: url, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
}

func mutateLedger<T: Codable & Equatable, R>(_ name: String, _ body: (inout [T]) throws -> R) throws -> R {
    try withStateLock {
        let before: [T] = try readLedger(name)
        var list = before
        let result = try body(&list)
        if list != before { try writeLedger(name, list) }
        return result
    }
}

// MARK: - Engine heartbeat
//
// The engine lives in a supervised watch or MCP process with Accessibility. Detached launchd
// helpers do not inherit the controller's grant. The heartbeat tells every command whether claims
// will really be enforced.

func heartbeatFile() -> URL {
    stateDirectory.appendingPathComponent("engine.heartbeat")
}

func beatEngine() {
    let stamp = "\(Date().timeIntervalSince1970) \(getpid())"
    try? Data(stamp.utf8).write(to: heartbeatFile(), options: .atomic)
}

func engineIsAlive() -> Bool {
    guard let text = try? String(contentsOf: heartbeatFile(), encoding: .utf8),
          let stamp = Double(text.split(separator: " ").first ?? ""),
          Date().timeIntervalSince1970 - stamp < 3 else { return false }
    return true
}

func clearEngineBeat() {
    // Only the process that wrote the beat may remove it; a second MCP connection that never got
    // the engine lock must not erase the real engine's liveness signal on exit.
    guard let text = try? String(contentsOf: heartbeatFile(), encoding: .utf8),
          let owner = Int32(text.split(separator: " ").last ?? ""), owner == getpid() else { return }
    try? FileManager.default.removeItem(at: heartbeatFile())
}

// MARK: - Parked windows (borrowing a window the user already had)

struct Parked: Codable, Equatable {
    var pid: Int32
    var index: Int
    var title: String
    var windowID: UInt32?
    var x: Double
    var y: Double
    var width: Double
    var height: Double
    var display: UInt32
    var fullscreen: Bool
    var parkedAt: Double
    var expiresAt: Double
    var nextAttemptAt: Double?

    var originalBounds: CGRect { CGRect(x: x, y: y, width: width, height: height) }
}

let parkedLedger = "parked.json"

func loadParked() -> [Parked] { (try? readLedger(parkedLedger)) ?? [] }

func mutateParked<R>(_ body: (inout [Parked]) throws -> R) throws -> R {
    try mutateLedger(parkedLedger, body)
}

enum Lookup {
    case found(AXUIElement)
    case gone       // the app quit or the window was closed
    case unknown    // windows could not be read right now; keep the record
}

// The window server knows every window on every Space, unlike kAXWindowsAttribute, which only
// lists the current Space. Only it may declare a window closed.
func windowExists(_ id: UInt32) -> Bool {
    guard let info = CGWindowListCopyWindowInfo([.optionIncludingWindow], CGWindowID(id)) as? [[String: Any]] else {
        return true
    }
    return info.contains { ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == id }
}

// Every window id the app owns on any Space or display, including tabs and full-screen windows
// that kAXWindowsAttribute does not list. Needs no screen-recording permission.
func allWindowIDs(_ pid: pid_t) -> [UInt32] {
    guard let info = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] else { return [] }
    return info.compactMap { entry in
        guard (entry[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid else { return nil }
        return (entry[kCGWindowNumber as String] as? NSNumber)?.uint32Value
    }
}

func lookup(_ item: Parked) -> Lookup {
    guard NSRunningApplication(processIdentifier: item.pid) != nil else { return .gone }
    guard let list = try? windows(item.pid) else { return .unknown }
    if let id = item.windowID {
        if let window = list.first(where: { windowID($0) == id }) { return .found(window) }
        return windowExists(id) ? .unknown : .gone
    }
    // Records written before window ids existed (1.4.0).
    if !item.title.isEmpty {
        return list.first(where: { windowTitle($0) == item.title }).map(Lookup.found) ?? .unknown
    }
    return list.indices.contains(item.index) ? .found(list[item.index]) : .unknown
}

enum RestoreOutcome: String {
    case restored
    case windowClosed = "window_closed"
    case failed
}

func restoreOne(_ item: Parked) -> RestoreOutcome {
    let window: AXUIElement
    switch lookup(item) {
    case .found(let found): window = found
    case .gone: return .windowClosed
    case .unknown: return .failed
    }
    setFullScreen(window, false)
    // The original screen may have been unplugged meanwhile. With one screen left the window is
    // already on it and stays untouched; otherwise it goes to the main screen, the user's screen.
    guard CGDisplayIsOnline(item.display) != 0, !CGDisplayBounds(item.display).isEmpty else {
        if agentScreen() == nil { return .restored }
        guard let main = NSScreen.screens.first(where: { screenID($0) == CGMainDisplayID() }),
              (try? place(window, on: main)) != nil else { return .failed }
        if item.fullscreen { setFullScreen(window, true) }
        return .restored
    }
    setSize(window, CGSize(width: item.width, height: item.height))
    Thread.sleep(forTimeInterval: 0.08)
    guard setPosition(window, CGPoint(x: item.x, y: item.y)) else { return .failed }
    let target = CGDisplayBounds(item.display)
    let deadline = Date().addingTimeInterval(1.5)
    repeat {
        Thread.sleep(forTimeInterval: 0.1)
        if let actual = try? windowRect(window), target.contains(CGPoint(x: actual.midX, y: actual.midY)) {
            // Re-enter full screen only once the window is back on its own display.
            if item.fullscreen { setFullScreen(window, true) }
            return .restored
        }
    } while Date() < deadline
    return .failed
}

// The promises of a park that must not depend on the agent remembering anything: an expired lease
// goes back where the user had it, and a closed window drops its record. Used by status, park,
// restore and the engine alike.
func settle(_ parked: inout [Parked], log: ((String) -> Void)? = nil) {
    guard AXIsProcessTrusted() else { return }
    let now = Date().timeIntervalSince1970
    parked = parked.compactMap { item in
        if item.expiresAt > now {
            if case .gone = lookup(item) {
                log?("park window closed, dropped record pid=\(item.pid) \(item.title)")
                return nil
            }
            return item
        }
        if let next = item.nextAttemptAt, next > now { return item }
        switch restoreOne(item) {
        case .restored:
            log?("park lease expired, restored pid=\(item.pid) \(item.title)")
            return nil
        case .windowClosed:
            log?("park lease expired, window already closed pid=\(item.pid) \(item.title)")
            return nil
        case .failed:
            // Keep retrying (the window may be on another Space for a while), but give up after
            // ten minutes so one unrestorable window cannot be retried forever.
            if now - item.expiresAt > 600 {
                log?("park lease expired, restore kept failing for 10 min, dropped pid=\(item.pid) \(item.title)")
                return nil
            }
            // Back off: a failing restore costs seconds on the main thread under the ledger lock.
            var retry = item
            retry.nextAttemptAt = now + 15
            return retry
        }
    }
}

@discardableResult
func settleParked(log: ((String) -> Void)? = nil) -> [Parked] {
    if loadParked().isEmpty { return [] }
    return (try? mutateParked { (parked: inout [Parked]) -> [Parked] in
        settle(&parked, log: log)
        return parked
    }) ?? loadParked()
}

func park(pid: pid_t, index: Int, minutes: Double) throws -> [String: Any] {
    let connected = try screens()
    guard connected.count > 1 else {
        return ["mode": "single_physical_screen", "parked": false, "virtual_screen_required": true,
                "message": "单物理屏不使用 park；请用 agent-spaceo 创建虚拟屏会话。"]
    }
    let target = try destination()
    // Settle first: an expired lease restored inside the update below would otherwise leave
    // `current` pointing at the agent screen and record that as the "original" position.
    settleParked()
    let appWindows = try windows(pid)
    guard appWindows.indices.contains(index) else { throw ScreenError("窗口序号不存在，请重新读取 status(pid)。") }
    let window = appWindows[index]
    guard !isMinimized(window) else { throw ScreenError("请先恢复目标窗口再放置。") }
    let fullscreen = isFullScreen(window)
    if fullscreen, !setFullScreen(window, false) { throw ScreenError("无法退出全屏，请先手动退出。") }
    let current = try windowRect(window)
    let id = windowID(window)
    let title = windowTitle(window)
    let now = Date().timeIntervalSince1970
    let lifetime = max(1, minutes) * 60
    let item: Parked = try mutateParked { parked in
        settle(&parked)
        let same: (Parked) -> Bool = {
            guard $0.pid == pid else { return false }
            if let id, let known = $0.windowID { return id == known }
            return title.isEmpty ? $0.index == index : $0.title == title
        }
        if let position = parked.firstIndex(where: same) {
            // Already parked once: keep the very first original geometry, only extend the lease.
            var item = parked.remove(at: position)
            item.index = index
            item.windowID = id ?? item.windowID
            if !title.isEmpty { item.title = title }
            item.parkedAt = now
            item.expiresAt = now + lifetime
            item.nextAttemptAt = nil
            parked.append(item)
            return item
        }
        let item = Parked(pid: pid, index: index, title: title, windowID: id,
                          x: current.minX, y: current.minY, width: current.width, height: current.height,
                          display: displayContaining(current) ?? CGMainDisplayID(),
                          fullscreen: fullscreen, parkedAt: now, expiresAt: now + lifetime, nextAttemptAt: nil)
        parked.append(item)
        return item
    }
    let alreadyThere = isOn(current, target)
    let bounds = alreadyThere ? current : try place(window, on: target)
    return ["parked": true, "pid": pid, "window_index": index, "title": item.title,
            "target_display_id": screenID(target), "target_screen": target.localizedName,
            "bounds": rectJSON(bounds), "already_on_target": alreadyThere,
            "restore_to_display": Int(item.display), "restore_to_bounds": rectJSON(item.originalBounds),
            "expires_in_minutes": secondsLeft(now + lifetime, now) / 60]
}

func restore(pid: pid_t?, index: Int?, all: Bool) throws -> [String: Any] {
    try mutateParked { parked in
        let hit: (Parked) -> Bool = { all || ($0.pid == pid && (index == nil || $0.index == index)) }
        var others = parked.filter { !hit($0) }
        settle(&others)
        var kept = others
        var handled: [[String: Any]] = []
        for item in parked where hit(item) {
            let outcome = restoreOne(item)
            var entry: [String: Any] = ["pid": Int(item.pid), "title": item.title, "status": outcome.rawValue]
            if outcome == .restored { entry["display"] = Int(item.display) }
            if outcome == .failed { kept.append(item) }
            handled.append(entry)
        }
        parked = kept
        var result: [String: Any] = ["restored": handled, "matched": handled.count, "remaining": kept.count]
        if handled.isEmpty { result["message"] = "没有这个窗口的停放记录。" }
        return result
    }
}

// MARK: - App claims (the agent drives an app and opens its own windows)

struct ClaimWindow: Codable, Equatable {
    var title: String
    var windowID: UInt32?
    var x: Double
    var y: Double
    var width: Double
    var height: Double
    var display: UInt32
}

struct Claim: Codable, Equatable {
    var pid: Int32
    var bundleID: String
    var name: String
    var startedAt: Double
    var expiresAt: Double
    var userWindows: [ClaimWindow]
    var agentWindows: [String]          // titles, still written for older readers
    var agentWindowIDs: [UInt32]?
    var userWindowIDs: [UInt32]?        // every window the app had on any Space at claim time

    // A window that existed when the claim began. A window whose id cannot be read is treated as
    // the user's: a claim must never move or close a user window by mistake.
    func ownsUserWindow(_ window: AXUIElement) -> Bool {
        guard let id = windowID(window) else { return true }
        if let ids = userWindowIDs { return ids.contains(id) }
        // Claims written by 1.4.0 only know titles.
        let title = windowTitle(window)
        return userWindows.contains { $0.windowID == id || (!title.isEmpty && $0.title == title) }
    }

    func ownsAgentWindow(_ window: AXUIElement) -> Bool {
        guard !ownsUserWindow(window), let id = windowID(window) else { return false }
        if let ids = agentWindowIDs { return ids.contains(id) }
        let title = windowTitle(window)
        return !title.isEmpty && agentWindows.contains(title)
    }

    mutating func record(_ window: AXUIElement) {
        if let id = windowID(window), !(agentWindowIDs ?? []).contains(id) {
            agentWindowIDs = (agentWindowIDs ?? []) + [id]
        }
        let title = windowTitle(window)
        if !title.isEmpty, !agentWindows.contains(title) { agentWindows.append(title) }
    }
}

let claimsLedger = "claims.json"

func loadClaims() -> [Claim] { (try? readLedger(claimsLedger)) ?? [] }

func mutateClaims<R>(_ body: (inout [Claim]) throws -> R) throws -> R {
    try mutateLedger(claimsLedger, body)
}

func claim(pid: pid_t, minutes: Double) throws -> [String: Any] {
    let connected = try screens()
    guard connected.count > 1 else {
        return ["mode": "single_physical_screen", "claimed": false, "virtual_screen_required": true,
                "message": "单物理屏不使用 claim；请用 agent-spaceo 创建虚拟屏会话。"]
    }
    guard let app = NSRunningApplication(processIdentifier: pid) else { throw ScreenError("应用 PID 不存在。") }
    let target = try destination()
    // Must succeed: an empty snapshot would hand every existing user window to the agent.
    let snapshots: [ClaimWindow] = try windows(pid).map { window in
        let rect = (try? windowRect(window)) ?? .zero
        return ClaimWindow(title: windowTitle(window), windowID: windowID(window),
                           x: rect.minX, y: rect.minY, width: rect.width, height: rect.height,
                           display: displayContaining(rect) ?? CGMainDisplayID())
    }
    let now = Date().timeIntervalSince1970
    let lifetime = max(1, minutes) * 60
    let bundleID = app.bundleIdentifier ?? ""
    let (extended, current): (Bool, Claim) = try mutateClaims { claims in
        if let index = claims.firstIndex(where: { $0.pid == pid && $0.bundleID == bundleID }) {
            // Claiming again only extends the lease. Re-snapshotting would turn the agent's own
            // windows into "user windows" that release could no longer close.
            claims[index].expiresAt = now + lifetime
            return (true, claims[index])
        }
        claims.removeAll { $0.pid == pid }
        let fresh = Claim(pid: pid, bundleID: bundleID, name: app.localizedName ?? "",
                          startedAt: now, expiresAt: now + lifetime,
                          userWindows: snapshots, agentWindows: [], agentWindowIDs: [],
                          userWindowIDs: Array(Set(allWindowIDs(pid) + snapshots.compactMap(\.windowID))))
        claims.append(fresh)
        return (false, fresh)
    }
    let live = engineIsAlive()
    var result: [String: Any] = [
        "claimed": true, "extended": extended, "pid": pid, "name": current.name, "bundle_id": bundleID,
        "target_screen": target.localizedName, "user_windows_left_alone": current.userWindows.count,
        "engine_running": live, "expires_in_minutes": secondsLeft(now + lifetime, now) / 60,
        "rule": "接管期间这个应用新开的窗口只在副屏出现；release 时关闭它们。接管前已存在的窗口属于用户，不会被移动。"]
    if !live {
        result["warning"] = "接管引擎没有运行，新窗口不会被自动移到副屏。请以受管后台运行 agent-screen watch，或连接 agent-screen MCP；后台生效前不要创建新窗口。"
    }
    return result
}

func releaseClaim(_ claim: Claim) -> [[String: Any]] {
    guard NSRunningApplication(processIdentifier: claim.pid) != nil else { return [["status": "app_gone"]] }
    guard let list = try? windows(claim.pid) else { return [["status": "windows_unreadable"]] }
    return list.filter { claim.ownsAgentWindow($0) }.map { window in
        ["title": windowTitle(window), "status": closeWindow(window) ? "closed" : "close_refused"]
    }
}

func release(pid: pid_t?, all: Bool) throws -> [String: Any] {
    try mutateClaims { claims in
        var kept: [Claim] = []
        var handled: [[String: Any]] = []
        for claim in claims {
            guard all || claim.pid == pid else { kept.append(claim); continue }
            let windows = releaseClaim(claim)
            // Unreadable right now: keep the claim so its windows can still be closed later.
            if windows.first?["status"] as? String == "windows_unreadable" { kept.append(claim) }
            handled.append(["pid": Int(claim.pid), "name": claim.name, "windows": windows])
        }
        claims = kept
        return ["matched": handled.count, "released": handled, "remaining": kept.count]
    }
}

func recordAgentWindows(pid: Int32, _ adopted: [AXUIElement]) {
    guard !adopted.isEmpty else { return }
    try? mutateClaims { claims in
        guard let index = claims.firstIndex(where: { $0.pid == pid }) else { return }
        for window in adopted { claims[index].record(window) }
    }
}

// Move every window of a claimed app that is not one of the user's windows onto the agent screen.
// Returns the number of windows moved, or -1 when the app is not claimed.
func sweepClaim(pid: Int32) -> Int {
    guard let claim = loadClaims().first(where: { $0.pid == pid }) else { return -1 }
    guard let target = agentScreen(), let list = try? windows(pid) else { return 0 }
    var adopted: [AXUIElement] = []
    var moved = 0
    for window in list where !claim.ownsUserWindow(window) {
        adopted.append(window)
        guard let rect = try? windowRect(window), !isOn(rect, target) else { continue }
        placeFast(window, on: target)
        moved += 1
    }
    recordAgentWindows(pid: pid, adopted)
    return moved
}

// Claims end when their lease expires (agent windows are closed) or when the app quits.
func settleClaims(log: ((String) -> Void)? = nil) {
    if loadClaims().isEmpty { return }
    let now = Date().timeIntervalSince1970
    try? mutateClaims { claims in
        claims = claims.filter { claim in
            if NSRunningApplication(processIdentifier: claim.pid) == nil {
                log?("claim ended, app quit pid=\(claim.pid) \(claim.name)")
                return false
            }
            guard claim.expiresAt <= now else { return true }
            let detail = releaseClaim(claim)
                .map { "\($0["title"] as? String ?? "-")=\($0["status"] as? String ?? "-")" }
                .joined(separator: ",")
            log?("claim expired, released pid=\(claim.pid) \(claim.name) [\(detail)]")
            return false
        }
    }
}

// Owns the AX observers that turn a claim into an enforced rule.
final class ClaimEngine {
    var log: ((String) -> Void)?
    private var observers: [Int32: AXObserver] = [:]
    private var pending: Set<Int32> = []

    func sync() {
        let claims = loadClaims()
        let pids = Set(claims.map(\.pid))
        for pid in Array(observers.keys) where !pids.contains(pid) { removeObserver(pid) }
        for pid in pids where observers[pid] == nil { addObserver(pid) }
        // Catch anything created between "claim" and the observer being installed, and keep
        // catching windows whose creation event an app never posts.
        for claim in claims {
            let moved = sweepClaim(pid: claim.pid)
            if moved > 0 { log?("swept \(moved) window(s) of pid=\(claim.pid)") }
        }
    }

    private func addObserver(_ pid: Int32) {
        guard NSRunningApplication(processIdentifier: pid) != nil else { return }
        let context = Unmanaged.passUnretained(self).toOpaque()
        var observer: AXObserver?
        guard AXObserverCreate(pid, { _, element, notification, refcon in
            guard let refcon else { return }
            Unmanaged<ClaimEngine>.fromOpaque(refcon).takeUnretainedValue().receive(notification as String, element: element)
        }, &observer) == .success, let observer else { return }
        let app = AXUIElementCreateApplication(pid)
        for name in [kAXWindowCreatedNotification, kAXFocusedWindowChangedNotification, kAXWindowMovedNotification] {
            _ = AXObserverAddNotification(observer, app, name as CFString, context)
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
        observers[pid] = observer
    }

    private func removeObserver(_ pid: Int32) {
        guard let observer = observers.removeValue(forKey: pid) else { return }
        CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
    }

    private func receive(_ notification: String, element: AXUIElement) {
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success, pid > 0 else { return }
        if notification == kAXWindowCreatedNotification as String {
            adopt(pid: pid, window: element)
        } else {
            schedule(pid: pid)
        }
    }

    private func schedule(pid: Int32) {
        guard !pending.contains(pid) else { return }
        pending.insert(pid)
        DispatchQueue.main.async { [weak self] in
            self?.pending.remove(pid)
            self?.reassert(pid: pid)
        }
    }

    // A brand-new window: place it at once (no waiting for the app to settle its size), then
    // correct a few times shortly after, because some apps re-apply their own frame just after
    // creation. Corrections stop as soon as the window sits on the agent screen.
    private func adopt(pid: Int32, window: AXUIElement) {
        guard let claim = loadClaims().first(where: { $0.pid == pid }), !claim.ownsUserWindow(window) else { return }
        moveIfNeeded(window)
        recordAgentWindows(pid: pid, [window])
        log?("adopted pid=\(pid) \(windowTitle(window))")
        for delay in [0.05, 0.12, 0.25, 0.45] {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in self?.moveIfNeeded(window) }
        }
    }

    private func moveIfNeeded(_ window: AXUIElement) {
        guard let target = agentScreen(), let rect = try? windowRect(window), !isOn(rect, target) else { return }
        placeFast(window, on: target)
    }

    // Any later event that drags an agent window back to the user's screen is undone at once.
    private func reassert(pid: Int32) {
        guard let claim = loadClaims().first(where: { $0.pid == pid }), let list = try? windows(pid) else { return }
        for window in list where claim.ownsAgentWindow(window) { moveIfNeeded(window) }
    }
}

// MARK: - Independent agent cursor
//
// The visible arrow is an NSPanel owned by the engine, not the WindowServer's single hardware
// cursor. Coordinate clicks go straight to the owning process, never through the global event
// stream. There is deliberately no HID-mouse fallback: if this path cannot serve a request, the
// user's real pointer stays untouched.

// CGEventField has no public enum case for the target window number. Fields 51 and 58 plus a
// window-local location are required for AppKit to accept a process-direct mouse event. The
// location setter is exported by CoreGraphics but absent from its public headers, like the AX
// window-id bridge above. These only annotate the event; they do not move the hardware pointer.
let cursorWindowNumberField = unsafeBitCast(UInt32(51), to: CGEventField.self)
let cursorWindowLocationField = unsafeBitCast(UInt32(58), to: CGEventField.self)

@_silgen_name("CGEventSetWindowLocation")
func setCursorWindowLocation(_ event: CGEvent?, _ point: CGPoint)

func validateCursorRequest(_ request: [String: Any]) throws -> String {
    guard let action = request["action"] as? String,
          ["status", "move", "click"].contains(action) else {
        throw ScreenError("cursor action 必须是 status、move 或 click。")
    }
    let fields: Set<String> = action == "status" ? ["action"] :
        (action == "move" ? ["action", "x", "y"] : ["action", "x", "y", "pid", "double", "shift"])
    guard Set(request.keys).isSubset(of: fields) else { throw ScreenError("cursor 包含未支持的参数。") }
    if action != "status" {
        for name in ["x", "y"] {
            guard let value = request[name] as? NSNumber,
                  CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite,
                  abs(value.doubleValue) <= 1_000_000 else {
                throw ScreenError("需要有效坐标：\(name)。")
            }
        }
    }
    if action == "click" {
        _ = try integerArgument(request, "pid", minimum: 1)
        for name in ["double", "shift"] where request[name] != nil {
            guard let value = request[name] as? NSNumber,
                  CFGetTypeID(value) == CFBooleanGetTypeID() else {
                throw ScreenError("需要布尔参数：\(name)。")
            }
        }
    }
    return action
}

final class AgentCursorView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath()
        path.move(to: NSPoint(x: 2, y: 30))
        path.line(to: NSPoint(x: 2, y: 7))
        path.line(to: NSPoint(x: 8, y: 13))
        path.line(to: NSPoint(x: 12, y: 4))
        path.line(to: NSPoint(x: 17, y: 7))
        path.line(to: NSPoint(x: 13, y: 16))
        path.line(to: NSPoint(x: 21, y: 16))
        path.close()
        NSColor.systemBlue.setFill()
        path.fill()
        NSColor.black.setStroke()
        path.lineWidth = 4
        path.stroke()
        NSColor.white.setStroke()
        path.lineWidth = 1.6
        path.stroke()
    }
}

final class AgentCursor {
    private var panel: NSPanel?
    private var server: CursorServer?
    private var position = CGPoint.zero
    private let hotspot = CGPoint(x: 2, y: 30)

    func start() throws {
        _ = NSApplication.shared
        NSApplication.shared.setActivationPolicy(.accessory)
        server = try CursorServer(cursor: self)
        sync()
    }

    func sync() {
        guard let target = agentScreen() else {
            panel?.orderOut(nil)
            return
        }
        if panel == nil { panel = makePanel() }
        let available = usableFrame(target)
        if !available.contains(position) {
            position = CGPoint(x: available.midX, y: available.midY)
        }
        layout()
        if panel?.isVisible == false { panel?.orderFrontRegardless() }
    }

    func handle(_ request: [String: Any]) throws -> [String: Any] {
        let action = try validateCursorRequest(request)
        switch action {
        case "status":
            sync()
            return status()
        case "move":
            let point = try point(from: request)
            try move(to: point)
            return status()
        case "click":
            let point = try point(from: request)
            let pid = try integerArgument(request, "pid", minimum: 1)
            return try click(at: point, expectedPID: pid_t(pid),
                             double: (request["double"] as? NSNumber)?.boolValue ?? false,
                             shift: (request["shift"] as? NSNumber)?.boolValue ?? false)
        default:
            throw ScreenError("未知光标操作。")
        }
    }

    private func point(from request: [String: Any]) throws -> CGPoint {
        guard let x = request["x"] as? NSNumber, let y = request["y"] as? NSNumber else {
            throw ScreenError("光标操作需要坐标。")
        }
        return CGPoint(x: x.doubleValue, y: y.doubleValue)
    }

    private func makePanel() -> NSPanel {
        let result = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 24, height: 32),
                             styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        result.backgroundColor = .clear
        result.isOpaque = false
        result.hasShadow = false
        result.ignoresMouseEvents = true
        result.hidesOnDeactivate = false
        result.isReleasedWhenClosed = false
        result.level = .statusBar
        result.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        result.contentView = AgentCursorView(frame: NSRect(x: 0, y: 0, width: 24, height: 32))
        return result
    }

    private func layout() {
        guard let panel else { return }
        let mainHeight = CGDisplayBounds(CGMainDisplayID()).height
        panel.setFrameOrigin(NSPoint(x: position.x - hotspot.x,
                                     y: mainHeight - position.y - hotspot.y))
    }

    private func status() -> [String: Any] {
        let connected = (try? screens()) ?? []
        let multi = connected.count > 1
        var result: [String: Any] = [
            "mode": multi ? "second_physical_screen" : "single_physical_screen",
            "visible": multi && panel?.isVisible == true,
            "position": ["x": position.x, "y": position.y],
            "independent": true
        ]
        if multi, let target = try? destination() {
            result["target_display_id"] = screenID(target)
            result["target_screen"] = target.localizedName
            result["target_built_in"] = CGDisplayIsBuiltin(screenID(target)) != 0
        }
        return result
    }

    private func move(to point: CGPoint) throws {
        sync()
        guard let target = agentScreen(), panel?.isVisible == true else {
            throw ScreenError("独立光标后台不可用，未移动真实指针。")
        }
        guard usableFrame(target).contains(point) else {
            throw ScreenError("目标点不在本机内置屏的可操作区域内，已拒绝。")
        }
        position = point
        layout()
    }

    private func windowTarget(at point: CGPoint, expectedPID: pid_t) throws -> (pid: pid_t, id: UInt32, bounds: CGRect) {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                     kCGNullWindowID) as? [[String: Any]] else {
            throw ScreenError("无法读取目标窗口。")
        }
        for entry in list {
            guard let rawPID = entry[kCGWindowOwnerPID as String] as? NSNumber,
                  let rawID = entry[kCGWindowNumber as String] as? NSNumber,
                  let rawBounds = entry[kCGWindowBounds as String] as? [String: Any],
                  let bounds = CGRect(dictionaryRepresentation: rawBounds as CFDictionary) else { continue }
            let pid = rawPID.int32Value
            let id = rawID.uint32Value
            if pid == getpid() || id == UInt32(panel?.windowNumber ?? -1) { continue }
            if bounds.contains(point) {
                guard pid == expectedPID else {
                    throw ScreenError("目标点当前属于 PID \(pid)，不是指定任务 PID \(expectedPID)，已拒绝点击。")
                }
                return (pid, id, bounds)
            }
        }
        throw ScreenError("内置屏目标点没有可接收点击的窗口，未发送鼠标事件。")
    }

    private func click(at point: CGPoint, expectedPID: pid_t, double: Bool, shift: Bool) throws -> [String: Any] {
        try move(to: point)
        let target = try windowTarget(at: point, expectedPID: expectedPID)
        let localPoint = CGPoint(x: point.x - target.bounds.minX, y: point.y - target.bounds.minY)
        let flags: CGEventFlags = shift ? .maskShift : []

        func post(_ type: CGEventType, clickState: Int64 = 0) throws {
            guard let event = CGEvent(mouseEventSource: nil, mouseType: type,
                                      mouseCursorPosition: point, mouseButton: .left) else {
                throw ScreenError("无法创建鼠标事件。")
            }
            event.flags = flags
            event.setIntegerValueField(.mouseEventClickState, value: clickState)
            event.setIntegerValueField(cursorWindowNumberField, value: Int64(target.id))
            event.setIntegerValueField(cursorWindowLocationField, value: 12345)
            setCursorWindowLocation(event, localPoint)
            event.postToPid(target.pid)
        }

        for clickState in double ? [1, 2] : [1] {
            try post(.leftMouseDown, clickState: Int64(clickState))
            Thread.sleep(forTimeInterval: 0.04)
            try post(.leftMouseUp, clickState: Int64(clickState))
            if double && clickState == 1 { Thread.sleep(forTimeInterval: 0.09) }
        }
        var result = status()
        result["posted"] = double ? 4 : 2
        result["target_pid"] = Int(target.pid)
        result["target_window_id"] = Int(target.id)
        return result
    }
}

// MARK: - Local cursor command channel

func withSocketAddress<T>(_ path: String,
                          _ body: (UnsafePointer<sockaddr>, socklen_t) throws -> T) throws -> T {
    guard path.utf8.count < 104 else { throw ScreenError("本机光标通道路径过长。") }
    var address = sockaddr_un()
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    address.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutablePointer(to: &address.sun_path) { pointer in
        pointer.withMemoryRebound(to: CChar.self, capacity: 104) { destination in
            path.withCString { source in _ = strncpy(destination, source, 103) }
        }
    }
    return try withUnsafePointer(to: &address) { pointer in
        try pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            try body($0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
}

func writeSocket(_ fd: Int32, _ data: Data) throws {
    try data.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return }
        var offset = 0
        while offset < data.count {
            let count = send(fd, base.advanced(by: offset), data.count - offset, MSG_NOSIGNAL)
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { throw ScreenError("本机光标通道写入失败。") }
            offset += count
        }
    }
}

func readSocketFrame(_ fd: Int32) throws -> Data {
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while true {
        let count = buffer.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
        if count < 0 && errno == EINTR { continue }
        guard count >= 0 else { throw ScreenError("本机光标通道读取失败。") }
        if count == 0 {
            guard !data.isEmpty else { throw ScreenError("本机光标通道已关闭。") }
            return data
        }
        data.append(contentsOf: buffer.prefix(count))
        if let newline = data.firstIndex(of: 10) { return data.prefix(newline) }
        guard data.count <= 65_536 else { throw ScreenError("光标请求超过 64 KiB。") }
    }
}

func cursorSocketPath() -> String {
    stateDirectory.appendingPathComponent("cursor.sock").path
}

func cursorRequest(_ request: [String: Any]) throws -> [String: Any] {
    _ = try validateCursorRequest(request)
    guard ProcessInfo.processInfo.environment["AGENT_SCREEN_ENGINE"] != "off" else {
        throw ScreenError("测试模式已禁用光标命令。")
    }
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { throw ScreenError("无法创建本机光标通道。") }
    defer { close(fd) }
    var timeout = timeval(tv_sec: 3, tv_usec: 0)
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
    let connected = try withSocketAddress(cursorSocketPath()) { address, length in
        connect(fd, address, length)
    }
    guard connected == 0 else {
        throw ScreenError("独立光标后台未连接；请先以受管后台运行 agent-screen watch，或连接 agent-screen MCP。")
    }
    var data = try JSONSerialization.data(withJSONObject: request, options: [.sortedKeys])
    data.append(10)
    try writeSocket(fd, data)
    let reply = try readSocketFrame(fd)
    guard let object = try JSONSerialization.jsonObject(with: reply) as? [String: Any],
          let ok = object["ok"] as? NSNumber else {
        throw ScreenError("光标后台返回无效响应。")
    }
    if ok.boolValue {
        guard let result = object["result"] as? [String: Any] else {
            throw ScreenError("光标后台缺少结果。")
        }
        return result
    }
    throw ScreenError(object["error"] as? String ?? "光标后台拒绝了请求。")
}

final class CursorServer {
    private weak var cursor: AgentCursor?
    private let fd: Int32
    private var thread: Thread?

    init(cursor: AgentCursor) throws {
        self.cursor = cursor
        try ensureStateDirectory()
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw ScreenError("无法创建光标服务。") }
        let path = cursorSocketPath()
        unlink(path)
        let bound = try withSocketAddress(path) { address, length in
            bind(fd, address, length)
        }
        guard bound == 0 else { close(fd); throw ScreenError("无法绑定本机光标通道。") }
        guard chmod(path, 0o600) == 0, listen(fd, 16) == 0 else {
            close(fd)
            unlink(path)
            throw ScreenError("无法启动本机光标通道。")
        }
        let thread = Thread { [weak self] in self?.acceptLoop() }
        thread.name = "agent-screen-cursor"
        thread.stackSize = 1 << 20
        self.thread = thread
        thread.start()
    }

    deinit {
        shutdown(fd, SHUT_RDWR)
        close(fd)
        unlink(cursorSocketPath())
    }

    private func acceptLoop() {
        while true {
            let client = accept(fd, nil, nil)
            if client < 0 && errno == EINTR { continue }
            guard client >= 0 else { break }
            handle(client)
        }
    }

    private func handle(_ client: Int32) {
        defer { close(client) }
        var timeout = timeval(tv_sec: 3, tv_usec: 0)
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        let response: [String: Any]
        do {
            let data = try readSocketFrame(client)
            guard let request = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw ScreenError("光标请求必须是 JSON 对象。")
            }
            let result: [String: Any] = try DispatchQueue.main.sync {
                guard let cursor else { throw ScreenError("光标后台已经停止。") }
                return try cursor.handle(request)
            }
            response = ["ok": true, "result": result]
        } catch {
            response = ["ok": false, "error": String(describing: error)]
        }
        if let data = try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]) {
            try? writeSocket(client, data + Data([10]))
        }
    }
}

// Runs the engine: settles parked leases, ends claims and enforces claimed apps every 0.2 s.
final class Service {
    private let lockFD: Int32
    private let engine = ClaimEngine()
    let cursor = AgentCursor()
    private var timer: Timer?
    // stdout is the JSON-RPC channel when this binary runs as an MCP server, so engine logging
    // only goes there in `watch` mode.
    var mirrorToStdout = false

    init() throws {
        try ensureStateDirectory()
        lockFD = open(stateDirectory.appendingPathComponent("watch.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard lockFD >= 0 else { throw ScreenError("无法打开引擎锁。") }
        // A class whose stored properties are all set still runs deinit when init throws, so
        // deinit is the single place that closes the descriptor.
        guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { throw ScreenError("引擎已在另一个进程中运行。") }
    }

    deinit { if lockFD >= 0 { close(lockFD) } }

    func log(_ text: String) {
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(text)\n"
        if mirrorToStdout {
            print(line, terminator: "")
            fflush(stdout)
        }
        try? FileManager.default.createDirectory(at: logFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let handle = try? FileHandle(forWritingTo: logFile) {
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
            try? handle.close()
        } else {
            try? Data(line.utf8).write(to: logFile)
        }
    }

    private func tick() {
        let log: (String) -> Void = { [weak self] text in self?.log(text) }
        settleParked(log: log)
        settleClaims(log: log)
        engine.sync()
        cursor.sync()
        beatEngine()
    }

    // Starts without blocking, so the engine can run inside the MCP server's main run loop.
    func start() -> Bool {
        guard AXIsProcessTrusted() else {
            log("cannot start engine: accessibility permission required (pid=\(getpid()))")
            return false
        }
        do {
            try cursor.start()
        } catch {
            log("cannot start independent cursor: \(error)")
            return false
        }
        engine.log = { [weak self] text in self?.log(text) }
        log("engine up pid=\(getpid()) version=\(version)")
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.tick() }
        DispatchQueue.main.async { [weak self] in self?.tick() }
        return true
    }

    func run() -> Never {
        mirrorToStdout = true
        guard start() else { exit(1) }
        while true { _ = RunLoop.main.run(mode: .default, before: .distantFuture) }
    }
}

// MARK: - Route and status

func route() throws -> [String: Any] {
    let connected = try screens()
    let target = try destination()
    let multi = connected.count > 1
    let live = engineIsAlive()
    return [
        "mode": multi ? "second_physical_screen" : "single_physical_screen",
        "placement_required": true,
        "virtual_screen_required": !multi,
        "target_kind": multi ? "built_in" : "spaceo_virtual",
        "browser_route": "managed_headless",
        "browser_command": "agent-browser",
        "browser_viewer": "agent-browser show --session ID",
        "instruction": multi
            ? "浏览器先用受管 agent-browser daemon 和隔离无窗口会话，观看时由用户主动 show；其他 GUI 新任务窗口先 claim(pid)，结束 release；借用已有窗口才 park / restore。engine_running 为 false 时先以受管后台运行 agent-screen watch，或连接 MCP，后台生效前不要创建新窗口。坐标点击使用独立 cursor 并指定任务 PID；控制端主界面留在用户主屏。"
            : "浏览器先用受管 agent-browser daemon 和隔离无窗口会话，观看时由用户主动 show；其他 GUI 按共享 agent-screen Skill 使用 SpaceO 会话，先以受管后台运行 agent-spaceo daemon，再 agent-spaceo create 创建共享虚拟屏的独立任务会话，任务窗口和会话光标只在对应区域，结束 destroy。不要直接操作真实屏。",
        "target_display_id": screenID(target),
        "target_screen": target.localizedName,
        "target_built_in": multi && CGDisplayIsBuiltin(screenID(target)) != 0,
        "accessibility": AXIsProcessTrusted(),
        "engine_running": live,
        "screens": connected.enumerated().map { index, screen in
            ["number": index + 1, "id": screenID(screen), "name": screen.localizedName,
             "built_in": CGDisplayIsBuiltin(screenID(screen)) != 0,
             "bounds": rectJSON(CGDisplayBounds(screenID(screen)))] as [String: Any]
        }
    ]
}

// Read-only: never settles leases or moves a window.
func prepare() throws -> [String: Any] { try route() }

// Route plus ledgers. Reading the status also settles expired leases, so a stranded window heals
// even when no engine is running.
func status(pid: pid_t? = nil) throws -> [String: Any] {
    var result = try route()
    let parked = settleParked()
    let now = Date().timeIntervalSince1970
    result["claimed_apps"] = loadClaims().map {
        ["pid": Int($0.pid), "name": $0.name, "user_windows_left_alone": $0.userWindows.count,
         "agent_windows": $0.agentWindows, "expires_in_seconds": secondsLeft($0.expiresAt, now)] as [String: Any]
    }
    result["parked"] = parked.map {
        ["pid": Int($0.pid), "title": $0.title, "window_index": $0.index,
         "original_display": Int($0.display), "original_bounds": rectJSON($0.originalBounds),
         "expires_in_seconds": secondsLeft($0.expiresAt, now)] as [String: Any]
    }
    if let pid {
        result["pid"] = pid
        result["windows"] = try windows(pid).enumerated().map { index, window in
            ["index": index, "title": windowTitle(window), "bounds": rectJSON(try windowRect(window))] as [String: Any]
        }
    } else {
        result["apps"] = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }.map {
            ["pid": $0.processIdentifier, "name": $0.localizedName ?? "", "bundle_id": $0.bundleIdentifier ?? ""] as [String: Any]
        }
    }
    return result
}

// MARK: - MCP server

let emptySchema: [String: Any] = ["type": "object", "properties": [:], "additionalProperties": false]
let pidProperty: [String: Any] = ["type": "integer", "minimum": 1]
let indexProperty: [String: Any] = ["type": "integer", "minimum": 0]
let minutesProperty: [String: Any] = ["type": "number", "minimum": 1, "maximum": maximumLeaseMinutes]

let tools: [[String: Any]] = [
    ["name": "agent_screen_prepare",
     "description": "Read the physical-screen and browser routes without changing windows or the pointer. For browser work, use the supervised agent-browser daemon and managed headless sessions; the local viewer is opened only when the user asks to watch. On multiple physical screens, claim the app you will drive before it opens task windows (agent_screen_claim), or park a window the user already had open (agent_screen_park); release or restore afterwards. On one physical screen, use the shared Skill's SpaceO virtual-session workflow (agent-spaceo) instead of operating the user's desktop directly.",
     "inputSchema": emptySchema],
    ["name": "agent_screen_status",
     "description": "Read physical screens, engine_running, claimed apps, parked windows and app PIDs; with pid, list that app's indexed windows. Also settles expired leases. When several physical screens are attached, the agent screen is always the built-in MacBook panel.",
     "inputSchema": ["type": "object", "properties": ["pid": pidProperty], "additionalProperties": false]],
    ["name": "agent_screen_claim",
     "description": "Preferred on multiple physical screens. Take a code-level hold on the one application this task will drive. While claimed, every window that app creates is moved to the agent screen on the window-created event and periodic rechecks. Windows that already existed belong to the user and are never touched. Claiming again only extends the lease. Requires engine_running: start supervised agent-screen watch or connect MCP before creating windows. Never claim a controller's normal main UI.",
     "inputSchema": ["type": "object", "properties": ["pid": pidProperty, "timeout_minutes": minutesProperty],
                     "required": ["pid"], "additionalProperties": false]],
    ["name": "agent_screen_release",
     "description": "End a claim and close the windows the agent opened during it, so the main screen returns to what the user had. Call it as soon as the GUI task finishes; claims also expire on their own lease.",
     "inputSchema": ["type": "object", "properties": ["pid": pidProperty, "all": ["type": "boolean"]],
                     "additionalProperties": false]],
    ["name": "agent_screen_park",
     "description": "Borrow one window the user already had open: move it to the agent screen (the built-in MacBook panel) and remember its original position for agent_screen_restore. Use it only for pre-existing windows; for windows the task opens, claim the app instead. Never sweep an app's windows or park a controller's normal main UI.",
     "inputSchema": ["type": "object", "properties": ["pid": pidProperty, "window_index": indexProperty,
                                                       "timeout_minutes": minutesProperty],
                     "required": ["pid"], "additionalProperties": false]],
    ["name": "agent_screen_restore",
     "description": "Hand a parked window back to where the user had it. Call with pid (optionally window_index), or all=true to clear every parked window. Expired leases restore themselves.",
     "inputSchema": ["type": "object", "properties": ["pid": pidProperty, "window_index": indexProperty,
                                                       "all": ["type": "boolean"]],
                     "additionalProperties": false]],
    ["name": "agent_screen_move",
     "description": "Alias of agent_screen_park kept for older clients: move one specified window to the agent screen and remember its original position for restore. Never sweep an app's windows or move a controller's normal main UI.",
     "inputSchema": ["type": "object", "properties": ["pid": pidProperty, "window_index": indexProperty],
                     "required": ["pid", "window_index"], "additionalProperties": false]],
    ["name": "agent_screen_cursor",
     "description": "Read or move the independent visible agent cursor on the built-in second screen, or post a click directly to a task application's process. Click requires the task pid and is rejected if the top window at the point belongs to another application. Coordinates outside that screen are rejected. This never warps, hides, or borrows the user's real pointer, and never falls back to global HID mouse input. A successful click result means events were posted; verify the UI effect afterwards.",
     "inputSchema": ["type": "object", "properties": [
                        "action": ["type": "string", "enum": ["status", "move", "click"]],
                        "x": ["type": "number"], "y": ["type": "number"], "pid": pidProperty,
                        "double": ["type": "boolean"], "shift": ["type": "boolean"]],
                     "required": ["action"], "additionalProperties": false]]
]

let allowedArguments: [String: Set<String>] = [
    "agent_screen_prepare": [],
    "agent_screen_status": ["pid"],
    "agent_screen_claim": ["pid", "timeout_minutes"],
    "agent_screen_release": ["pid", "all"],
    "agent_screen_park": ["pid", "window_index", "timeout_minutes"],
    "agent_screen_restore": ["pid", "window_index", "all"],
    "agent_screen_move": ["pid", "window_index"],
    "agent_screen_cursor": ["action", "x", "y", "pid", "double", "shift"]
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

func optionalInteger(_ arguments: [String: Any], _ name: String, minimum: Int) throws -> Int? {
    guard arguments[name] != nil else { return nil }
    return try integerArgument(arguments, name, minimum: minimum)
}

func booleanArgument(_ arguments: [String: Any], _ name: String) throws -> Bool {
    guard let raw = arguments[name] else { return false }
    guard let number = raw as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
        throw ScreenError("需要布尔参数：\(name)。")
    }
    return number.boolValue
}

func minutesArgument(_ arguments: [String: Any]) throws -> Double {
    guard let raw = arguments["timeout_minutes"] else { return defaultLeaseMinutes }
    guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, number.doubleValue >= 1, number.doubleValue <= maximumLeaseMinutes else {
        throw ScreenError("需要有效数值参数：timeout_minutes（1 到 \(Int(maximumLeaseMinutes))）。")
    }
    return number.doubleValue
}

func callTool(_ name: String, _ arguments: [String: Any]) throws -> [String: Any] {
    guard let allowed = allowedArguments[name] else { throw ScreenError("未知工具。") }
    guard Set(arguments.keys).isSubset(of: allowed) else { throw ScreenError("包含未支持的工具参数。") }
    switch name {
    case "agent_screen_prepare":
        return try prepare()
    case "agent_screen_status":
        return try status(pid: try optionalInteger(arguments, "pid", minimum: 1).map { pid_t($0) })
    case "agent_screen_claim":
        let pid = try integerArgument(arguments, "pid", minimum: 1)
        return try claim(pid: pid_t(pid), minutes: try minutesArgument(arguments))
    case "agent_screen_release":
        let pid = try optionalInteger(arguments, "pid", minimum: 1)
        let all = try booleanArgument(arguments, "all")
        guard pid != nil || all else { throw ScreenError("release 需要 pid 或 all=true。") }
        return try release(pid: pid.map { pid_t($0) }, all: all)
    case "agent_screen_park", "agent_screen_move":
        let pid = try integerArgument(arguments, "pid", minimum: 1)
        // The legacy alias has always required an explicit window, as its schema says.
        let index = try name == "agent_screen_move"
            ? integerArgument(arguments, "window_index", minimum: 0)
            : optionalInteger(arguments, "window_index", minimum: 0) ?? 0
        return try park(pid: pid_t(pid), index: index, minutes: try minutesArgument(arguments))
    case "agent_screen_restore":
        let pid = try optionalInteger(arguments, "pid", minimum: 1)
        let index = try optionalInteger(arguments, "window_index", minimum: 0)
        let all = try booleanArgument(arguments, "all")
        guard pid != nil || all else { throw ScreenError("restore 需要 pid 或 all=true。") }
        return try restore(pid: pid.map { pid_t($0) }, index: index, all: all)
    case "agent_screen_cursor":
        _ = try validateCursorRequest(arguments)
        if let cursor = hostingService?.cursor { return try cursor.handle(arguments) }
        return try cursorRequest(arguments)
    default:
        throw ScreenError("未知工具。")
    }
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
                              "capabilities": ["tools": [:]], "serverInfo": ["name": "agent-screen", "version": version]]
    case "ping":
        response["result"] = [:] as [String: Any]
    case "tools/list":
        response["result"] = ["tools": tools]
    case "tools/call":
        do {
            guard params["arguments"] == nil || params["arguments"] is [String: Any] else {
                throw ScreenError("工具参数必须为对象。")
            }
            let result = try callTool(params["name"] as? String ?? "", params["arguments"] as? [String: Any] ?? [:])
            response["result"] = ["content": [["type": "text", "text": json(result)]]]
        } catch {
            response["result"] = ["isError": true, "content": [["type": "text", "text": String(describing: error)]]]
        }
    default:
        response["error"] = ["code": -32601, "message": "Method not found"]
    }
    emit(response)
}

// Reads newline-delimited JSON-RPC from stdin on its own thread and executes every request on the
// main thread, which also runs the engine: requests and engine ticks never interleave.
func readRequests() {
    var line = Data()
    var dropping = false
    var buffer = [UInt8](repeating: 0, count: 4096)
    let maximumBytes = 65_536
    let oversized: [String: Any] = ["jsonrpc": "2.0", "id": NSNull(),
                                    "error": ["code": -32700, "message": "Request exceeds 64 KiB"]]
    func finish(_ frame: Data, dropped: Bool) {
        DispatchQueue.main.sync { dropped ? emit(oversized) : processRequest(frame) }
    }
    while true {
        let count = buffer.withUnsafeMutableBytes { read(STDIN_FILENO, $0.baseAddress, $0.count) }
        if count < 0 && errno == EINTR { continue }
        guard count > 0 else { break }
        for byte in buffer.prefix(count) {
            if byte == 10 {
                finish(line, dropped: dropping)
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
    if dropping || !line.isEmpty { finish(line, dropped: dropping) }
    // The client closed the pipe: give up the engine so the next MCP process can host it.
    DispatchQueue.main.sync {
        clearEngineBeat()
        exit(0)
    }
}

var hostingService: Service?

func serve() -> Never {
    // AGENT_SCREEN_ENGINE=off serves tools only (used by the test suite, which must never move a
    // real window). Otherwise the first MCP process with accessibility hosts the engine; the
    // others serve tools and keep trying, so the engine survives when that first session ends.
    if ProcessInfo.processInfo.environment["AGENT_SCREEN_ENGINE"] != "off" {
        var reportedNoAccess = false
        func host() -> Bool {
            guard hostingService == nil else { return true }
            // Without accessibility, log the refusal once instead of every retry.
            if !AXIsProcessTrusted() {
                if reportedNoAccess { return false }
                reportedNoAccess = true
            }
            guard let service = try? Service(), service.start() else { return false }
            hostingService = service
            return true
        }
        if !host() {
            Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { timer in
                if host() { timer.invalidate() }
            }
        }
    }
    // Keep the main run loop alive even without the engine (a loop with no sources returns at
    // once and would spin): tool calls are dispatched onto it.
    RunLoop.main.add(NSMachPort(), forMode: .default)
    let reader = Thread { readRequests() }
    reader.name = "agent-screen-mcp-stdin"
    reader.stackSize = 1 << 20
    reader.start()
    while true { _ = RunLoop.main.run(mode: .default, before: .distantFuture) }
}

// MARK: - CLI

let usage = """
用法：
  agent-screen prepare                      只读路由
  agent-screen status [PID]                 路由、引擎、接管与停放台账（会结算过期租约）
  agent-screen claim PID [MINUTES]          接管应用：新窗口只在副屏出现
  agent-screen release PID | --all          结束接管，关闭接管期间新开的窗口
  agent-screen sweep PID                    手动补扫一次已接管应用的窗口
  agent-screen park PID [INDEX] [MINUTES]   借用用户已有窗口到副屏
  agent-screen restore PID [INDEX] | --all  把停放窗口还回原位
  agent-screen watch                        前台运行引擎和独立光标（需辅助功能授权）
  agent-screen mcp                          MCP 服务（控制端调用）
  agent-screen cursor status                查询独立光标
  agent-screen cursor move X Y              移动独立光标，不碰真实指针
  agent-screen cursor click X Y --pid PID [--double] [--shift]
                                            向指定任务进程定向投递点击
"""

func parsePID(_ text: String) throws -> pid_t {
    guard let value = Int32(text), value > 0 else { throw ScreenError("PID 无效：\(text)") }
    return value
}

func parseIndex(_ text: String) throws -> Int {
    guard let value = Int32(text), value >= 0 else { throw ScreenError("窗口序号无效：\(text)") }
    return Int(value)
}

func parseMinutes(_ text: String) throws -> Double {
    guard let value = Double(text), value.isFinite, value >= 1, value <= maximumLeaseMinutes else {
        throw ScreenError("分钟数无效（1 到 \(Int(maximumLeaseMinutes))）：\(text)")
    }
    return value
}

func parseCoordinate(_ text: String) throws -> Double {
    guard let value = Double(text), value.isFinite, abs(value) <= 1_000_000 else {
        throw ScreenError("坐标无效：\(text)")
    }
    return value
}

func runCLI(_ args: [String]) throws {
    let command = args.first ?? "status"
    let rest = Array(args.dropFirst())
    func expect(_ range: ClosedRange<Int>) throws {
        guard range.contains(rest.count) else { throw ScreenError(usage) }
    }
    switch command {
    case "mcp":
        try expect(0...0)
        serve()
    case "prepare":
        try expect(0...0)
        print(json(try prepare()))
    case "status":
        try expect(0...1)
        let pid = try rest.first.map(parsePID)
        print(json(try status(pid: pid)))
    case "claim":
        try expect(1...2)
        let pid = try parsePID(rest[0])
        let minutes = try rest.count == 2 ? parseMinutes(rest[1]) : defaultLeaseMinutes
        print(json(try claim(pid: pid, minutes: minutes)))
    case "release":
        try expect(1...1)
        if rest[0] == "--all" { print(json(try release(pid: nil, all: true))) }
        else { print(json(try release(pid: try parsePID(rest[0]), all: false))) }
    case "sweep":
        try expect(1...1)
        let pid = try parsePID(rest[0])
        print(json(["pid": Int(pid), "moved": sweepClaim(pid: pid), "accessibility": AXIsProcessTrusted()]))
    case "park", "move":
        try expect(1...3)
        let pid = try parsePID(rest[0])
        let index = try rest.count >= 2 ? parseIndex(rest[1]) : 0
        let minutes = try rest.count == 3 ? parseMinutes(rest[2]) : defaultLeaseMinutes
        print(json(try park(pid: pid, index: index, minutes: minutes)))
    case "restore":
        try expect(1...2)
        if rest[0] == "--all" {
            try expect(1...1)
            print(json(try restore(pid: nil, index: nil, all: true)))
        } else {
            let pid = try parsePID(rest[0])
            let index: Int? = try rest.count == 2 ? parseIndex(rest[1]) : nil
            print(json(try restore(pid: pid, index: index, all: false)))
        }
    case "cursor":
        try expect(1...7)
        let action = rest[0]
        var request: [String: Any] = ["action": action]
        switch action {
        case "status":
            try expect(1...1)
        case "move", "click":
            try expect(action == "move" ? 3...3 : 5...7)
            request["x"] = try parseCoordinate(rest[1])
            request["y"] = try parseCoordinate(rest[2])
            if action == "click" {
                var flags = Set<String>()
                var index = 3
                while index < rest.count {
                    let flag = rest[index]
                    switch flag {
                    case "--double", "--shift":
                        guard flags.insert(flag).inserted else { throw ScreenError(usage) }
                        request[flag == "--double" ? "double" : "shift"] = true
                        index += 1
                    case "--pid":
                        guard request["pid"] == nil, index + 1 < rest.count else { throw ScreenError(usage) }
                        request["pid"] = try parsePID(rest[index + 1])
                        index += 2
                    default:
                        throw ScreenError(usage)
                    }
                }
            }
        default:
            throw ScreenError(usage)
        }
        print(json(try cursorRequest(request)))
    case "watch":
        try expect(0...0)
        try Service().run()
    default:
        throw ScreenError(usage)
    }
}

do {
    try runCLI(Array(CommandLine.arguments.dropFirst()))
} catch {
    FileHandle.standardError.write(Data((String(describing: error) + "\n").utf8))
    exit(1)
}
