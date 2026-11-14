# VisionPilot

> A Multimodal Autonomous GUI Agent for Intelligent Desktop Automation

**VisionPilot** 是一个基于多模态大语言模型（Multimodal LLM）的自主桌面操作智能体。它通过视觉感知理解图形用户界面（GUI），利用大语言模型进行任务规划与决策，并通过工具调用控制鼠标、键盘及其他系统接口，实现复杂桌面任务的自主执行。

与传统依赖固定脚本的 RPA 自动化工具不同，VisionPilot 希望具备环境感知、动态规划、错误恢复和经验积累能力，使 Agent 能够在面对不确定的桌面环境时自主完成任务。

## 1. 项目目标

VisionPilot 的核心目标是构建一个具备完整感知、决策、执行和反馈闭环的 GUI Agent。

项目重点解决以下问题：

- **GUI 理解：** 如何让 Agent 理解屏幕内容、界面布局、控件及当前应用状态？
- **自主规划：** 如何将用户提出的自然语言目标拆解为可执行的操作步骤？
- **工具执行：** 如何让 Agent 安全、准确地调用鼠标、键盘及系统工具？
- **动态纠错：** 当操作失败或环境发生变化时，如何识别问题并重新规划？
- **经验积累：** 如何从历史执行轨迹中提取有效经验，提高后续相似任务的执行效率？

最终希望实现一个能够接收自然语言任务、自主理解桌面环境、规划操作流程、执行任务并验证结果的通用 GUI Agent。

## 2. 核心架构

VisionPilot 采用模块化架构，主要由以下六个模块组成。

### 2.1 Perception —— 视觉感知模块

负责获取和理解当前桌面环境，为 Agent 提供结构化的环境状态。

主要功能：

- 桌面截图获取。
- OCR 文字识别。
- UI 元素检测与定位。
- 界面结构解析。
- 多模态模型辅助视觉理解。
- 当前界面状态提取。

技术方向：

- OpenCV
- OCR
- Playwright DOM / Accessibility Tree
- Vision Language Model（VLM）

### 2.2 Planner —— 任务规划模块

负责理解用户目标，并生成合理的执行计划。

主要功能：

- 自然语言任务解析。
- 复杂任务拆解。
- 子任务依赖关系管理。
- 操作步骤生成。
- 动态任务调整。
- 任务执行状态管理。

规划模块不应只生成一组固定操作指令，而应根据当前环境状态和执行反馈动态调整计划。

### 2.3 Executor —— 工具执行模块

负责将 Agent 的决策转换为实际操作。

主要功能：

- 鼠标移动与点击。
- 键盘输入与快捷键操作。
- 浏览器自动化。
- 文件系统操作。
- 系统命令调用。
- 工具调用结果反馈。

技术方向：

- PyAutoGUI
- Playwright
- Windows UI Automation
- Python 系统接口

通过统一的 Tool Interface 管理不同工具，降低 Agent 核心逻辑与具体执行工具之间的耦合。

### 2.4 Memory —— 记忆管理模块

负责保存任务执行过程中的上下文、历史经验和可复用知识。

记忆分为三个层次：

| 记忆类型        | 主要内容                           |
| --------------- | ---------------------------------- |
| Working Memory  | 当前任务、执行步骤、实时状态       |
| Episodic Memory | 历史任务、操作轨迹、成功与失败案例 |
| Semantic Memory | 可复用知识、操作经验、用户偏好     |

主要功能：

- 执行轨迹存储。
- 历史经验检索。
- 相似任务匹配。
- 成功经验提取。
- 失败案例记录。

初期采用 SQLite 实现结构化存储，后续根据检索需求扩展向量检索能力。

### 2.5 Verifier —— 结果验证模块

负责判断 Agent 的操作是否真正达到预期目标。

主要功能：

- 操作结果检查。
- 当前状态与预期状态对比。
- 任务完成条件判断。
- 操作异常检测。
- 失败原因分析。
- 触发重试或重新规划。

核心原则：

**工具调用成功不等于任务执行成功。**

Agent 必须通过实际环境反馈验证操作结果，而不是仅根据工具返回值判断任务是否完成。

反过来同样成立：**中途某一步失败，也不等于任务失败**。判定顺序是"终态目标条件"优先于"步骤历史"——只要目标里的可验证条件（应用、文本、文件）在最终画面上全部满足，任务即算完成；期间被重试或重规划救回来的失败步骤只会降低评估置信度（0.88 → 0.8），并在结果摘要里标注 `recovered from N failed step(s)`，不会再让一个已经完成的任务显示为失败。只有在无法从目标里提取出可验证条件时，才退回按步骤成败判定。

同一原则也用于计划展示：重规划（replan）会原地更新同一个计划对象，因此控制台"计划与执行"和最终结果里看到的始终是最后一次重规划后的步骤，而不是最初那版失败的计划。


### 2.6 Learning —— 经验学习模块

负责从历史执行轨迹中提取经验，为后续任务提供优化依据。

主要功能：

- 执行轨迹分析。
- 成功操作流程抽象。
- 失败模式总结。
- 可复用 Skill 生成。
- Skill 效果评估。
- 经验更新与淘汰。

该模块是项目后续重点研究方向，目标是探索如何让 Agent 随着使用次数增加，逐步改善任务执行效率和成功率。

## 3. 系统工作流程

VisionPilot 采用闭环执行模式：

**Observe → Understand → Plan → Act → Verify → Learn**

1. **Observe：** 获取当前桌面截图及环境信息。
2. **Understand：** 分析界面内容，提取当前状态。
3. **Plan：** 根据用户目标和当前状态生成执行计划。
4. **Act：** 调用相应工具执行操作。
5. **Verify：** 检查操作结果，判断是否达到预期。
6. **Learn：** 保存执行轨迹，提取成功经验和失败案例。

当验证失败时，系统将返回规划阶段，根据最新环境状态重新制定执行方案。

## 4. 技术选型

| 技术                  | 用途                     |
| --------------------- | ------------------------ |
| Python                | 核心开发语言             |
| FastAPI               | 后端服务接口             |
| DeepSeek / 多模态 LLM | 任务理解、推理与决策     |
| OpenAI SDK            | 模型接口调用             |
| OpenCV                | 图像处理                 |
| OCR                   | 屏幕文字识别             |
| Playwright            | 浏览器自动化             |
| PyAutoGUI             | 鼠标键盘控制             |
| SQLite                | 任务与经验存储           |
| Vue3 + Vite           | 可视化控制台（后续扩展） |

注：具体模型及视觉接口根据实际 API 能力选择，不将某个模型供应商绑定为系统核心依赖。

## 5. 项目目录结构

```
VisionPilot/
│
├── agent/                      # Agent 核心控制
│   ├── core.py                 # Agent 主控制器
│   ├── planner.py              # 任务规划
│   ├── reasoning.py            # 推理与决策
│   └── state.py                # 状态管理
│
├── perception/                 # 视觉感知
│   ├── screenshot.py           # 屏幕截图
│   ├── ocr.py                  # OCR 识别
│   ├── ui_detector.py          # UI 元素检测
│   └── vision.py               # 多模态视觉理解
│
├── executor/                   # 工具执行
│   ├── base.py                 # 工具接口
│   ├── mouse.py                # 鼠标操作
│   ├── keyboard.py             # 键盘操作
│   ├── browser.py              # 浏览器操作
│   └── file_system.py          # 文件操作
│
├── memory/                     # 记忆系统
│   ├── working_memory.py       # 短期记忆
│   ├── episodic_memory.py      # 情景记忆
│   ├── semantic_memory.py      # 知识记忆
│   └── database.py             # 数据存储
│
├── verifier/                   # 结果验证
│   ├── evaluator.py            # 结果评估
│   ├── checker.py              # 状态检查
│   └── recovery.py             # 错误恢复
│
├── learning/                   # 经验学习
│   ├── trajectory.py           # 执行轨迹分析
│   ├── skill_manager.py        # Skill 管理
│   └── optimizer.py            # 策略优化
│
├── api/                        # 后端接口
│   ├── routes.py
│   └── schemas.py
│
├── frontend/                   # 可视化界面（后续扩展）
│
├── config/                     # 配置管理
│   └── settings.py
│
├── tests/                      # 自动化测试
│
├── logs/                       # 执行日志
│
├── main.py                     # 项目入口
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## 6. 开发路线

项目采用渐进式开发，优先保证核心 Agent 闭环能够稳定运行，再逐步引入复杂能力。

### Phase 1：基础 GUI Agent

- 搭建 Python 项目结构。
- 接入大语言模型。
- 实现桌面截图获取。
- 实现鼠标键盘操作工具。
- 建立统一工具调用接口。
- 实现基本任务规划与执行循环。

阶段目标：能够根据自然语言指令完成简单桌面操作。

### Phase 2：多模态环境感知

- 实现 OCR 识别。
- 实现 UI 元素定位。
- 接入多模态视觉模型。
- 建立结构化 GUI 状态表示。
- 实现基于环境反馈的动态操作。

阶段目标：Agent 不再依赖固定坐标，而是能够根据界面状态决定操作。

### Phase 3：记忆与错误恢复

- 实现 Working Memory。
- 实现历史任务存储。
- 实现执行结果验证。
- 实现失败检测与重试。
- 实现动态任务重新规划。

阶段目标：提高复杂任务执行的稳定性和可靠性。

### Phase 4：经验学习与 Skill 优化

- 建立执行轨迹分析机制。
- 实现成功经验提取。
- 实现可复用 Skill 管理。
- 实现 Skill 效果评估。
- 对比经验学习前后的任务表现。

阶段目标：让 Agent 能够从历史任务中提取有效经验，并在后续相似任务中复用和优化。

### Phase 5：系统评估与可视化

- 构建 GUI Agent 测试任务集。
- 统计任务成功率。
- 统计平均操作步数与执行时间。
- 评估失败恢复能力。
- 对比不同模型和策略的表现。
- 开发可视化控制台。

阶段目标：形成可复现、可量化的实验结果。

## 7. 项目特色与研究重点

VisionPilot 不以接入多少模型或工具作为主要目标，而是重点关注 Agent 自身的执行能力。

项目重点研究以下问题：

1. **动态环境下的 GUI 感知与决策：** 如何在界面变化、操作反馈不确定的情况下保持正确的任务状态？
2. **长程任务规划：** 如何实现复杂任务的分层拆解、状态管理与动态重规划？
3. **执行可靠性：** 如何通过独立验证机制降低错误操作和虚假完成？
4. **经验驱动的能力改进：** 如何从执行轨迹中提取可复用技能，并通过实验验证其实际收益？

## 8. 项目评估指标

为了避免项目停留在功能演示阶段，将建立标准化任务测试集。

| 指标               | 评估内容               |
| ------------------ | ---------------------- |
| Task Success Rate  | 任务成功完成率         |
| Step Efficiency    | 平均操作步数           |
| Execution Time     | 平均任务耗时           |
| Recovery Rate      | 失败恢复成功率         |
| Tool Call Accuracy | 工具调用准确率         |
| Token Consumption  | 单次任务 Token 消耗    |
| Experience Gain    | 经验复用前后的性能变化 |

重点通过消融实验，分别评估记忆、验证、动态规划和经验学习模块的实际贡献。

## 9. 安全机制

桌面 Agent 具有实际操作能力，因此需要在系统设计中考虑安全性：

- 限制工具的可访问范围。
- 对高风险操作设置人工确认。
- 对终端命令实施权限和参数校验。
- 设置最大执行步数与超时限制。
- 提供任务中止机制。
- 记录关键操作及执行轨迹。

默认优先在测试环境中运行，避免未经确认地删除文件、修改重要系统配置或执行不可逆操作。

## 10. 项目愿景

VisionPilot 希望探索的不只是如何让大语言模型控制鼠标和键盘，而是如何构建一个能够理解环境、自主规划、可靠执行并从经验中持续改进的智能体。

**让 AI 不仅能够理解用户的需求，更能够真正完成用户交给它的任务。**

**Project:** VisionPilot

**Category:** AI Agent / Multimodal AI / GUI Automation

**Language:** Python

**Status:** Under Development

## 11. 当前实现与快速开始

本仓库已经实现了第 2–5 节描述的完整闭环，默认以 **模拟模式** 运行：不移动真实鼠标、不输入真实键盘，而是挂载一个可交互的合成桌面（`perception.screenshot.SimulatedDesktop`），因此无需 API Key 也能离线演示“观察 → 规划 → 执行 → 验证 → 学习”的全过程。

更完整的环境准备、API 接入、启动参数、页面指标解释与常见问题见 **[QUICKSTART.md](QUICKSTART.md)**。

### 安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt     # 运行测试
pip install -r requirements-optional.txt  # 可选：真实桌面 / OCR / 浏览器
```

### 配置

```bash
copy .env.example .env
```

- 留空 `DEEPSEEK_API_KEY` 时，规划器自动降级为规则规划器，验证器使用规则判定。
- 配置 Key 后，`VISIONPILOT_PLANNER_MODE=auto` 会优先使用 LLM 生成计划；`VISIONPILOT_LLM_ENABLED=false` 可强制离线。
- 使用真实桌面需 `VISIONPILOT_EXECUTION_MODE=live`；输入后端优先使用 `pyautogui`，Windows 未安装时自动回退到内置 `SendInput`（支持中文输入）。浏览器工具需 `playwright`，OCR 需 Tesseract 与 `pytesseract`。这些后端缺失时会自动标记为不可用，不影响其余功能。
- `VISIONPILOT_ALLOW_SHELL=false`（默认）时 `system_run_command` 工具不可用；开启后也只允许执行 `VISIONPILOT_SHELL_ALLOWLIST`（逗号分隔，默认 `where,whoami,hostname,findstr`）中的可执行文件，不接受路径或 cmd 内建命令；命令参数（含 `findstr /d:` 目录与 `/d:/Windows` 这类根目录写法）不能指向工作区外；`findstr /f:` 文件列表因无法原子校验而被拒绝，工作区存在 junction/符号链接时命令工具也会拒绝执行。
- `VISIONPILOT_CONFIRM_DANGEROUS=true` 时，删除文件、移动/覆盖等危险操作需要显式确认：CLI 使用 `--confirm-dangerous`，HTTP API 在创建任务时传 `"confirmed": true`，否则任务会以拒绝结果结束。
- API 没有鉴权，`confirmed` 只应由受信任的本地客户端发送；默认只允许本机前端源跨域（`VISIONPILOT_CORS_ORIGINS`），WebSocket `/ws/events` 也会校验 `Origin`（不带 `Origin` 的非浏览器客户端不受影响，回环地址上的同源页面不受端口限制）。把 `VISIONPILOT_CORS_ORIGINS` 设为 `*` 会显式关闭 WebSocket 来源校验；保持 `VISIONPILOT_API_HOST=127.0.0.1`，不要暴露到公网。

### 运行

```bash
python main.py init                          # 创建 logs/ workspace/ 等目录
python main.py demo                          # 4 个内置场景，全部在合成桌面执行
python main.py demo --scenario notes         # notes / explorer / settings / english
python main.py run "打开记事本，输入 hello，然后保存"
python main.py run "..." --max-steps 10 --json
python main.py --confirm-dangerous run "删除 workspace 里的旧文件"
python main.py tools                         # 查看工具注册表与可用性
python main.py status                        # 历史任务、成功率、技能与经验
fastapi run                                  # FastAPI 服务 + 可视化控制台（推荐）
```

启动服务后访问 <http://127.0.0.1:8000>，即可看到实时事件流、任务详情、统计指标、技能与工具列表；REST 接口位于 `/api/*`，WebSocket 位于 `/ws/events`。应用入口配置在根目录 `pyproject.toml`，前端 Vite 开发代理也默认连接 `8000` 端口。

任务执行中，任务详情右上角与任务列表每一行都有 **停止执行** 按钮（`POST /api/tasks/{task_id}/cancel`）：点击后状态先变为 `cancelling`，agent 会在毫秒级中断当前正在执行的工具调用、截图观察或 LLM 请求，最终以 `aborted` 结束并记录 `task cancelled by user`，已完成的步骤与截图都保留在任务详情里。

`fastapi run` 与 `fastapi dev` 的差别只有热重载：`dev` 会带上 `uvicorn --reload`，而 Windows 上 reload 模式的事件循环是 `SelectorEventLoop`（`uvicorn/loops/asyncio.py` 的 `asyncio_loop_factory(use_subprocess=True)` 返回 `SelectorEventLoop`），Playwright 只能在 `ProactorEventLoop` 上 `create_subprocess_exec`，因此在 `fastapi dev` 下 7 个 `browser_*` 工具会全部返回 `NotImplementedError`。桌面、文件、键鼠类任务在 `dev` 下不受影响；需要浏览器工具时用 `fastapi run`（或 `python -m uvicorn api.routes:app --port 8000`，同样不带 reload）。

如果 `system_launch_app` 报 `exited before a visible window appeared` 且提示 `Low integrity`，说明服务进程的完整性级别被文件标签降到了 Low（窗口永远出不来），处理方法见 [QUICKSTART 常见问题](QUICKSTART.md) 中的 `icacls ... /setintegritylevel`。

### 测试

```bash
python -m pytest -q
```

测试覆盖工具注册与沙箱、记忆三层、验证/恢复、启发式规划、端到端 Agent 循环以及 HTTP/WebSocket API，全部在临时数据库与合成桌面上运行，无需网络和真实桌面。

### 实现说明

- `agent/core.py` 是主循环：每次操作后重新截图并做独立验证（`verifier/checker.py`），失败时按 `verifier/recovery.py` 的决策重试、重新观察或重新规划；任务结束由 `verifier/evaluator.py` 按目标条件判定成功，而不是只看工具返回值。
- 取消是协作式的：`VisionPilotAgent.cancel()` 置位取消标志并唤醒内部的 `_cancel_event`，主循环用 `_cancel_aware()` 把每个 `await`（工具调用、观察、规划、验证、重规划）与取消事件竞速，输掉的一方被 `cancel()` 并以 `TaskAborted` 退出，因此不必等单个步骤的 30 秒超时；`_settle()` 的步间等待同样可被取消事件打断。
- `executor/base.py` 提供统一 Tool 接口、参数校验、风险分级与危险操作确认；文件工具被限制在 `workspace/` 内，且目录遍历、复制与删除都不会跟随 junction/符号链接逃逸沙箱；NTFS 备用数据流（`file:stream`）、Win32 尾随空格/点别名与保留设备名（`NUL`、`CON.txt`、`COM1`）也会被拒绝。
- `executor/system.py` 的 `system_launch_app` 按应用名在 PATH、注册表 App Paths 与开始菜单快捷方式中定位并直接启动（支持“微信”“记事本”等中文名）：名称匹配不区分大小写，会忽略 “app/程序/应用/软件/客户端” 等后缀，也会用快捷方式实际指向的 exe 名反查（“Douyin” → `抖音.lnk`）；启动后默认最多等待 8 秒轮询窗口，按进程映像名、子进程树和窗口标题依次匹配，并用 `SetForegroundWindow`/`AttachThreadInput`/最小化还原/置顶/点击标题栏多级手段真正把窗口切到前台——只有窗口确认在前台才算成功（后台进程的 `SetForegroundWindow` 会被 Windows 静默忽略，旧实现因此报“启动成功”但界面仍停在控制台）。能覆盖 Electron 应用首帧较慢、启动器把窗口交给已运行实例后立即退出等情况，返回 `window_found`、`focused`、窗口标题与 PID，失败时给出可诊断的原因。`...\Microsoft\WindowsApps\` 下的商店应用别名只交给 shell 激活（`CreateProcess` 会以 `0xC0000409` 崩溃），但商店应用的冷启动仍可能超过等待时间，建议对这类应用放宽 `wait`。该工具会拒绝 shell/脚本宿主（含 `mintty`、`tclsh`、`wscript`、`python3.12`、`javaw` 等别名/版本变体与快捷方式目标）以及 `.bat`/`.ps1`/`.msi` 等脚本或安装包；带解释器前缀的普通 GUI 应用（如 PhpStorm、RubyMine）不受影响；`executor/native_input.py` 提供 Windows SendInput 鼠标键盘与 Unicode 输入后备。
- `executor/browser.py` 只接受 `http://`、`https://` 与 `about:blank`；`file://`、`javascript:`、`data:` 等协议会被拒绝，避免浏览器绕过文件沙箱。浏览器工具还依赖 Windows 的 `ProactorEventLoop`：`fastapi dev`（等价 `uvicorn --reload`）会使用 `SelectorEventLoop`，Playwright 在其上无法 `create_subprocess_exec`，所有 `browser_*` 都会返回 `NotImplementedError`，因此推荐用 `fastapi run` 启动服务。
- 需要提权的程序不会被启动：UAC 同意框位于安全桌面（`Winlogon`），本进程既截不到也点不到。`system_launch_app` 会读取目标 exe 的 manifest（`RT_MANIFEST` 资源，非 PE 文件回退到字节扫描），对 `requireAdministrator` 立即返回 `needs_admin` 与具体级别；`highestAvailable`（标准用户其实可以非提权启动）交给 `CreateProcess` 判定，`WinError 740` 同样转为 `needs_admin` 而不是弹出无人应答的 UAC 框。`verifier/recovery.py` 对这类失败直接重规划，不浪费重试次数。
- 截图启用 DPI 感知并默认捕获主显示器；多显示器环境下如需其他屏幕，应在 `perception/screenshot.py` 中扩展捕获区域。LLM/VLM 返回的坐标会按实际发送图片尺寸缩放到完整截图。
- `memory/` 使用 SQLite 保存任务、步骤、经验与知识；`learning/` 从成功轨迹沉淀 Skill，并在后续规划时作为提示注入。
- `api/` 提供 FastAPI 接口与 WebSocket 事件流，`frontend/` 使用 Vue 3 + Vite 构建控制台；运行 `cd frontend && npm install && npm run build` 后由 FastAPI 提供 `frontend/dist/`。
- 当前启发式规划器面向内置演示桌面与常见中文/英文指令；真实桌面上的复杂任务建议配置多模态 LLM。
