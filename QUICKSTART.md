# VisionPilot 启动与配置说明

本文汇总运行 VisionPilot 的环境准备、API 接入、启动命令、模拟/真实模式差异和常见问题。项目根目录即本文件所在目录。

---

## 1. 环境准备

要求 Python 3.10+（本项目在 Python 3.14 下验证），Windows 推荐使用项目自带的 `.venv`。

```powershell
# 如 .venv 已存在，跳过前两行
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

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
- 修改后需重启 `serve` / 重新执行 `run` 才生效。
- 是否接入成功可看 `python main.py status` 的 `LLM：` 一行，或网页顶部 `LLM 已配置`。
- 追求完全离线：`VISIONPILOT_LLM_ENABLED=false`。

相关代码位置：

- 配置字段与加载：[config/settings.py](config/settings.py)（`llm_api_key`、`llm_ready`、`load_settings`）
- SDK 调用与多模态请求：[agent/reasoning.py](agent/reasoning.py)（`LLMClient`）

---

## 3. 启动项目

所有命令都在项目根目录执行，使用 `.venv` 里的 Python。

### 3.1 Web 控制台 + 后端 API（推荐）

```powershell
.\.venv\Scripts\python.exe main.py serve
```

浏览器访问 **http://127.0.0.1:8765**。

同时提供：

- 可视化控制台：`frontend/index.html`
- REST 接口：`/api/*`（路由见 [api/routes.py](api/routes.py)）
- WebSocket 实时事件：`/ws/events`

常用参数：

```powershell
# 改端口 / 监听地址
.\.venv\Scripts\python.exe main.py serve --host 0.0.0.0 --port 9000

# 开发热重载
.\.venv\Scripts\python.exe main.py serve --reload

# 真实桌面模式（全局参数必须放在子命令之前）
.\.venv\Scripts\python.exe main.py --live serve
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
.\.venv\Scripts\python.exe main.py --live serve
.\.venv\Scripts\python.exe main.py --live run "打开记事本"
```

> 注意：`--live` 是全局参数，必须写在 `serve` / `run` / `demo` 之前，写在后面会报参数错误。

安全边界（两种模式都生效）：

- 文件工具被限制在 `workspace/` 内，禁止 `../` 越界。
- `fs_delete`、`system_run_command` 等危险工具默认需要确认；`ALLOW_SHELL=false` 时命令工具直接拒绝。
- 最大步数、单任务超时、重试与重规划次数都有上限。
- 默认不自动确认任何危险操作。

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

换端口 `--port 9001`，或修改 `.env` 的 `VISIONPILOT_API_PORT`。确认终端里 `serve` 没有报错退出。

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

从 0.1.1 起新增 `system_launch_app`：按应用名在 PATH、注册表 App Paths 和开始菜单快捷方式中查找并直接启动，不再依赖模拟 Win 键打开开始菜单（部分环境下注入的 Win 键会被系统忽略）。`main.py tools` 中显示为可用即表示该工具就绪。修改代码后需要重启 `main.py serve` 才会生效。

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
.\.venv\Scripts\python.exe main.py serve

# 4. 打开 http://127.0.0.1:8765
```

默认就是模拟模式，点页面里的演示按钮或输入目标即可看到完整闭环。
