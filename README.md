# ENSP-MCP

让 AI 通过 MCP 协议管理华为 eNSP 模拟网络设备，并提供拓扑可视化工具。

## 功能概览

- **MCP Server** — 通过自然语言操作 eNSP 设备（CLI 执行、配置下发、配置保存、运行配置查看、网络诊断、健康检查、拓扑同步）
- **Topo Viewer** — 浏览器拓扑可视化，支持上传 `.topo` / `.xml` / `.json` 文件，自动生成 `topology.json` 并同步当前拓扑给 AI
- **Current Topology Context** — AI 可通过 `get_topology_context` 或 `ensp://topology/current` 直接读取当前网页上传拓扑，并优先把它作为当前会话的拓扑事实来源
- **Prompt Layer** — 内置 `ensp_ops_playbook` 与 `ensp_senior_network_engineer` 两个 MCP Prompts，分别提供 topology-first 规则与资深网络工程师角色约束；若客户端不支持 MCP Prompts，可直接使用根目录 `prompts.md` 作为自定义提示词

## 前置条件

- Windows 10以上系统（eNSP 仅支持 Windows）
- [华为 eNSP](https://support.huawei.com/enterprise/zh/tool/ensp-TL1000000015) 已安装并启动
- Python 3.11–3.13（`.python-version` 默认选择 3.11.6）
- [uv](https://docs.astral.sh/uv/getting-started/installation/)，用于创建虚拟环境和同步依赖
- 一个支持 MCP 的 AI 客户端（Claude Desktop / Claude Code / Cursor 等）

## 安装

当前运行依赖锁定为 FastMCP 4.0.11、MCP SDK 2.3.0 和 Pydantic 2.13.5。Python 要求为 3.11–3.13，默认版本为 3.11.6；开发依赖组记录了 uv 0.12.23。

依赖声明在 `pyproject.toml`，完整版本和下载校验信息记录在 `uv.lock`。`requirements.txt` 提供相同的直接运行依赖，推荐使用锁文件同步环境。

### 创建环境并启动

下载或克隆仓库后，在包含 `pyproject.toml` 的项目根目录打开 PowerShell，执行：

```powershell
uv sync --locked
uv run --locked python -m ensp_mcp_server.server
```

`uv sync --locked` 根据 `.python-version`、`pyproject.toml` 和 `uv.lock` 创建或同步项目的 `.venv`。`uv run` 使用该虚拟环境运行服务，无需手动激活环境或全局安装 Python 依赖。虚拟环境不随源码发布。

### 协议兼容性

服务通过 stdio 支持新协议 `2026-07-28`，以及旧版握手协议 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`。客户端与服务端协商共同支持的协议版本。

本地回归验证已覆盖 `2026-07-28` 与 `2025-11-25` 的 stdio 调用；FastMCP 3.4.0 / MCP SDK 1.30.0 客户端连接当前服务也已验证。测试脚本不随此仓库发布。其他列出的旧协议属于底层 SDK 的支持范围，不等于每个客户端都已逐一验证。

这里的兼容性指通信协议。服务源码当前按 FastMCP 4.0.11 / MCP SDK 2.3.0 运行，未实现降级到 FastMCP 3.x / MCP SDK 1.x 的运行环境兼容。服务提供 20 个工具、2 个资源和 2 个提示词。

### 依赖检查

在项目根目录完成依赖同步后，可以执行：

```powershell
uv pip check --python .venv/Scripts/python.exe
```

该命令检查已安装依赖是否兼容，不验证设备功能。

## 配置 MCP 客户端

先在项目根目录执行 `uv sync --locked`，创建 `.venv`。下面的 `my/path` 是演示项目目录，`<项目根目录>` 也是占位符，使用时替换为自己的实际项目目录。JSON 中的 Windows 路径可使用正斜杠，或将反斜杠写为 `\\`。

### 通用 stdio 配置

直接使用 uv 创建的虚拟环境中的 Python 启动服务：

```json
{
  "ensp-mcp": {
    "type": "stdio",
    "command": "my/path/.venv/Scripts/python.exe",
    "args": [
      "-m",
      "ensp_mcp_server.server"
    ],
    "cwd": "my/path"
  }
}
```

`command` 指向项目 `.venv` 中的解释器，`cwd` 指定包含 `ensp_mcp_server` 的项目根目录。使用 `-m` 启动时，必须确保项目根目录在模块搜索路径中，仅指定解释器路径并不能保证找到项目模块。

若客户端不支持 `cwd`，请在其界面设置服务工作目录为项目根目录；也可使用下面 Claude Code 示例中的 `uv --directory` 启动方式，由 uv 指定工作目录。若客户端不接受 `type` 字段，按其格式省略该字段。

### Claude Code

使用命令行添加本地 stdio 服务：

```powershell
claude mcp add --transport stdio ensp-mcp -- uv --directory "<项目根目录>" run --locked python -m ensp_mcp_server.server
```

也可通过 Claude Code 支持的 MCP JSON 配置入口使用上面的通用配置。配置方式见 [Claude Code 官方文档](https://code.claude.com/docs/en/mcp)。

### Claude Desktop、Cursor、Trae 等客户端

在客户端的本地 MCP 配置中设置解释器、启动参数和项目工作目录。Claude Desktop 的配置文件为 `%APPDATA%\Claude\claude_desktop_config.json`；其他客户端按自己的配置界面或文件格式填写。

如果界面只接受单个服务器对象，复制通用示例中 `ensp-mcp` 对应的对象；如果要求 `mcpServers` 结构，将整个示例对象放在 `mcpServers` 下。不要额外添加尾随逗号，并按客户端支持情况设置工作目录。

配置完成后重新加载该 MCP 服务。连接后应能列出本项目的工具；服务启动后，可直接访问随服务启动的 Web UI。

## 使用方式

### 1. 启动 eNSP 并运行设备

在 eNSP 中打开拓扑，启动所有设备，等待设备完成初始化。

### 2. 注册设备

有三种方式：

**自动发现：** 让 AI 执行 `auto_discover`。它首先查找 `eNSP_Client.exe` 进程及其 2000–2999 范围的本地控制台监听端口；如果进程枚举或端口关联不可用，则回退到当前网页上传拓扑生成的 `topology.json`，逐个 TCP 探测其中的 `com_port`。发现结果会合并到设备注册表，并保留拓扑中的设备名称。没有控制台端口的终端设备仍不会被发现。

**手动注册：** 在 eNSP 中右键设备 > 设置 > 查看串口号，然后让 AI 执行：

```
register_device(name="R1", port=2000)
```

**网页上传拓扑（主要推荐）：** 在 Web UI 上传 `.topo` 后，页面会自动生成 `topology.json`，并自动把拓扑设备同步进 MCP 注册表。

### 3. 开始使用

在 MCP 客户端连接服务后，先通过 `get_topology_context` 读取当前上传拓扑，或读取资源 `ensp://topology/current`，再使用自然语言提出操作目标。

如果客户端支持 MCP Prompts，建议启用 `ensp_ops_playbook` 与 `ensp_senior_network_engineer`；如果不支持，可直接使用项目根目录下的 `prompts.md` 作为自定义角色提示词。

```
"帮我看看 R1 的路由表"
"给 R1 和 R2 配置 OSPF"
"检查所有设备的健康状态"
"基于当前上传的拓扑检查各设备配置是否匹配"
```

## Topo Viewer 拓扑可视化

浏览器拓扑查看器，上传 `.topo` 文件即可渲染交互式网络图，并自动同步拓扑数据给 AI。

### 启动

```powershell
uv run --locked python -m ensp_mcp_server.server
```

上述命令在项目根目录执行。MCP 客户端已经启动服务时，可直接打开浏览器，无需另开一个相同服务实例：

```text
http://127.0.0.1:18080/index.html
```

AI 会优先通过读取当前网页生成的 `topology.json`、`get_topology_context` 或 `ensp://topology/current` 获取拓扑信息，而不是优先搜索本地 `.topo` 文件。

### 功能

- 上传 `.topo` / `.xml` / `.json` 拓扑文件
- 自动生成并保存 `topology.json`
- 自动将拓扑中的设备同步进 MCP 注册表
- 终端输出面板展示连接、命令发送和回显内容
- MCP 调用记录展示高层工具调用与异常信息
- 支持拓扑重新同步与一键清空
- 支持深色 / 浅色主题切换
- 支持终端字体大小调整
- 支持日志区拖拽调整高度
- 支持桌面端中等宽度与缩放场景下的布局优化

网页“一键清空”会删除当前拓扑快照，清除全部设备注册项并关闭相应活动控制台连接，其中包括手动注册项。再次使用前需要重新上传或注册设备。

## MCP 工具列表

### 设备管理

| 工具 | 说明 |
|------|------|
| `register_device` | 注册 eNSP 设备（名称 + 端口） |
| `unregister_device` | 注销设备 |
| `list_devices` | 列出所有已注册设备 |
| `auto_discover` | 自动发现并注册 eNSP 设备 |
| `sync_topology_devices` | 将当前 `topology.json` 中的设备同步到 MCP 注册表 |

### CLI 与配置

| 工具 | 说明 |
|------|------|
| `execute_cli` | 执行单条查询/显示命令 |
| `mcp_execute` | 使用当前拓扑同步后的设备名直接执行命令 |
| `push_config` | 自动进入 system-view，逐条下发配置；遇错停止并报告未执行命令 |
| `mcp_push_config` | 使用当前拓扑同步后的设备名下发配置 |
| `multi_device_push_config` | 先校验全部批次，再按顺序下发；遇错停止后续设备 |
| `save_config` | 处理 Y/N 和默认文件名；收到保存成功回显和最终提示符才返回成功 |
| `get_running_config` | 查看运行配置（section 过滤需设备 VRP 支持；不支持时返回明确错误，可省略 section 获取完整配置） |

配置批次开始前会校验全部设备和命令。单设备及多设备批次遇到拒绝、超时或连接错误时停止后续配置，报告已确认、失败或结果未确认、未执行的部分，并尝试退出配置视图；已经生效的配置不会自动回滚。设备名与命令必须是非空单行字符串，配置列表必须非空。

注册、解析和注销设备名统一使用 `casefold` 匹配。再次注册同名设备时沿用原有名称并更新该条目，保留凭据；拓扑同步和自动发现也避免创建仅大小写不同的重复名称。

手动注册的控制台端口限 1–65535。拓扑中的非零控制台端口必须唯一；多个 PC/Client/Server 可使用端口 0，表示没有 CLI。无效拓扑上传或同步会在修改注册表前被拒绝；显式的 `devices: []` 表示合法空拓扑。上传拓扑快照、注册表和同步清单使用原子文件替换，写入失败会恢复原状态。

`save_config` 会处理 Y/N 确认和带默认值的 cfg/zip 文件名提示。只有保存成功回显与其后的控制台提示符均出现时才确认成功；超时、断开或无法识别的交互会报错。报错不能证明保存一定没有生效，应检查实际状态后再决定后续操作。

`multi_health_check` 不指定设备时会检查全部注册项。若当前拓扑有端口 0 的终端，建议显式提供路由器或交换机名称，避免将“没有 CLI”误判为网络故障。

### 网络诊断

| 工具 | 说明 |
|------|------|
| `ping_from_device` | 从设备发起 Ping |
| `traceroute_from_device` | 从设备发起 Traceroute |
| `health_check` | 单设备健康检查（TCP + CLI） |
| `mcp_health_check` | 使用当前拓扑同步后的设备名进行健康检查 |
| `multi_health_check` | 批量健康检查 |

### 拓扑发现

| 工具 | 说明 |
|------|------|
| `discover_topology` | 解析 eNSP `.topo` 文件，或直接读取上传后的 `topology.json` |
| `find_topo_files` | 搜索本地 `.topo` 文件；若网页拓扑存在，会优先提示当前 `topology.json` |
| `get_topology_context` | 获取当前网页上传拓扑的摘要上下文 |

## MCP 资源

| 资源 URI | 说明 |
|------|------|
| `ensp://topology/current` | 当前上传拓扑的上下文 |
| `ensp://devices/registered` | 当前设备注册列表 |

## MCP Prompts

| Prompt | 说明 |
|------|------|
| `ensp_ops_playbook` | 拓扑优先的操作提示词，优先读取当前网页上传拓扑，再进行设备操作 |
| `ensp_senior_network_engineer` | 资深网络工程师角色提示词，强调规划、配置、验证与故障排查 |

如果客户端不支持 MCP Prompts，可以复制根目录 [prompts.md](prompts.md) 中的提示词到客户端自定义指令。该文档不会因存在于仓库中而自动加载。

## 项目结构

```text
eNSP-MCP/
├── .python-version            # 默认 Python 3.11.6
├── pyproject.toml             # Python 版本约束与依赖声明
├── uv.lock                    # uv 依赖锁文件
├── requirements.txt           # 直接运行依赖
├── README.md                  # 安装与使用说明
├── index.html                 # 唯一前端入口
├── topo-viewer.css            # Web UI 样式
├── topo-viewer.js             # Web UI 逻辑
├── prompts.md                 # 客户端可直接粘贴的角色提示词
└── ensp_mcp_server/           # MCP Server 主包
    ├── __init__.py
    ├── server.py              # MCP Server 入口 & 工具注册
    ├── web_ui.py              # 本地 Web UI 服务
    ├── runtime.py             # 运行时事件 / SSE
    ├── topology_context.py    # 当前拓扑上下文与资源导出
    ├── prompts.py             # ENSP 操作 Prompt 模板
    ├── config.py              # 配置 & 设备注册表
    ├── console.py             # Telnet 控制台连接
    ├── exceptions.py          # 自定义异常
    ├── validation.py          # 工具参数与拓扑校验
    └── tools/
        ├── cli.py             # CLI 执行 & 配置工具
        └── topology.py        # 拓扑发现与拓扑文件处理工具
```

## 环境变量（可选）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ENSP_LOG_LEVEL` | 日志级别 | WARNING |

项目未读取 `ENSP_USERNAME` / `ENSP_PASSWORD` 环境变量。注册或同步工具中的 `username`、`password` 参数目前只保存为设备元数据，控制台连接没有实现使用这些凭据自动登录。需要交互登录的设备不能仅靠填写这些字段完成连接。

## 本地数据与隐私

运行时生成的 `devices.json`、`topology.json` 和 `topology_registry.json` 保存在项目根目录，已列入 `.gitignore`。注册凭据以明文写入本地 JSON；不要将这些文件提交到公共仓库。`.gitignore` 不会取消已经被 Git 跟踪的文件。

设备配置、控制台回显和调用记录也可能包含密码、地址或其他敏感信息。分享截图、日志和配置前应检查并脱敏；客户端配置中只填写自己的路径和所需参数，仓库示例不需要包含真实凭据。

## 社区反馈

欢迎在 [Issues](../../issues) 中留下你的想法或提出建议，一起完善这个项目。

## License

MIT
