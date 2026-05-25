"""
周报服务 — 聚合打分 + 统计 + AI 分析 → 生成完整周报

Sprint C: 替换通用 Prompt 为 periph.txt #7 风格的专业周报 Prompt
  - 300-450 字叙述体，不用条列
  - 温和真实，从具体行为切入
  - 支持从 prompts/weekly_report.md 加载外部 Prompt

生成流程:
  1. calculate_all_spirits()  → 5个精灵得分
  2. build_tree_data()        → 生命树数据
  3. calculate_weekly_stats() → 统计数据(完成率、最高效日等)
  4. llm_generate_analysis()  → AI 分析(headline、highlights、suggestions)
  5. save WeeklyReport        → 存储

Sprint 3 增量:
  - generate_weekly_report 中传入 focus_snapshot + quality_notes 给 _generate_analysis
  - score_lines 增加 raw_score / focus_weight / partial 信息
  - 新增 _collect_quality_notes 方法
  - 新增 _format_focus_for_prompt 方法
"""
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, and_, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.report import WeeklyReport, WeeklySummary
from app.models.task import Task, SubTask
from app.models.score import SpiritWeeklyScore
from app.services.scoring_service import ScoringService, SPIRIT_CODES
from app.services.tree_service import TreeService
from app.services.weekly_focus_service import WeeklyFocusService
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
        self.focus_svc = WeeklyFocusService(db)

    async def generate_weekly_report(
        self,
        user_id: uuid.UUID,
        week_start: date,
        force: bool = False,
    ) -> WeeklyReport:
        week_end = week_start + timedelta(days=6)

        if not force:
            existing = await self.get_report(user_id, week_start)
            if existing:
                return existing
        else:
            await self._delete_existing(user_id, week_start)

        scores = await self.scoring_svc.calculate_all_spirits(user_id, week_start)

        tree_data = await self.tree_svc.build_tree_data(user_id, week_start)

        stats = await self._calculate_weekly_stats(user_id, week_start, week_end)

        overall_score = await self.scoring_svc.get_overall_score(user_id, week_start)
        vs_last_week = await self._calc_vs_last_week(user_id, week_start, overall_score)

        focus_snapshot = await self.focus_svc.get_focus_snapshot(user_id, week_start)
        quality_notes = await self._collect_quality_notes(user_id, week_start, week_end)

        analysis = await self._generate_analysis(
            scores, stats, tree_data, overall_score, vs_last_week,
            focus_snapshot=focus_snapshot,
            quality_notes=quality_notes,
        )

        headline = analysis.get(
            "headline",
            self._fallback_headline(
                overall_score, vs_last_week, focus_snapshot.get("label")
            ),
        )
        suggestions = analysis.get("suggestions", [])

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

    async def get_report(self, user_id: uuid.UUID, week_start: date) -> Optional[WeeklyReport]:
        result = await self.db.execute(
            select(WeeklyReport).where(
                WeeklyReport.user_id == user_id,
                WeeklyReport.week_start == week_start,
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_report(self, user_id: uuid.UUID) -> Optional[WeeklyReport]:
        result = await self.db.execute(
            select(WeeklyReport)
            .where(WeeklyReport.user_id == user_id)
            .order_by(desc(WeeklyReport.week_start))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def generate_weekly_summary(
        self, user_id: uuid.UUID, week_start: date,
    ) -> WeeklySummary:
        week_end = week_start + timedelta(days=6)

        existing = await self._get_existing_summary(user_id, week_start)
        if existing:
            return existing

        stats = await self._calculate_weekly_stats(user_id, week_start, week_end)
        scores = await self.scoring_svc.get_week_scores(user_id, week_start)

        key_events = self._extract_key_events(stats, scores)
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

    async def _calculate_weekly_stats(
        self, user_id: uuid.UUID, week_start: date, week_end: date,
    ) -> dict:
        ws_dt = datetime.combine(week_start, datetime.min.time())
        we_dt = datetime.combine(week_end, datetime.max.time())

        # 周报任务数：直接统计父任务（与 App 日历里创建的一条一致）
        result = await self.db.execute(
            select(Task)
            .where(
                Task.user_id == user_id,
                or_(
                    and_(
                        Task.deadline.isnot(None),
                        Task.deadline >= ws_dt,
                        Task.deadline <= we_dt,
                    ),
                    and_(
                        Task.deadline.is_(None),
                        Task.created_at >= ws_dt,
                        Task.created_at <= we_dt,
                    ),
                ),
            )
            .options(selectinload(Task.subtasks))
        )
        tasks = list(result.scalars().all())

        def _completion_percent(t: Task) -> int:
            if t.status == "completed":
                return 100
            subs = t.subtasks or []
            if not subs:
                return 0
            return max(st.completion_percent or 0 for st in subs)

        def _is_done(t: Task) -> bool:
            # App 通过子任务 completion 更新进度，父任务 status 可能仍为 pending
            return t.status == "completed" or _completion_percent(t) >= 100

        def _task_hours(t: Task) -> float:
            if t.estimated_hours and t.estimated_hours > 0:
                return float(t.estimated_hours)
            return 1.0

        total_planned = len(tasks)
        completed = [t for t in tasks if _is_done(t)]
        cancelled = [t for t in tasks if t.status == "cancelled"]

        total_hours_planned = sum(_task_hours(t) for t in tasks)
        total_hours_actual = sum(_task_hours(t) for t in completed)

        completion_rate = len(completed) / total_planned if total_planned > 0 else 0

        by_spirit = {}
        for code in SPIRIT_CODES:
            spirit_tasks = [t for t in tasks if t.primary_spirit == code]
            spirit_completed = [t for t in spirit_tasks if _is_done(t)]
            by_spirit[code] = {
                "planned": len(spirit_tasks),
                "completed": len(spirit_completed),
                "hours_planned": round(sum(_task_hours(t) for t in spirit_tasks), 1),
            }

        by_day: dict[str, int] = {}
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for t in completed:
            ref = t.deadline or t.created_at
            if ref:
                day_name = weekday_names[ref.weekday()]
                by_day[day_name] = by_day.get(day_name, 0) + 1

        most_productive_day = max(by_day, key=by_day.get) if by_day else "N/A"

        by_hour: dict[int, int] = {}
        for t in completed:
            ref = t.deadline or t.created_at
            if ref:
                h = ref.hour
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
        if st.scheduled_start and st.actual_end:
            delta = (st.actual_end - st.scheduled_start).total_seconds() / 60
            return max(0, delta)
        return st.duration_minutes or 60

    async def _calc_vs_last_week(
        self, user_id: uuid.UUID, week_start: date, current_score: float
    ) -> Optional[float]:
        last_week = week_start - timedelta(days=7)
        last_scores = await self.scoring_svc.get_week_scores(user_id, last_week)
        if not last_scores:
            return None

        total_w = 0
        weighted = 0
        for s in last_scores:
            base_w = max(1, s.intensity_at_scoring)
            focus_w = float(s.focus_weight or 1.0)
            w = base_w * focus_w
            weighted += s.score * w
            total_w += w
        last_overall = weighted / total_w if total_w else 0

        return round(current_score - last_overall, 1)

    async def _generate_analysis(
        self,
        scores: list[SpiritWeeklyScore],
        stats: dict,
        tree_data: dict,
        overall_score: float,
        vs_last_week: Optional[float],
        focus_snapshot: Optional[dict] = None,
        quality_notes: Optional[list[dict]] = None,
    ) -> dict:
        # 映射精灵代码到自然语言描述
        spirit_label_map = {
            "light": "学习工作",
            "water": "娱乐放松",
            "soil": "身体健康",
            "air": "社交互动",
            "nutrition": "兴趣爱好",
        }
        
        # 构建自然语言的行为描述（不包含技术词汇）
        behavior_lines = []
        
        # 先整理按方向的任务数据
        by_spirit_data = stats.get("by_spirit", {})
        for spirit_code in ["light", "water", "soil", "air", "nutrition"]:
            label = spirit_label_map.get(spirit_code, spirit_code)
            spirit_stats = by_spirit_data.get(spirit_code, {})
            planned = spirit_stats.get("planned", 0)
            completed = spirit_stats.get("completed", 0)
            hours = spirit_stats.get("hours_planned", 0)
            
            # 收集这个方向的部分完成任务
            partial_notes = []
            if quality_notes:
                for note in quality_notes:
                    if note["spirit"] == spirit_code and 0 < note["completion_percent"] < 100:
                        partial_notes.append(note)
            
            if planned > 0:
                line = f"- {label}：计划了 {planned} 件事，完整完成 {completed} 件"
                if hours > 0:
                    line += f"，预计投入约 {hours} 小时"
                if partial_notes:
                    partial_count = len(partial_notes)
                    line += f"，另有 {partial_count} 件做了一部分"
                behavior_lines.append(line)
        
        # 统计整体完成情况
        total_planned = stats.get("total_tasks_planned", 0)
        total_completed = stats.get("total_tasks_completed", 0)
        completion_rate = stats.get("completion_rate", 0)
        most_productive_day = stats.get("most_productive_day", "N/A")
        most_productive_hour = stats.get("most_productive_hour", 10)
        
        # 构建部分完成任务的说明（不带精灵名称）
        notes_block = ""
        if quality_notes:
            top_notes = quality_notes[:8]
            if top_notes:
                note_lines = []
                for note in top_notes:
                    pct = note["completion_percent"]
                    if 0 < pct < 100:
                        note_line = f"- 《{note['title'][:30]}》：做了 {pct}%"
                        if note["note"]:
                            note_line += f"，你提到「{note['note'][:60]}」"
                        note_lines.append(note_line)
                if note_lines:
                    notes_block = "\n部分完成的任务：\n" + "\n".join(note_lines)
        
        # 构建与上周的对比（不带分数）
        trend_block = ""
        if vs_last_week is not None:
            if vs_last_week > 0:
                trend_block = "\n整体节奏比上周更饱满一些"
            elif vs_last_week < 0:
                trend_block = "\n整体节奏比上周稍缓一些"
            else:
                trend_block = "\n整体节奏与上周相近"
        
        # 加载外部prompt
        external_prompt = load_prompt("weekly_report")
        
        if external_prompt:
            system = external_prompt
        else:
            system = """你是精灵日程系统的周报撰写者, 一位长期陪伴用户生活的记录者。

写作要求:
- 叙述体, 300-450字, 不要 bullet/编号
- 从具体行为切入, 不要空泛
- 不使用"分数""维度""精灵""权重"等技术词
- 如果用户为本周设了重点方向, 重点方向是周报主线
- 重点方向表现好 → 肯定; 表现差 → 直接温和点出
- 非重点低分 → 肯定取舍, 不当问题

输出 JSON:
{
  "headline": "≤25字带 emoji",
  "narrative": "300-450字叙述",
  "highlights": ["2-3点"],
  "improvements": ["1-2点"],
  "patterns": ["行为模式"],
  "suggestions": ["下周 2-3 条具体建议"]
}"""
        
        # 构建用户prompt（只包含行为数据，不包含技术词汇）
        user_prompt = f"""本周共安排了 {total_planned} 件事，完整完成了 {total_completed} 件，完成率 {completion_rate:.0%}。
最高效的一天是 {most_productive_day}，最高效时段是 {most_productive_hour}:00。

各方向的投入情况：
{chr(10).join(behavior_lines)}{trend_block}{notes_block}"""
        
        # 如果有focus_snapshot，也用自然语言传递
        if focus_snapshot and focus_snapshot.get("theme"):
            label = focus_snapshot.get("label", "")
            key_spirits = focus_snapshot.get("key_spirits") or []
            key_labels = [spirit_label_map.get(c, c) for c in key_spirits]
            key_str = "、".join(key_labels) if key_labels else ""
            
            weights = focus_snapshot.get("weights") or {}
            higher = [spirit_label_map.get(c, c) for c, w in weights.items() if w >= 1.3]
            lower = [spirit_label_map.get(c, c) for c, w in weights.items() if w <= 0.7]
            
            focus_line = f"\n\n这周你重点关注的是「{label}」"
            if key_str:
                focus_line += f"，主要在 {key_str} 上"
            if higher or lower:
                parts = []
                if higher:
                    parts.append(f"主动多安排了 {', '.join(higher)}")
                if lower:
                    parts.append(f"主动减少了 {', '.join(lower)}")
                focus_line += f"（{'; '.join(parts)}）"
            user_prompt += focus_line
        
        result = await llm_client.complete_json(
            system=system,
            user=user_prompt,
            purpose="weekly_analysis",
        )
        
        if result and result.get("headline"):
            result.setdefault("narrative", "")
            result.setdefault("highlights", [])
            result.setdefault("improvements", [])
            result.setdefault("patterns", [])
            result.setdefault("suggestions", [])
            return result
        
        return self._fallback_analysis(
            scores, stats, overall_score, vs_last_week, focus_snapshot
        )

    @staticmethod
    def _format_focus_for_prompt(focus_snapshot: Optional[dict]) -> str:
        if not focus_snapshot or not focus_snapshot.get("theme"):
            return ""

        label = focus_snapshot.get("label", "")
        key_spirits = focus_snapshot.get("key_spirits") or []
        key_names = [SPIRIT_NAMES.get(c, c) for c in key_spirits]
        key_str = "、".join(key_names) if key_names else "(无具体重点)"

        weights = focus_snapshot.get("weights") or {}
        higher = [SPIRIT_NAMES.get(c, c) for c, w in weights.items() if w >= 1.3]
        lower = [SPIRIT_NAMES.get(c, c) for c, w in weights.items() if w <= 0.7]
        weight_hint = ""
        if higher or lower:
            parts = []
            if higher:
                parts.append(f"主动加重: {', '.join(higher)}")
            if lower:
                parts.append(f"主动收敛: {', '.join(lower)}")
            weight_hint = " (" + "; ".join(parts) + ")"

        return (
            f"\n本周用户主动设的方向: 「{label}」, 重点是 {key_str}{weight_hint}\n"
        )

    async def _collect_quality_notes(
        self, user_id: uuid.UUID, week_start: date, week_end: date,
    ) -> list[dict]:
        ws_dt = datetime.combine(week_start, datetime.min.time())
        we_dt = datetime.combine(week_end, datetime.max.time())

        result = await self.db.execute(
            select(SubTask).join(Task).where(
                Task.user_id == user_id,
                SubTask.scheduled_start != None,
                SubTask.scheduled_start >= ws_dt,
                SubTask.scheduled_start <= we_dt,
                SubTask.quality_note != None,
                SubTask.quality_note != "",
            )
        )
        subtasks = list(result.scalars().all())

        def _sort_key(st):
            pct = st.completion_percent or 0
            is_partial = 1 if 0 < pct < 100 else 0
            return (-is_partial, -pct)

        subtasks.sort(key=_sort_key)

        return [
            {
                "subtask_id": str(st.id),
                "spirit": st.spirit,
                "title": st.title,
                "completion_percent": st.completion_percent or 0,
                "note": st.quality_note or "",
            }
            for st in subtasks
        ]

    def _fallback_analysis(
        self, scores, stats, overall_score, vs_last_week,
        focus_snapshot: Optional[dict] = None,
    ) -> dict:
        highlights = []
        improvements = []
        best = max(scores, key=lambda s: s.score) if scores else None
        worst = min(scores, key=lambda s: s.score) if scores else None

        key_spirits = set((focus_snapshot or {}).get("key_spirits") or [])

        key_scores = [s for s in scores if s.spirit_code in key_spirits]
        for s in key_scores:
            name = SPIRIT_NAMES.get(s.spirit_code, "")
            if s.score >= 70:
                highlights.append(f"本周重点 {name} 表现达标 ({s.score}分)")
            elif s.score < 50:
                improvements.append(f"本周重点 {name} 偏低 ({s.score}分), 需要重新调整节奏")

        if best and best.score >= 70 and best.spirit_code not in key_spirits:
            name = SPIRIT_NAMES.get(best.spirit_code, "")
            highlights.append(f"{name}意外表现出色 ({best.score}分)")

        rate = stats.get("completion_rate", 0)
        if rate >= 0.8:
            highlights.append(f"完成率达到 {rate:.0%}, 执行力很强")
        elif rate < 0.5:
            improvements.append(f"完成率仅 {rate:.0%}, 需要减少任务量或提高专注度")

        if worst and worst.score < 50 and worst.spirit_code not in key_spirits:
            name = SPIRIT_NAMES.get(worst.spirit_code, "")
            highlights.append(f"{name}本周取舍合理, 不强求")

        focus_label = (focus_snapshot or {}).get("label")
        headline = self._fallback_headline(overall_score, vs_last_week, focus_label)

        return {
            "headline": headline,
            "highlights": highlights or ["本周有所进步"],
            "improvements": improvements or ["继续保持"],
            "patterns": [],
            "suggestions": ["下周尝试提前规划任务", "保持运动习惯"],
        }

    @staticmethod
    def _fallback_headline(
        overall: float,
        vs_last_week: Optional[float],
        focus_label: Optional[str] = None,
    ) -> str:
        prefix = f"「{focus_label}」 " if focus_label else ""
        if overall >= 85:
            return f"✨ {prefix}精彩的一周, 继续保持!"
        elif overall >= 65:
            trend = ""
            if vs_last_week and vs_last_week > 3:
                trend = "📈 "
            return f"{trend}{prefix}稳步前进的一周"
        elif overall >= 45:
            return f"💪 {prefix}平稳的一周, 下周争取更好"
        else:
            return f"🌱 {prefix}需要调整节奏, 加油!"

    def _extract_key_events(
        self, stats: dict, scores: list[SpiritWeeklyScore]
    ) -> list[dict]:
        events = []

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

        rate = stats.get("completion_rate", 0)
        if rate >= 0.9:
            events.append({"type": "high_completion", "desc": f"完成率{rate:.0%}"})
        elif rate < 0.4:
            events.append({"type": "low_completion", "desc": f"完成率仅{rate:.0%}"})

        return events

    async def _generate_narrative(
        self, stats: dict, scores: list, key_events: list
    ) -> str:
        events_str = "; ".join(e["desc"] for e in key_events[:5])
        planned = stats.get("total_tasks_planned", 0)
        completed = stats.get("total_tasks_completed", 0)
        rate = stats.get("completion_rate", 0)

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
