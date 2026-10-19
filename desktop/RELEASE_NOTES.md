School Watcher 2.27：支持自定义 OpenAI 兼容 API 服务。

- 在“AI 服务”中新增“自定义（OpenAI 兼容）”服务商，可填写显示名称、接口地址、模型名称和自己的 API 密钥。
- 接口地址既可以是基础地址（例如 `https://example.com/v1`），也可以是完整的 `/chat/completions` 地址；程序会自动补全并校验端口。
- 自定义密钥继续使用本机加密保存，页面不会回显完整密钥；修改接口地址后会停用该服务并要求重新测试，避免把密钥发送到未经确认的地址。
- 连接测试只发送一次最小结构化输出请求；即使第三方网关不返回 usage，也可以通过测试，并在界面明确提示实际调用不会计入 Token 统计。
- 服务商列表、编辑表单、英文文案、数据库迁移和本地 HTTP 模拟测试同步更新；验证过程不调用真实付费接口。

English: Adds a fifth “Custom (OpenAI-compatible)” provider with your own display name, endpoint, model and API key. The endpoint accepts either a base URL or a full chat-completions URL, validates the port, and requires a fresh test after the address changes. Keys remain encrypted at rest and are never returned in full to the page. A gateway that omits usage can still pass the one-request connection test, with an explicit notice that actual calls will not count toward token statistics.

School Watcher 2.26：合并图谱交互、实时跟随、桌面全屏与语言修复。

- 合并另一会话的完整图谱改动，并保留 2.25 的更新窗口英文和 API 历史用量修复。
- “跟随探索”记住用户选择，根据真实任务的最新进展定位入口；被筛选或收起的目标可一键显示。手动浏览与后台探索分别运行。
- 手动选择与实时跟随的路径分别高亮；根据已保存的目录归属和来源证据显示可确认的关系，保留原始发现路径，不猜测归属。
- 增加分支间距设置、成簇布局、中键平移和独立的动画控制。沉浸视图中的搜索、详情及设置可按需展开。
- 支持图谱全屏；Windows 桌面窗口可覆盖整个屏幕，退出后恢复之前的普通或最大化状态。
- 补充密集图谱、持续生长、目标定位、全屏恢复和中英文检查。验证使用隔离数据，不调用付费 AI。

English: Combines the discovery graph improvements with the 2.25 localization fixes. Live tracking remembers your choice and locates real task progress, while manual selections use a separate highlighted path. Recorded directory relationships are shown with their evidence and original navigation paths. Adds clustered layouts, branch spacing, middle-button panning, optional immersive-view panels, and Windows fullscreen that restores the previous window state. Existing summary usage remains historical accounting; the reader's summary-generation entry is not restored.
