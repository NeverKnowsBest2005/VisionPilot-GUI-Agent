"""VisionPilot command line entry point.

Usage examples::

    python main.py run "打开记事本，输入 hello，然后保存"
    python main.py --live run "..." --max-steps 12
    python main.py demo --scenario all
    python main.py serve --port 8765
    python main.py tools
    python main.py status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT, Settings, load_settings
from config.logging import configure_logging

DEMO_GOALS: dict[str, str] = {
    "notes": "打开记事本，输入 VisionPilot 已经跑通端到端闭环，然后保存",
    "explorer": "打开文件资源管理器，进入 Documents 文件夹，打开 notes.txt",
    "settings": "打开设置",
    "english": 'open the Notes app and type "hello from VisionPilot"',
}


def _configure_console() -> None:
    """Best effort UTF-8 console so Chinese goals are readable on Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - exotic terminals
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="visionpilot",
        description="VisionPilot - 视觉 GUI Agent（观察→理解→规划→执行→验证→学习）",
    )
    parser.add_argument("--env-file", default=".env", help="dotenv 文件（默认 .env）")
    parser.add_argument(
        "--live",
        action="store_true",
        help="使用真实鼠标键盘（默认 simulate 模拟执行）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本消费")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="运行一条自然语言任务")
    run.add_argument("goal", help="任务目标，例如：打开记事本并输入 hello")
    run.add_argument("--max-steps", type=int, default=None, help="覆盖最大步数")
    run.add_argument("--task-id", default=None, help="自定义任务 ID")

    serve = sub.add_parser("serve", help="启动 FastAPI + 前端控制台")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="开发模式热重载")

    demo = sub.add_parser("demo", help="运行内置演示场景")
    demo.add_argument(
        "--scenario",
        default="all",
        choices=["all", *DEMO_GOALS.keys()],
        help="演示场景",
    )

    sub.add_parser("tools", help="列出工具注册表及依赖状态")
    sub.add_parser("status", help="显示配置、数据库与技能概况")
    sub.add_parser("init", help="创建 logs/workspace 目录并初始化数据库")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if getattr(args, "live", False):
        overrides["execution_mode"] = "live"
    settings = load_settings(env_file=args.env_file, overrides=overrides)
    settings.ensure_directories()
    return settings


def _print_run_result(result, *, max_steps: int | None = None) -> None:
    icon = "✅" if result.success else "❌"
    print(f"\n{icon} 任务{('成功' if result.success else '未完成')}：{result.summary}")
    print(f"   状态 {result.status} | {result.duration_ms} ms | {len(result.steps)} 步 | "
          f"{result.tokens} tokens | {result.llm_calls} 次 LLM 调用")
    if result.plan and result.plan.get("source"):
        print(f"   计划来源：{result.plan['source']} (revision {result.plan.get('revision', 0)})")
    for step in result.steps:
        mark = "✓" if step.get("ok") else "✗"
        print(f"   {mark} {step.get('index', 0):02d}. {step.get('tool')} "
              f"{(step.get('arguments_preview') or '')[:80]}")
        observation = str(step.get("observation") or "").strip()
        if observation:
            print(f"        {observation[:160]}")
    evaluation = result.evaluation or {}
    criteria = evaluation.get("criteria") or []
    if criteria:
        print("   验证：")
        for criterion in criteria:
            mark = "✓" if criterion.get("matched") else "✗"
            print(f"   {mark} {criterion.get('kind')}: {criterion.get('value')} "
                  f"{criterion.get('note') or ''}".rstrip())
    if result.error and not result.success:
        print(f"   错误：{result.error}")


async def _run_task(settings: Settings, goal: str, max_steps: int | None, task_id: str | None) -> int:
    from agent import VisionPilotAgent

    configure_logging(settings, stream=False)

    async def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "step_finished":
            mark = "✓" if event.get("ok") else "✗"
            print(f"   {mark} {event.get('tool')} · {str(event.get('observation') or '')[:120]}")
        elif event.get("type") == "recovery":
            print(f"   ↻ 恢复策略：{event.get('action')} · {event.get('reason')}")

    agent = VisionPilotAgent(settings, event_callback=on_event)
    banner = "simulate（模拟桌面）" if settings.uses_synthetic_desktop else settings.execution_mode
    print(f"VisionPilot · 模式 {banner} · LLM {'已配置' if settings.llm_ready else '规则模式'}")
    print(f"目标：{goal}")
    try:
        result = await agent.run(goal, task_id=task_id, max_steps=max_steps)
    finally:
        await agent.aclose()
    _print_run_result(result)
    return 0 if result.success else 1


async def _run_demo(settings: Settings, scenario: str) -> int:
    from agent import VisionPilotAgent

    configure_logging(settings, stream=False)
    names = list(DEMO_GOALS) if scenario == "all" else [scenario]
    successes = 0
    for name in names:
        goal = DEMO_GOALS[name]
        print(f"\n=== 演示 {name} ===\n目标：{goal}")
        agent = VisionPilotAgent(settings)
        try:
            result = await agent.run(goal)
        finally:
            await agent.aclose()
        _print_run_result(result)
        successes += 1 if result.success else 0
    print(f"\n演示完成：{successes}/{len(names)} 成功")
    return 0 if successes == len(names) else 1


def _cmd_tools(settings: Settings, as_json: bool) -> int:
    from executor import build_default_registry

    registry = build_default_registry(settings)
    tools = registry.all()
    if as_json:
        print(json.dumps([
            {
                "name": tool.name,
                "description": tool.description,
                "danger": tool.danger,
                "category": tool.category,
                "available": tool.is_available(),
                "reason": tool.unavailable_reason(),
            }
            for tool in tools
        ], ensure_ascii=False, indent=2))
        return 0
    groups: dict[str, list] = {}
    for tool in tools:
        groups.setdefault(tool.category, []).append(tool)
    available = sum(1 for tool in tools if tool.is_available())
    print(f"工具注册表：{len(tools)} 个（可用 {available}）")
    for category, items in groups.items():
        print(f"\n[{category}]")
        for tool in items:
            mark = "●" if tool.is_available() else "○"
            reason = "" if tool.is_available() else f"  ({tool.unavailable_reason()})"
            print(f"  {mark} {tool.name:<22} {tool.danger:<9} {tool.description}{reason}")
    return 0


def _cmd_status(settings: Settings, as_json: bool) -> int:
    from executor import build_default_registry
    from learning import SkillManager
    from memory import Database, EpisodicMemory, SemanticMemory

    registry = build_default_registry(settings)
    db = Database.from_settings(settings)
    try:
        episodic = EpisodicMemory(db)
        semantic = SemanticMemory(db)
        skills = SkillManager(db)
        stats = episodic.stats()
        payload = {
            "settings": settings.safe_dict(),
            "counts": db.table_counts(),
            "stats": stats,
            "skills": skills.stats(),
            "lessons": [item.content for item in semantic.lessons(limit=5)],
            "tools": {
                "total": len(registry.all()),
                "available": len(registry.available()),
            },
        }
    finally:
        db.close()
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"项目：{settings.app_name} v{settings.version}")
    print(f"根目录：{PROJECT_ROOT}")
    print(f"模式：{settings.execution_mode}（dry_run={settings.dry_run}, "
          f"合成桌面={settings.uses_synthetic_desktop}）")
    print(f"LLM：{'已配置 ' + settings.llm_model if settings.llm_ready else '未配置（规则规划）'}")
    print(f"数据库：{settings.db_path}")
    print(f"工具：{payload['tools']['available']}/{payload['tools']['total']} 可用")
    print(f"任务：{stats.get('tasks_total', 0)} | 经验：{stats.get('episodes', 0)} | "
          f"成功率：{stats.get('success_rate', 0):.1%}")
    print(f"技能：{payload['skills'].get('total', 0)} 个"
          f"（活跃 {payload['skills'].get('active', 0)}）")
    if payload["lessons"]:
        print("最近经验：")
        for lesson in payload["lessons"]:
            print(f"  - {lesson}")
    return 0


def _cmd_init(settings: Settings) -> int:
    from memory import Database

    settings.ensure_directories()
    db = Database.from_settings(settings)
    db.close()
    print("初始化完成：")
    print(f"  logs       {settings.log_dir}")
    print(f"  screenshots {settings.screenshot_dir}")
    print(f"  workspace  {settings.workspace_dir}")
    print(f"  database   {settings.db_path}")
    return 0


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    host = args.host or settings.api_host
    port = args.port or settings.api_port
    configure_logging(settings, stream=True)
    print(f"VisionPilot 控制台：http://{host}:{port}  （Ctrl+C 退出）")
    if settings.execution_mode == "live":
        print("⚠️  当前为 live 模式：Agent 将控制真实鼠标和键盘。")
    uvicorn.run(
        "api.routes:app",
        host=host,
        port=port,
        reload=bool(args.reload),
        log_level=str(settings.log_level).lower(),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    settings = _settings(args)

    if args.command == "run":
        return asyncio.run(_run_task(settings, args.goal, args.max_steps, args.task_id))
    if args.command == "demo":
        return asyncio.run(_run_demo(settings, args.scenario))
    if args.command == "tools":
        return _cmd_tools(settings, args.json)
    if args.command == "status":
        return _cmd_status(settings, args.json)
    if args.command == "init":
        return _cmd_init(settings)
    if args.command == "serve":
        return _cmd_serve(settings, args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
