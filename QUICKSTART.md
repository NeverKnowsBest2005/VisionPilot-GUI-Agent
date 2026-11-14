# VisionPilot 启动与配置说明

本文汇总运行 VisionPilot 的环境准备、API 接入、启动命令、模拟/真实模式差异和常见问题。项目根目录即本文件所在目录。

---

## 1. 环境准备

要求 Python 3.10+（本项目在 Python 3.14 下验证），Windows 推荐使用项目自带的 `.venv`。

```powershell
# 如 .venv 已存在，跳过前两行
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\Activate.ps1

# 开发/测试
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 可选后端：真实鼠标键盘、OCR、浏览器自动化
.\.venv\Scripts\python.exe -m pip install -r requirements-optional.txt
```

可选依赖说明：

| 依赖 | 用途 | 缺失时 |
| --- | --- | --- |
| `pyautogui` | 真实鼠标/键盘（live 模式，优先使用） | Windows 下自动回退到内置 `SendInput` 后端，仍可 live；其他平台只能用 simulate |
| `playwright` + `playwright install chromium` | 浏览器工具 | `browser_*` 工具标记为不可用 |
| `pytesseract` + Tesseract | OCR | 模拟桌面回退到内置文本；真实截图无文字识别 |
| `opencv-python` | 图像轮廓检测 | 仅用 OCR 文本框做 UI 检测 |
| `mss` | 真实屏幕截图 | 回退到 Pillow / PyAutoGUI |

---

## 2. 接入大模型 API（可选）

不配置 API Key 也能运行：规划器自动降级为规则规划，验证器使用规则判定。

```powershell
copy .env.example .env
```

编辑 `.env`：

```ini
# DeepSeek / OpenAI 兼容接口
DEEPSEEK_API_KEY=sk-你的密钥
VISIONPILOT_LLM_BASE_URL=https://api.deepseek.com
VISIONPILOT_LLM_MODEL=deepseek-chat
VISIONPILOT_LLM_VISION_MODEL=deepseek-chat
VISIONPILOT_LLM_ENABLED=true
VISIONPILOT_PLANNER_MODE=auto          # auto | llm | heuristic
```

- 也支持系统环境变量：`VISIONPILOT_LLM_API_KEY`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY` 均可作为密钥来源。
- `.env` 必须放在项目根目录，或启动时用全局参数 `--env-file 路径` 指定。
- 修改后需重启 `fastapi dev` / 重新执行 `run` 才生效。
- 是否接入成功可看 `python main.py status` 的 `LLM：` 一行，或网页顶部 `LLM 已配置`。
- 追求完全离线：`VISIONPILOT_LLM_ENABLED=false`。

相关代码位置：

- 配置字段与加载：[config/settings.py](config/settings.py)（`llm_api_key`、`llm_ready`、`load_settings`）
- SDK 调用与多模态请求：[agent/reasoning.py](agent/reasoning.py)（`LLMClient`）

---

## 3. 启动项目

所有命令都在项目根目录执行，使用 `.venv` 里的 Python。

前端使用 Vue 3 + Vite。首次运行或修改前端后，在项目根目录构建一次：

```powershell
cd frontend
npm install
npm run build
cd ..
```

FastAPI CLI 会提供 `frontend/dist` 中的构建产物。前端开发时可另开终端运行 `cd frontend; npm run dev`，Vite 会将 `/api` 和 `/ws` 请求代理到本地后端。

### 3.1 Web 控制台 + 后端 API（推荐）

```powershell
fastapi dev
```

根目录 `pyproject.toml` 已配置应用入口（`api.routes:app`），无需在命令中重复指定文件路径。FastAPI dev 默认启用代码热重载。

浏览器访问 **http://127.0.0.1:8000**。

同时提供：

- Vue 可视化控制台：`frontend/src/`（生产构建输出到 `frontend/dist/`）
- REST 接口：`/api/*`（路由见 [api/routes.py](api/routes.py)）
- WebSocket 实时事件：`/ws/events`

常用参数：

```powershell
# 改端口 / 监听地址
fastapi dev --host 0.0.0.0 --port 9000

# 真实桌面模式：先在 .env 中设置 VISIONPILOT_EXECUTION_MODE=live
fastapi dev --port 8765
```

> 默认只监听 `127.0.0.1`，没有登录鉴权。改成 `0.0.0.0` 暴露到局域网前请确认网络环境安全。

### 3.2 单条任务（不启动服务）

```powershell
.\.venv\Scripts\python.exe main.py run "打开记事本，输入 hello，然后保存"
.\.venv\Scripts\python.exe main.py run "打开设置" --max-steps 5
.\.venv\Scripts\python.exe main.py run "..." --json     # 机器可读输出
```

### 3.3 内置演示

```powershell
.\.venv\Scripts\python.exe main.py demo
.\.venv\Scripts\python.exe main.py demo --scenario notes   # notes / explorer / settings / english
```

### 3.4 其他命令

```powershell
.\.venv\Scripts\python.exe main.py init      # 创建 logs/、workspace/ 并初始化数据库
.\.venv\Scripts\python.exe main.py tools     # 工具清单与可用性（含缺失依赖提示）
.\.venv\Scripts\python.exe main.py status    # 模式、LLM、数据库、成功率、技能
```

### 3.5 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试全部使用临时数据库和合成桌面，不需要网络、真实桌面或 API Key。

---

## 4. 页面上的指标是什么意思

顶部状态条：

| 显示 | 含义 |
| --- | --- |
| `模式 模拟` | 当前是 `simulate`，所有操作发往内置合成桌面，不碰真实鼠标键盘；`dry_run=True` |
| `模式 实时桌面` | `live` 模式，会真实操作 Windows 桌面 |
| `LLM 已配置` / `规则模式` | 是否检测到有效 API Key；未配置时用规则规划 |
| `工具 23/30` | 注册 30 个工具，23 个当前可用（缺 Playwright/OCR 等可选依赖） |
| `历史 N 任务 · M 经验` | SQLite 中累计的任务数与成功/失败经验条数 |

页面上方还有：

- **合成桌面=True**：模拟模式下挂载的虚拟桌面，包含 File Explorer、Notes、Settings 和示例文件。
- **成功率 / 平均步数 / 平均耗时 / 恢复率 / Token**：来自历史任务统计；无数据时显示 0 或 —。
- **技能**：成功任务自动沉淀的可复用操作流程；**工具**：可展开查看每个工具的参数与可用性。

---

## 5. 模拟模式 vs 真实桌面

默认配置（`.env.example` 中的值）：

```ini
VISIONPILOT_EXECUTION_MODE=simulate
VISIONPILOT_SIMULATE_DESKTOP=true
```

- **simulate**：安全演练。截图是程序绘制的 1440×900 合成桌面，点击/输入只修改内存状态；任务、验证、经验、技能照常真实记录。适合演示、测试和开发。
- **live**：真实桌面。优先使用 `pyautogui`；Windows 上未安装时自动回退到内置 `SendInput` 后端（支持中文等 Unicode 输入）。建议先跑简单任务（如打开记事本输入文字），不要在重要窗口测试删除类操作。

切换方式：

```ini
VISIONPILOT_EXECUTION_MODE=live
VISIONPILOT_SIMULATE_DESKTOP=false
```

或启动时加全局参数：

```powershell
在 `.env` 中设置 `VISIONPILOT_EXECUTION_MODE=live`，再运行 `fastapi dev --port 8765`。
.\.venv\Scripts\python.exe main.py --live run "打开记事本"
```

> 注意：Web 服务运行模式请在 `.env` 设置；`--live` 是 `main.py` 单任务 CLI 的全局参数。

安全边界（两种模式都生效）：

- 文件工具被限制在 `workspace/` 内，禁止 `../` 越界；递归遍历、复制和删除不会跟随指向沙箱外的 junction/符号链接，NTFS 备用数据流（`file:stream`）、尾随空格/点别名和保留设备名（`NUL`、`CON.txt`、`COM1`）也会被拒绝。
- `fs_delete` 等危险工具默认需要确认：CLI 在子命令前加 `--confirm-dangerous`（如 `main.py --confirm-dangerous run "..."`），HTTP API 创建任务时传 `"confirmed": true`。默认不自动确认任何危险操作。
- `ALLOW_SHELL=false` 时 `system_run_command` 工具直接不可用；开启后也只允许白名单中的可执行文件（默认 `where,whoami,hostname,findstr`），命令参数不能指向工作区外的路径（含 `findstr /d:` 的目录参数和 `/d:/Windows` 这类根目录写法）。`findstr /f:` 读取的文件列表无法原子校验，会被直接拒绝；工作区存在 junction/符号链接时命令工具也拒绝执行，因为 `findstr /s`、`where /r` 等子进程会跟随链接读到沙箱外。
- `system_launch_app` 拒绝 `cmd`、`powershell`、`wscript`、`mshta`、`python`、`wt`、`py`、`mintty`、`tclsh` 等 shell/脚本宿主（含 `python3.12`、`javaw` 这类带版本/后缀的变体）；中文别名（如“终端”）和开始菜单快捷方式的实际目标也会检查，`.msi`/`.pif` 目标同样拒绝，避免绕过 `ALLOW_SHELL` 后门执行命令。普通 GUI 应用（如 “FileZilla FTP Client”、“PhpStorm”、“RubyMine”）不会因为名称里含网络工具或解释器前缀而被误拦。
- 浏览器工具只允许 `http://`、`https://` 与 `about:blank`，`file://` 等协议会被拒绝；WebSocket `/ws/events` 只接受 `VISIONPILOT_CORS_ORIGINS` 中的浏览器源，以及回环地址上的同源页面（`fastapi dev` 的 8000 端口可直接使用）；无 `Origin` 的本地客户端不受影响。将 `VISIONPILOT_CORS_ORIGINS` 设为 `*` 会显式关闭该校验。
- 最大步数、单任务超时、重试与重规划次数都有上限。

---

## 6. 数据与日志位置

| 路径 | 内容 |
| --- | --- |
| `logs/visionpilot.db` | SQLite：任务、步骤、经验、知识、技能 |
| `logs/visionpilot.log` | 运行日志 |
| `logs/events.jsonl` | API 服务产生的结构化事件流 |
| `logs/screenshots/` | 截图，保留最近 `KEEP_SCREENSHOTS` 张（默认 80） |
| `workspace/` | 文件工具的沙箱目录 |

想清空演示数据（会删除全部任务/经验/技能）：

```powershell
Remove-Item logs\visionpilot.db* -Force
Remove-Item logs\screenshots\* -Recurse -Force
.\.venv\Scripts\python.exe main.py init
```

---

## 7. 常见问题

**Q：执行 `main.py` 提示找不到模块 / 依赖缺失？**

确认用的是项目的虚拟环境：`.\.venv\Scripts\python.exe`，并已安装 `requirements.txt`。不要直接双击 `main.py`。

**Q：网页打不开 / 端口被占用？**

换端口可运行 `fastapi dev --port 9001`。确认终端中的 FastAPI CLI 服务没有报错退出。

**Q：配置了 API Key 但页面仍显示“规则模式”？**

检查 `.env` 是否在项目根目录、`DEEPSEEK_API_KEY` 是否非空、`VISIONPILOT_LLM_ENABLED` 是否为 `true`，并重启服务；可先用 `main.py status` 确认。

**Q：浏览器工具显示不可用？**

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -m playwright install chromium
```

**Q：任务一直卡在 running？**

检查日志 `logs/visionpilot.log`；可在网页任务详情里取消，或调用 `POST /api/tasks/{task_id}/cancel`。单任务默认 300 秒超时（`VISIONPILOT_MAX_TASK_SECONDS`）。

**Q：任务成功但报告“未完成”？**

这是刻意设计：工具调用成功不等于任务成功，最终由 `verifier/` 根据目标条件独立验证。若期望条件不合理，可调整目标描述中的关键词（应用名、文件名、要输入的文字）。

**Q：真实模式没反应？**

确认 `--live` / `VISIONPILOT_EXECUTION_MODE=live` 生效，Windows 会话不是锁屏状态。输入后端优先用 `pyautogui`，缺失时使用内置 `SendInput`；两者都不可用会在工具返回中提示。建议先用 `main.py --live run "打开记事本"` 验证。中文应用名可直接输入（内置后端支持 Unicode）。

**Q：打开微信 / QQ 等应用失败？**

从 0.1.1 起新增 `system_launch_app`：按应用名在 PATH、注册表 App Paths 和开始菜单快捷方式中查找并直接启动，不再依赖模拟 Win 键打开开始菜单（部分环境下注入的 Win 键会被系统忽略）。`main.py tools` 中显示为可用即表示该工具就绪。修改代码后需要重启 `fastapi dev` 才会生效。

启动失败时先看返回的 `error` 与 `data`：

- `found the … window (…) but Windows would not bring it to the foreground`：窗口找到了但没抢到前台。旧版本在这里只调用一次 `SetForegroundWindow`（后台进程会被系统静默忽略），于是报“已启动”而界面仍停在控制台；现在会依次尝试 `AttachThreadInput`、`SwitchToThisWindow`、最小化还原、`SetWindowPos` 置顶与点击标题栏，只有确认窗口在前台才返回成功。若仍失败，通常是目标应用以管理员权限运行而本程序不是（UIPI 隔离），请用相同权限启动两者。
- `no visible … window appeared` / `exited before a visible window appeared`：进程起来了但没等到窗口。Electron 应用（微信、抖音、VS Code）冷启动首帧可能 3–10 秒，可把 `wait` 参数调到 10；若目标应用本身无法在当前账户下启动（例如受限账户写不了 `%APPDATA%` 下的 profile），窗口永远不会出现，这属于环境问题。商店应用（记事本、画图、计算器）走 `...\Microsoft\WindowsApps\` 别名，只能由 shell 激活，冷启动可能超过等待时间。
- `could not find an installed application named …`：名称没命中。可以改用可执行文件名（`douyin`、`code`）或快捷方式名（`抖音`、`Visual Studio Code`）；名称匹配不区分大小写，也会忽略 “app/程序/客户端” 后缀，并用快捷方式指向的 exe 名反查。
- `needs administrator rights (requestedExecutionLevel=…)`：目标程序的 manifest 要求提权（或在 `highestAvailable` 下 `CreateProcess` 返回 `WinError 740`）。UAC 同意框显示在安全桌面上，agent 既截不到也点不到，因此工具会**立即拒绝**这种目标而不再空等窗口（`data.needs_admin=true`），恢复策略也不会重试同一步。遇到这类程序（`mmc`、`dcomcnfg`、安装包等）请手动启动并在弹窗里点“是”，然后让任务继续做后面的步骤。
- 名称里有空格时用引号包住，例如 `"Visual Studio Code"`。

**Q：中文输出乱码？**

代码与文件均为 UTF-8；旧版 Windows 控制台可能显示乱码，可执行 `chcp 65001` 或使用 Windows Terminal。网页控制台不受影响。

---

## 8. 最短启动清单

```powershell
# 1. 依赖（首次）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 配置（可选，接大模型）
copy .env.example .env
# 编辑 .env 填入 DEEPSEEK_API_KEY

# 3. 启动
fastapi dev --port 8765

# 4. 打开 http://127.0.0.1:8765
```

默认就是模拟模式，点页面里的演示按钮或输入目标即可看到完整闭环。
