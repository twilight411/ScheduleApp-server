"""
周报服务 — 聚合打分 + 统计 + AI 分析 → 生成完整周报

Sprint C: 替换通用 Prompt 为 periph.txt #7 风格的专业周报 Prompt
  - 300-450 字叙述体，不用条列
  - 温和真实，从具体行为切入
  - 支持从 prompts/weekly_analysis.md 加载外部 Prompt

生成流程:
  1. calculate_all_spirits()  → 5个精灵得分
  2. build_tree_data()        → 生命树数据
  3. calculate_weekly_stats() → 统计数据(完成率、最高效日等)
  4. llm_generate_analysis()  → AI 分析(headline、highlights、suggestions)
  5. save WeeklyReport        → 存储

同时负责:
  - 周行为摘要 (WeeklySummary) 生成
  - 周报查询 / 最新周报 / 重新生成
"""
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import WeeklyReport, WeeklySummary
from app.models.task import Task, SubTask
from app.models.score import SpiritWeeklyScore
from app.services.scoring_service import ScoringService, SPIRIT_CODES
from app.services.tree_service import TreeService
from app.ai.llm_client import llm_client
from app.utils.prompt_loader import load_prompt

import structlog

logger = structlog.get_logger()

SPIRIT_NAMES = {
    "light": "光精灵", "water": "水精灵", "soil": "土壤精灵",
    "air": "空气精灵", "nutrition": "营养精灵",
}


class ReportService:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.scoring_svc = ScoringService(db)
        self.tree_svc = TreeService(db)

    # ========================================
    #  生成周报
    # ========================================

    async def generate_weekly_report(
        self,
        user_id: uuid.UUID,
        week_start: date,
        force: bool = False,
    ) -> WeeklyReport:
        """
        生成或重新生成周报。
        force=True 时删除已有记录重新生成。
        """
        week_end = week_start + timedelta(days=6)

        # 幂等：检查是否已有周报
        if not force:
            existing = await self.get_report(user_id, week_start)
            if existing:
                return existing
        else:
            await self._delete_existing(user_id, week_start)

        # Step 1: 打分
        scores = await self.scoring_svc.calculate_all_spirits(user_id, week_start)

        # Step 2: 生命树
        tree_data = await self.tree_svc.build_tree_data(user_id, week_start)

        # Step 3: 统计数据
        stats = await self._calculate_weekly_stats(user_id, week_start, week_end)

        # Step 4: 总分 + 对比上周
        overall_score = await self.scoring_svc.get_overall_score(user_id, week_start)
        vs_last_week = await self._calc_vs_last_week(user_id, week_start, overall_score)

        # Step 5: AI 分析
        analysis = await self._generate_analysis(
            scores, stats, tree_data, overall_score, vs_last_week
        )

        headline = analysis.get("headline", self._fallback_headline(overall_score, vs_last_week))
        suggestions = analysis.get("suggestions", [])

        # Step 6: 存储
        report = WeeklyReport(
            user_id=user_id,
            week_start=week_start,
            week_end=week_end,
            headline=headline,
            overall_score=overall_score,
            vs_last_week=vs_last_week,
            stats=stats,
            tree_data=tree_data,
            analysis=analysis,
            next_week_suggestions=suggestions,
        )
        self.db.add(report)
        await self.db.flush()

        logger.info(
            "weekly_report_generated",
            user_id=str(user_id),
            week_start=str(week_start),
            score=overall_score,
        )

        return report

    # ========================================
    #  查询
    # ========================================

    async def get_report(
        self, user_id: uuid.UUID, week_start: date
    ) -> Optional[WeeklyReport]:
        result = await self.db.execute(
            select(WeeklyReport).where(
                WeeklyReport.user_id == user_id,
                WeeklyReport.week_start == week_start,
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_report(
        self, user_id: uuid.UUID
    ) -> Optional[WeeklyReport]:
        result = await self.db.execute(
            select(WeeklyReport)
            .where(WeeklyReport.user_id == user_id)
            .order_by(desc(WeeklyReport.week_start))
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ========================================
    #  周行为摘要
    # ========================================

    async def generate_weekly_summary(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> WeeklySummary:
        """
        生成周行为摘要（用于 Context Engineering）。
        比周报更简洁，主要是文本叙述 + 关键事件列表。
        """
        week_end = week_start + timedelta(days=6)

        # 检查幂等
        existing = await self._get_existing_summary(user_id, week_start)
        if existing:
            return existing

        # 获取本周统计
        stats = await self._calculate_weekly_stats(user_id, week_start, week_end)
        scores = await self.scoring_svc.get_week_scores(user_id, week_start)

        # 关键事件
        key_events = self._extract_key_events(stats, scores)

        # 生成叙述
        narrative = await self._generate_narrative(stats, scores, key_events)

        summary = WeeklySummary(
            user_id=user_id,
            week_start=week_start,
            narrative=narrative,
            key_events=key_events,
        )
        self.db.add(summary)
        await self.db.flush()

        return summary

    # ========================================
    #  统计计算
    # ========================================

    async def _calculate_weekly_stats(
        self,
        user_id: uuid.UUID,
        week_start: date,
        week_end: date,
    ) -> dict:
        """计算周统计数据"""
        ws_dt = datetime.combine(week_start, datetime.min.time())
        we_dt = datetime.combine(week_end, datetime.max.time())

        # 获取本周所有子任务
        result = await self.db.execute(
            select(SubTask).join(Task).where(
                Task.user_id == user_id,
                SubTask.scheduled_start != None,
                SubTask.scheduled_start >= ws_dt,
                SubTask.scheduled_start <= we_dt,
            )
        )
        subtasks = list(result.scalars().all())

        total_planned = len(subtasks)
        completed = [st for st in subtasks if st.status == "completed"]
        cancelled = [st for st in subtasks if st.status == "cancelled"]

        total_hours_planned = sum(
            (st.duration_minutes or 60) for st in subtasks
        ) / 60
        total_hours_actual = sum(
            self._calc_actual_minutes(st) for st in completed
        ) / 60

        completion_rate = len(completed) / total_planned if total_planned > 0 else 0

        # 按精灵分类
        by_spirit = {}
        for code in SPIRIT_CODES:
            spirit_tasks = [st for st in subtasks if st.spirit == code]
            spirit_completed = [st for st in spirit_tasks if st.status == "completed"]
            by_spirit[code] = {
                "planned": len(spirit_tasks),
                "completed": len(spirit_completed),
                "hours_planned": round(
                    sum((st.duration_minutes or 60) for st in spirit_tasks) / 60, 1
                ),
            }

        # 最高效的一天
        by_day: dict[str, int] = {}
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for st in completed:
            if st.scheduled_start:
                day_name = weekday_names[st.scheduled_start.weekday()]
                by_day[day_name] = by_day.get(day_name, 0) + 1

        most_productive_day = max(by_day, key=by_day.get) if by_day else "N/A"

        # 最高效的小时
        by_hour: dict[int, int] = {}
        for st in completed:
            if st.scheduled_start:
                h = st.scheduled_start.hour
                by_hour[h] = by_hour.get(h, 0) + 1
        most_productive_hour = max(by_hour, key=by_hour.get) if by_hour else 10

        return {
            "total_tasks_planned": total_planned,
            "total_tasks_completed": len(completed),
            "total_tasks_cancelled": len(cancelled),
            "total_hours_planned": round(total_hours_planned, 1),
            "total_hours_actual": round(total_hours_actual, 1),
            "completion_rate": round(completion_rate, 2),
            "most_productive_day": most_productive_day,
            "most_productive_hour": most_productive_hour,
            "by_spirit": by_spirit,
        }

    @staticmethod
    def _calc_actual_minutes(st) -> float:
        """计算子任务实际耗时"""
        if st.scheduled_start and st.actual_end:
            delta = (st.actual_end - st.scheduled_start).total_seconds() / 60
            return max(0, delta)
        return st.duration_minutes or 60

    async def _calc_vs_last_week(
        self, user_id: uuid.UUID, week_start: date, current_score: float
    ) -> Optional[float]:
        """对比上周"""
        last_week = week_start - timedelta(days=7)
        last_scores = await self.scoring_svc.get_week_scores(user_id, last_week)
        if not last_scores:
            return None

        total_w = 0
        weighted = 0
        for s in last_scores:
            w = max(1, s.intensity_at_scoring)
            weighted += s.score * w
            total_w += w
        last_overall = weighted / total_w if total_w else 0

        return round(current_score - last_overall, 1)

    # ========================================
    #  AI 分析
    # ========================================

    async def _generate_analysis(
        self,
        scores: list[SpiritWeeklyScore],
        stats: dict,
        tree_data: dict,
        overall_score: float,
        vs_last_week: Optional[float],
    ) -> dict:
        """
        LLM 生成周报分析 — Sprint C 升级版

        Prompt 策略 (来自 periph.txt #7):
          - 叙述体，300-450 字，不用条列/bullet
          - 温和真实，从具体行为切入
          - 不要空泛鼓励，要指出"这周你做了什么 → 带来了什么变化"
          - 改进建议要具体到"下周 X 天做 Y"
        """
        # 构建上下文
        score_lines = []
        for s in scores:
            name = SPIRIT_NAMES.get(s.spirit_code, s.spirit_code)
            planned = s.task_stats.get("planned", 0)
            completed = s.task_stats.get("completed", 0)
            score_lines.append(
                f"- {name}: {s.score}分({s.level}), "
                f"设计{s.design_score}+完成{s.completion_score}+质量{s.quality_score}, "
                f"计划{planned}个任务/完成{completed}个"
            )

        trend_str = ""
        if vs_last_week is not None:
            if vs_last_week > 0:
                trend_str = f"比上周上升 {abs(vs_last_week)} 分"
            elif vs_last_week < 0:
                trend_str = f"比上周下降 {abs(vs_last_week)} 分"
            else:
                trend_str = "与上周持平"

        # 尝试从 prompts/weekly_analysis.md 加载外部 Prompt
        external_prompt = load_prompt("weekly_report")

        if external_prompt:
            system = external_prompt
        else:
            # 内置的 periph.txt #7 风格 Prompt
            system = """你是精灵日程系统的周报撰写者。你的任务是根据用户本周的五精灵得分和行为数据，写一份温暖真实的周报分析。

## 写作要求
1. **叙述体**：用流畅的段落，不要使用条列、bullet point 或编号列表
2. **300-450 字**：不多不少，像一封朋友的信
3. **从具体行为切入**：不要说"你做得不错"，要说"你这周完成了 X 个任务中的 Y 个，特别是周三那天..."
4. **温和真实**：好的要肯定，不足的要直说但不伤人，像一个了解你的朋友
5. **改进建议要具体**：不要说"下周加油"，要说"下周试着在周二和周四各安排一次30分钟的运动"

## 输出格式
请输出 JSON：
{
  "headline": "一句话标题（不超过25字，要有emoji，积极但实事求是）",
  "narrative": "300-450字的叙述体周报正文（不含任何列表格式）",
  "highlights": ["用一句话概括做得好的2-3个点"],
  "improvements": ["用一句话概括需要改进的1-2个点"],
  "patterns": ["发现的行为模式（如果有的话）"],
  "suggestions": ["下周的2-3条具体建议，每条带时间和动作"]
}

## 重要
narrative 字段是核心输出，必须是流畅的叙述段落，绝对不要出现 - / * / 1. 等列表标记。"""

        completion_rate = stats.get("completion_rate", 0)
        user_prompt = f"""本周总分：{overall_score} {trend_str}

各精灵得分：
{chr(10).join(score_lines)}

统计：
- 计划 {stats.get('total_tasks_planned', 0)} 个任务，完成 {stats.get('total_tasks_completed', 0)} 个
- 完成率 {completion_rate:.0%}
- 最高效日：{stats.get('most_productive_day', 'N/A')}
- 最高效时段：{stats.get('most_productive_hour', 10)}:00
- 生命树健康度：{tree_data.get('tree_health', 'N/A')}
- 季节标签：{tree_data.get('season_label', 'N/A')}"""

        result = await llm_client.complete_json(
            system=system,
            user=user_prompt,
            purpose="weekly_analysis",
        )

        if result and result.get("headline"):
            # 兼容新旧格式：如果有 narrative 但没有 highlights，从 narrative 提取
            result.setdefault("narrative", "")
            result.setdefault("highlights", [])
            result.setdefault("improvements", [])
            result.setdefault("patterns", [])
            result.setdefault("suggestions", [])
            return result

        # Fallback
        return self._fallback_analysis(scores, stats, overall_score, vs_last_week)

    def _fallback_analysis(
        self, scores, stats, overall_score, vs_last_week
    ) -> dict:
        """LLM 不可用时的降级分析"""
        highlights = []
        improvements = []
        best = max(scores, key=lambda s: s.score) if scores else None
        worst = min(scores, key=lambda s: s.score) if scores else None

        if best and best.score >= 70:
            name = SPIRIT_NAMES.get(best.spirit_code, "")
            highlights.append(f"{name}表现出色，得分{best.score}")

        rate = stats.get("completion_rate", 0)
        if rate >= 0.8:
            highlights.append(f"完成率达到 {rate:.0%}，执行力很强")
        elif rate < 0.5:
            improvements.append(f"完成率仅 {rate:.0%}，需要减少任务量或提高专注度")

        if worst and worst.score < 50:
            name = SPIRIT_NAMES.get(worst.spirit_code, "")
            improvements.append(f"{name}得分偏低({worst.score})，需要更多关注")

        return {
            "headline": self._fallback_headline(overall_score, vs_last_week),
            "highlights": highlights or ["本周有所进步"],
            "improvements": improvements or ["继续保持"],
            "patterns": [],
            "suggestions": ["下周尝试提前规划任务", "保持运动习惯"],
        }

    @staticmethod
    def _fallback_headline(overall: float, vs_last_week: Optional[float]) -> str:
        if overall >= 85:
            return "✨ 精彩的一周！继续保持！"
        elif overall >= 65:
            trend = ""
            if vs_last_week and vs_last_week > 3:
                trend = "📈 "
            return f"{trend}稳步前进的一周，做得不错！"
        elif overall >= 45:
            return "💪 平稳的一周，下周争取更好"
        else:
            return "🌱 需要调整节奏，加油！"

    # ========================================
    #  周行为摘要辅助
    # ========================================

    def _extract_key_events(
        self, stats: dict, scores: list[SpiritWeeklyScore]
    ) -> list[dict]:
        """提取关键事件"""
        events = []

        # 高分精灵
        for s in scores:
            if s.score >= 90:
                events.append({
                    "type": "high_score",
                    "spirit": s.spirit_code,
                    "score": s.score,
                    "desc": f"{SPIRIT_NAMES.get(s.spirit_code, '')}得分{s.score}(繁茂)",
                })
            elif s.score <= 30:
                events.append({
                    "type": "low_score",
                    "spirit": s.spirit_code,
                    "score": s.score,
                    "desc": f"{SPIRIT_NAMES.get(s.spirit_code, '')}得分仅{s.score}(枯萎)",
                })

        # 完成率
        rate = stats.get("completion_rate", 0)
        if rate >= 0.9:
            events.append({"type": "high_completion", "desc": f"完成率{rate:.0%}"})
        elif rate < 0.4:
            events.append({"type": "low_completion", "desc": f"完成率仅{rate:.0%}"})

        return events

    async def _generate_narrative(
        self, stats: dict, scores: list, key_events: list
    ) -> str:
        """生成行为摘要叙述"""
        events_str = "; ".join(e["desc"] for e in key_events[:5])
        planned = stats.get("total_tasks_planned", 0)
        completed = stats.get("total_tasks_completed", 0)
        rate = stats.get("completion_rate", 0)

        # 简单模板，不调 LLM（摘要用于 Context 而非用户展示）
        narrative = (
            f"本周安排{planned}个任务，完成{completed}个(完成率{rate:.0%})。"
            f"最高效日为{stats.get('most_productive_day', 'N/A')}。"
        )
        if events_str:
            narrative += f"关键表现：{events_str}。"

        return narrative

    async def _get_existing_summary(
        self, user_id: uuid.UUID, week_start: date
    ) -> Optional[WeeklySummary]:
        result = await self.db.execute(
            select(WeeklySummary).where(
                WeeklySummary.user_id == user_id,
                WeeklySummary.week_start == week_start,
            )
        )
        return result.scalar_one_or_none()

    async def _delete_existing(self, user_id: uuid.UUID, week_start: date):
        """删除已有周报（重新生成时用）"""
        result = await self.db.execute(
            select(WeeklyReport).where(
                WeeklyReport.user_id == user_id,
                WeeklyReport.week_start == week_start,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            await self.db.delete(existing)
            await self.db.flush()