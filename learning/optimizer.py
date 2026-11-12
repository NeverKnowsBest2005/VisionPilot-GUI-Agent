"""Performance tuning: metrics in, concrete suggestions out.

This is the "optimise" half of the learning loop.  It never mutates the running
configuration on its own - it computes metrics, surfaces weak spots and feeds
short hints back into planning prompts.  The suggestions are meant for the
operator (shown in the web console) as much as for the agent.
"""

from __future__ import annotations

import logging
from typing import Any

from config.settings import Settings
from learning.skill_manager import SkillManager
from memory.episodic_memory import EpisodicMemory
from memory.semantic_memory import SemanticMemory
from memory.text import short

logger = logging.getLogger("visionpilot.learning.optimizer")


class PerformanceOptimizer:
    def __init__(
        self,
        settings: Settings,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        skills: SkillManager,
    ) -> None:
        self.settings = settings
        self.episodic = episodic
        self.semantic = semantic
        self.skills = skills

    # -------------------------------------------------------------- metrics
    def metrics(self) -> dict[str, Any]:
        stats = self.episodic.stats()
        episodes = self.episodic.list_episodes(limit=50)
        recent = episodes[:10]
        previous = episodes[10:20]
        recent_rate = _rate(recent)
        previous_rate = _rate(previous)
        trend = "flat"
        if previous:
            if recent_rate > previous_rate + 0.1:
                trend = "improving"
            elif recent_rate < previous_rate - 0.1:
                trend = "declining"

        failures_in_trajectory = 0
        episodes_with_failures = 0
        recovered = 0
        for episode in episodes:
            failed = [step for step in episode.trajectory if not step.get("ok", True)]
            if failed:
                episodes_with_failures += 1
                failures_in_trajectory += len(failed)
                if episode.success:
                    recovered += 1

        success_rate = float(stats.get("success_rate") or 0.0)
        return {
            **stats,
            "recent_success_rate": round(recent_rate, 3),
            "previous_success_rate": round(previous_rate, 3),
            "trend": trend,
            "avg_seconds": round(float(stats.get("avg_duration_ms") or 0) / 1000.0, 2),
            "tokens_per_task": int(
                float(stats.get("tokens_total") or 0) / max(1, int(stats.get("episodes") or 0))
            ),
            "steps_per_task": float(stats.get("avg_steps") or 0.0),
            "failed_steps": failures_in_trajectory,
            "episodes_with_failures": episodes_with_failures,
            "recovery_rate": round(recovered / episodes_with_failures, 3) if episodes_with_failures else None,
            "skills": self.skills.stats(),
            "memory": self.semantic.stats(),
            "success_rate": round(success_rate, 3),
        }

    # ----------------------------------------------------------- suggestions
    def suggestions(self, limit: int = 6) -> list[dict[str, str]]:
        metrics = self.metrics()
        episodes = int(metrics.get("episodes") or 0)
        tips: list[dict[str, str]] = []

        if episodes >= 5 and float(metrics.get("success_rate") or 0) < 0.6:
            tips.append(
                {
                    "severity": "high",
                    "title": "成功率偏低",
                    "detail": (
                        f"当前成功率 {float(metrics['success_rate']):.0%}（{episodes} 个回合）。"
                        "建议缩短计划步长、增加每步校验，并检查失败轨迹里反复出现的工具。"
                    ),
                }
            )
        if float(metrics.get("steps_per_task") or 0) > 8 and episodes >= 3:
            tips.append(
                {
                    "severity": "medium",
                    "title": "计划粒度过细",
                    "detail": (
                        f"平均 {metrics['steps_per_task']:.1f} 步/任务。可以把「移动鼠标→点击」"
                        "合并为一次点击，并优先使用键盘快捷键。"
                    ),
                }
            )
        if float(metrics.get("tokens_per_task") or 0) > 12000 and episodes >= 3:
            tips.append(
                {
                    "severity": "medium",
                    "title": "上下文开销偏大",
                    "detail": (
                        f"平均 {metrics['tokens_per_task']} tokens/任务。减少历史步数、"
                        "压缩可见文本（只保留前 20 行）可显著降低成本。"
                    ),
                }
            )
        unreliable = [
            (name, data)
            for name, data in self.episodic.tool_reliability().items()
            if data["calls"] >= 3 and data["rate"] < 0.55
        ]
        if unreliable:
            names = ", ".join(f"{name}({data['rate']:.0%})" for name, data in unreliable[:3])
            tips.append(
                {
                    "severity": "high",
                    "title": "工具可靠性差",
                    "detail": f"这些工具失败率偏高：{names}。检查坐标定位、等待时间与可用性判断。",
                }
            )
        recovery = metrics.get("recovery_rate")
        if recovery is not None and recovery < 0.4 and int(metrics.get("episodes_with_failures") or 0) >= 3:
            tips.append(
                {
                    "severity": "medium",
                    "title": "恢复策略需要加强",
                    "detail": (
                        f"只有 {recovery:.0%} 的失败回合被恢复。建议在失败后先重新观察屏幕，"
                        "再决定重试还是重新规划。"
                    ),
                }
            )
        if episodes >= 3 and int((metrics.get("skills") or {}).get("total") or 0) == 0:
            tips.append(
                {
                    "severity": "low",
                    "title": "还没有沉淀技能",
                    "detail": "成功且步骤 ≥ 2 的任务会自动生成技能；可多跑几个完整任务来积累复用经验。",
                }
            )
        if metrics.get("trend") == "declining":
            tips.append(
                {
                    "severity": "medium",
                    "title": "近期成功率下降",
                    "detail": "最近 10 个任务比之前明显变差，建议检查环境变化（窗口位置、分辨率、应用版本）。",
                }
            )
        if not tips:
            tips.append(
                {
                    "severity": "low",
                    "title": "状态良好",
                    "detail": "成功率、步骤数与工具可靠性都在正常范围，继续保持。",
                }
            )
        return tips[:limit]

    # --------------------------------------------------------------- hints
    def prompt_hints(self, goal: str, max_chars: int = 1200) -> str:
        """Short, high-signal context for the planner prompt."""
        lines: list[str] = []
        similar = self.episodic.similar_episodes(goal, limit=2, only_success=True)
        if similar:
            for episode in similar:
                lines.append(
                    f"PAST SUCCESS ({episode.score:.2f} similar, {episode.steps_count} steps): "
                    f"{short(episode.goal, 120)}"
                )
                if episode.lessons:
                    lines.append(f"  lesson: {short(episode.lessons[0], 160)}")
        lessons = self.semantic.recall(goal, kinds=["lesson", "hint"], limit=2)
        for lesson in lessons:
            lines.append(f"MEMORY: {short(lesson.content, 160)}")
        matches = self.skills.match(goal, limit=2)
        if matches:
            lines.append(SkillManager.render_prompt(matches, max_skills=2))
        return "\n".join(lines)[:max_chars]

    def report(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics(),
            "suggestions": self.suggestions(),
            "skills": [skill.as_dict() for skill in self.skills.list(limit=20)],
            "knowledge": [item.as_dict() for item in self.semantic.all(limit=20)],
            "recent_episodes": [episode.as_dict() for episode in self.episodic.list_episodes(limit=10)],
        }


def _rate(episodes: list[Any]) -> float:
    if not episodes:
        return 0.0
    return sum(1 for episode in episodes if episode.success) / len(episodes)
