# agent-screen

只处理智能体工作窗口的位置：多块物理屏时放到第二块物理屏；单屏时返回
`single_physical_screen`，直接用电脑操作工具在该屏操作，不依赖 SpaceO。

## 安装

需要 macOS 14 或更新版本、Swift 命令行工具、make 和 Python 3。当前已在 Apple Silicon
上使用；其他硬件组合还需要验证实际窗口放置。

```sh
git clone https://github.com/jimmyxu-code/agent-screen.git
cd agent-screen
make install
```

默认安装到 `~/.local/bin/agent-screen`。在系统设置 → 隐私与安全性 → 辅助功能中，
为启动此工具的客户端授权。MCP 配置示例见 [英文说明](README.md#mcp-configuration)。

## 使用

1. 打开工作窗口前先调用 `agent_screen_prepare`。
2. 多屏时打开应用，用 `agent_screen_status(pid)` 读取窗口序号，然后调用
   `agent_screen_move(pid, window_index)`。后续使用电脑操作工具控制该窗口，新窗口同样放置。
3. 单屏时直接在当前物理屏操作任务窗口，无需移动窗口、SpaceO 会话、额外隔离或健康检查。

只移动当前任务新打开或用户明确指定的具体工作窗口。WorkBuddy、Codex、DeepSeek Harness
等控制端的正常主界面留在用户主屏，不能批量搬动整个应用或用户已有的其他窗口。

主屏排第一，其余物理屏按显示器 ID 排序；多屏目标是第二个条目，单屏目标是唯一的物理屏。SpaceO 虚拟屏会被排除。
需要扩展显示模式；全屏或最小化窗口应先恢复。窗口移动后会读取实际边界确认结果。
鼠标跨屏方向由 macOS 的显示器排列决定；外接屏在左、本机屏在右时，可以从外接屏右边缘进入本机屏。

## 验证

`make test` 和 CI 覆盖协议、参数、通知、输入长度、错误恢复、工具路由说明和物理屏路由的
只读检查，不移动真实窗口或输入。实际窗口操作需通过电脑操作工具在本机验收；多屏移动
还需要连接扩展显示屏并授予辅助功能权限。

本项目采用 [MIT 许可证](LICENSE)，单屏工作无需安装 SpaceO。
