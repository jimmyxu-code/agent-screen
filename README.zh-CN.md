# agent-screen

让 GUI 智能体的任务窗口和独立可见光标留在本机内置第二物理屏；只有一块物理屏时，改用 SpaceO 共享虚拟屏、独立任务区域和会话光标。用户主屏、控制端正常主界面和真实指针不被占用。

多物理屏时按内置硬件选择智能体屏幕，不依赖屏序号或 macOS 当前把哪块屏叫主屏。单物理屏通过共享 Skill 创建 SpaceO 虚拟会话，不直接操作真实桌面。

## 一次安装

需要 macOS 14 或更新版本、Swift 命令行工具、make、Python 3 和 PyObjC。输入层使用 Quartz、ApplicationServices 和 CoreText：

```sh
python3 -m pip install --user pyobjc-framework-Quartz pyobjc-framework-ApplicationServices pyobjc-framework-CoreText
```

然后执行：

```sh
git clone https://github.com/jimmyxu-code/agent-screen.git
cd agent-screen
make install
```

一次安装并重签名：

- `~/.local/bin/agent-screen`
- `~/.local/bin/agent-input`
- `~/.local/bin/agent-spaceo`
- `~/.local/bin/agent-browser` 和本地观察页资源 `~/.local/share/agent-browser/viewer.html`
- `~/.agents/skills/agent-screen/SKILL.md` 及其脚本

`PREFIX` 可改二进制目录，`SKILL_HOME` 可改 Skill 目录。安装只检查 Python 依赖是否齐全，不会静默安装或升级其他包。

## 一个 Skill，不逐智能体适配

仓库只有一份通用 `skills/agent-screen/SKILL.md`。支持共享 `~/.agents/skills` 目录的智能体可以直接发现它，其他智能体也可通过自身标准 Skill 机制读取同一文件。项目不包含各家客户端的专用行为或配置清单。

Skill 只提供规则，不等于 macOS 授权或常驻后台。所有智能体共享一个后台即可：

```sh
~/.local/bin/agent-screen watch
```

必须用控制端的受管、持续后台任务运行它，并保持宿主存活。不要用 launchd、`nohup` 或 `(cmd &)` 脱离控制端，否则辅助功能授权可能不再跟随进程。其他智能体复用同一引擎，不重复启动。

单物理屏改用受管 `agent-spaceo daemon` 作为 SpaceO 后台，管理虚拟会话及其光标；该路径不使用物理屏的 `watch` 或 `agent-screen cursor click`。两种后台都支持受管进程和标准 MCP，不做逐客户端配置。

不支持受管后台的宿主可以使用标准 MCP 接口；受管 `watch` 可用时，不需要任何逐客户端 MCP 注册。两类能力都没有的宿主，不能声称具备窗口创建时接管能力。

### 可选 MCP 配置

```json
{
  "mcpServers": {
    "agent_screen": {
      "command": "/absolute/home/.local/bin/agent-screen",
      "args": ["mcp"]
    }
  }
}
```

第一个具备辅助功能授权的 `watch` 或 MCP 进程取得 `watch.lock`，托管引擎和光标。其他 MCP 连接也可通过同一本机通道使用光标。

## 浏览器：无窗口隔离与按需观看

浏览器任务在单双屏下都使用 `agent-browser`。它启动独立的无窗口 Chromium，不创建桌面 Chrome 窗口。每个任务都有独立 browser context、页面、视口和 lease；不接管用户浏览器进程、profile、cookie 或标签页。需要安装 Google Chrome，或用 `AGENT_BROWSER_CHROME` 指定受管 Chromium 可执行文件。

```sh
agent-browser doctor
agent-browser daemon                       # 受管持续后台任务
agent-browser create --session unique-task --url https://example.com
agent-browser snapshot --session unique-task
agent-browser click --session unique-task --selector 'a'
agent-browser type --session unique-task --selector '#field' --text '你好' --replace
agent-browser scroll --session unique-task --dy 400
agent-browser tabs --session unique-task
agent-browser screenshot --session unique-task -o browser.png
agent-browser heartbeat --session unique-task
agent-browser destroy --session unique-task
```

所有浏览器输入都通过 DevTools，不使用全局鼠标键盘、真实指针或共享剪贴板快捷键。截图包含该会话独立可见的蓝色光标。`prepare` 为此返回 `browser_route=managed_headless` 和 `browser_command=agent-browser`。SpaceO 健康检查拒绝时也不会退回真实桌面。

用户明确要求观看时才执行 `agent-browser show --session unique-task`。本地观察页仅监听回环地址，通过一次性 ticket 换取 HttpOnly、SameSite=Strict 的本机 cookie，默认只读。只有用户可以勾选控制开关。隔离 profile 默认未登录，需要登录时由用户在观察页中自行完成，不共享现有浏览器会话，也不向智能体提供密码。

宿主的内嵌浏览器只有在后台操作时不自动显示或激活面板才可用；用户主动要求观看时才显示，否则统一使用 `agent-browser`。会话 lease 和观察页凭据不输出到命令或日志。

## 使用

1. 先运行 `agent-screen prepare`，它是只读的。
2. `single_physical_screen`：要求 `target_kind=spaceo_virtual`，使用下面的 SpaceO 流程，不操作真实桌面。
3. `second_physical_screen`：确认 `target_built_in=true` 和 `engine_running=true`。
4. 任务将创建窗口的应用，先 `agent-screen claim PID`。引擎在窗口创建事件中把新窗口移到内置屏，并每 0.2 秒复查。任何桌面上的已有窗口属于用户，身份不明的窗口也受保护。结束用 `agent-screen release PID`，只关闭本次接管创建的窗口。
5. 要借用已有用户窗口，依次 `agent-screen status PID`、`agent-screen park PID INDEX`、用完 `agent-screen restore PID INDEX`。这会有一次可见的位置切换，所以新任务窗口优先 claim。

只接管或借用当前任务目标。控制端正常主界面和其他用户窗口不动。

```sh
agent-screen prepare
agent-screen status [PID]
agent-screen claim PID [MINUTES]        agent-screen release PID | --all
agent-screen park PID [INDEX] [MINUTES] agent-screen restore PID [INDEX] | --all
agent-screen sweep PID                  agent-screen watch
```

`agent_screen_move` 保留为 `agent_screen_park` 的兼容别名。

## 单物理屏：SpaceO

安装配套的 SpaceO 构建（基于 1.0.9，包含会话光标与自动镜像处理；本集成对应源码树为 `SpaceO-agent-screen`），仅安装上游 1.0.9 不包含这些改动。用 `agent-spaceo doctor` 核对主机可用性、CLI / daemon 构建一致性和 `session-cursors` 能力，报告为 JSON。`create` 在查询显示池或创建显示前也会执行这项检查，拒绝旧后台和缺少配套能力的构建。有界显示身份避免旧版本随机身份造成的 ColorSync 累积。会话和光标都由 SpaceO daemon 管理，不交给 agent-screen。

```sh
agent-spaceo doctor
agent-spaceo daemon                       # 受管持续后台任务
agent-spaceo create --session unique-task
agent-spaceo run --session unique-task --app Application
agent-spaceo command --session unique-task ax
agent-spaceo command --session unique-task click --element 3
agent-spaceo command --session unique-task screenshot -o session.png
agent-spaceo command --session unique-task verify
agent-spaceo heartbeat --session unique-task
agent-spaceo destroy --session unique-task
```

受管后台把同一块虚拟屏分成四个任务区域，`create` 默认使用 `shared`，不再为每个智能体新增显示。所有使用该封装的智能体通过本机私有锁串行完成占用检查和会话创建；区域已满会明确拒绝，不静默新增显示。创建前会重新读取物理屏路由：本机内置第二物理屏已可用时，包括 exclusive 在内的所有虚拟屏创建都会拒绝，改用上面的多屏流程；路由无法确认也不会创建。确有独立显示需求的高级用法可在单物理屏时显式选择 exclusive。

可以用 `agent-spaceo command --session unique-task screenshot -o session.png` 查看虚拟屏截图，或用 `open -g -a "SpaceO Viewer" --args --background` 打开实时 Viewer。Viewer 是观察虚拟屏的窗口；有外接屏时它按多屏规则放在本机屏。需要坐标操作时先用 `agent-screen claim PID` 接管新启动的 Viewer，不要搬动控制端主界面。

每个会话和 tile 都有独立可见的蓝色箭头，由 SpaceO daemon 持有并包含在会话截图中。`agent-spaceo` 只在权限受限的本地状态中保存 lease，不输出它。新任务应用使用 `run`；只有用户明确要求借用已有应用时才 `adopt --pid`，结束用 `destroy --keep-apps` 释放，不终止应用。控制端主界面不能 adopt。

使用最新元素索引并核对真实 UI / 文档效果。`partial` 不是完整隔离通过，`breached` 必须停止。任务应用结束用 `destroy`，借用应用用 `--keep-apps`，再检查 `agent-spaceo status` 和 `agent-spaceo pool`，不影响其他会话。正式工作不携带 `SPACEO_LIVE_TESTS` 或 `SPACEO_TESTING_HOST`，它们只用于诊断测试。

## 多物理屏：独立可见光标

`watch` 在内置屏显示蓝色箭头。它不激活应用、不接收鼠标点击，是引擎持有的可见面板，不是 WindowServer 的单一硬件光标；每条 CLI 命令退出后它仍然存在。

```sh
agent-screen cursor status
agent-screen cursor move X Y
agent-screen cursor click X Y --pid PID [--double] [--shift]
agent-input click X Y --pid PID [--double] [--shift]
```

坐标操作前必须确认 `visible=true` 和 `target_built_in=true`。坐标在内置屏之外，或该点顶层窗口属于其他 PID 时，点击直接拒绝。事件携带目标窗口编号和窗口局部坐标，直接发往目标进程；不搬动、隐藏、借用或恢复真实指针，也不回退到全局 HID 鼠标事件。

`posted` 只表示事件已投递，不表示应用执行了动作。点击后必须读取真实 UI 或文档状态核对。原生 AppKit 按钮和自绘 AppKit 视图已做真实验收，其他 UI 框架仍需单独验证。

进程定向点击使用已导出但未写入公开文档的 CoreGraphics 窗口字段，以及未出现在公开头文件中的窗口位置设置函数。这些接口具有系统兼容性风险，macOS 升级后应重新验收；它们不依赖私有驱动，也不经过全局事件流。

优先使用 AX 菜单 / 控件和定向键盘。需要置前时先激活并确认，再重新读取当前坐标；两个点击命令都不会替你激活应用或修正过期坐标。全局键盘是单独确认的操作，不作为鼠标回退。

## 后台状态与边界

只有具备授权的引擎运行时，claim 才会被强制执行。`agent_screen_status` 读取心跳；引擎未运行时 `claim` 返回警告，不假装成功。租约默认 30 分钟，最长 1440 分钟；到期接管关闭智能体窗口，到期停放自动还原。没有引擎时，读取 `status` 也会结算过期停放。

台账位于 `~/.local/state/agent-screen/`，权限 0600，读改写加锁。日志位于 `~/Library/Logs/AgentScreen/engine.log`。

只有活动物理屏参与路由，已知虚拟屏会被排除。镜像模式会被拒绝；最小化窗口需先恢复；全屏窗口停放时退出全屏，还原到原屏后再进入全屏。工具不调整显示器排列，也不修改隐私权限。

## 验证

```sh
make test
git diff --check
```

测试设置 `AGENT_SCREEN_ENGINE=off`，不托管引擎、不显示光标、不移动真实窗口，也不合成输入。覆盖 MCP 协商、参数校验、通知、输入长度、路由说明、共享 Skill 安装结构、禁止真实指针鼠标操作，以及使用伪 CLI 的 SpaceO 生命周期。真实窗口和光标行为仍需在扩展物理屏或 SpaceO 虚拟屏与辅助功能授权环境中验收。

本项目采用 [MIT 许可证](LICENSE)。
