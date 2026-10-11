# agent-screen

只处理智能体工作窗口的位置：多块物理屏时放到第二块物理屏，单屏时返回 `spaceo`
模式，由智能体使用单独配置的 [SpaceO MCP](https://github.com/ParthJadhav/SpaceO) 会话。

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
3. 单屏时读取 SpaceO pool / doctor；健康允许后创建会话，核对窗口和隔离回执。
   健康 blocked/unknown 时停止图形操作，保留真实的 partial 验证结果。

主屏排第一，其余物理屏按显示器 ID 排序，目标是第二个条目。SpaceO 虚拟屏会被排除。
需要扩展显示模式；全屏或最小化窗口应先恢复。窗口移动后会读取实际边界确认结果。
鼠标跨屏方向由 macOS 的显示器排列决定；外接屏在左、本机屏在右时，可以从外接屏右边缘进入本机屏。

## 验证

`make test` 和 CI 覆盖协议、参数、通知、输入长度与错误恢复，不移动真实窗口。
实际多屏移动需在连接外接屏并授予辅助功能权限的本机验收。单屏已完成 SpaceO 的
原生输入、读回和截图验证；键盘/输入法路由在系统无法提供证据时仍属于部分验证。

本项目采用 [MIT 许可证](LICENSE)，SpaceO 独立安装和管理。
