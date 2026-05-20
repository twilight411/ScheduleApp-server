"""
任务解析器 (Module 1) — 自然语言 → 结构化任务
用 LLM 识别意图、提取实体、分配精灵
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from app.ai.llm_client import llm_client
from app.utils.prompt_loader import load_prompt

logger = structlog.get_logger()

PARSER_SYSTEM_PROMPT = """你是精灵日程系统的任务解析器。
你的任务是将用户的自然语言输入解析为结构化的任务数据。

## 精灵领域定义
| 精灵 | 代号 | 领域 | 关键词 |
|------|------|------|--------|
| 光精灵 | light | 工作、学习、职业发展 | 开会、复习、写报告、考试、项目、培训、上课 |
| 水精灵 | water | 娱乐、休闲、放松 | 看电影、打游戏、休息、度假、追剧、刷视频 |
| 土壤精灵 | soil | 健康、运动、作息 | 跑步、健身、睡眠、体检、吃药、瑜伽 |
| 空气精灵 | air | 社交、人际关系 | 聚会、约会、拜访、送礼、联系朋友 |
| 营养精灵 | nutrition | 兴趣爱好、个人成长 | 画画、弹琴、读书、学摄影、写作、学做饭 |

## 规则
1. 单任务涉及多领域时，选最主要的为 primary_spirit，其他放 secondary_spirits
2. 时间提取：绝对时间（1月15日）、相对时间（明天、下周五、这周末）、重复（每天、每周一）
3. 优先级：有"紧急/重要/必须/DDL近"→ high，"有空/闲时/可以的话"→ low，其余 medium
4. 如果信息不足（时间不明/类型不明），设 needs_clarification=true 并提问
5. estimated_hours 请根据常识估算合理时长

## 重要：当前日期是 {current_date}

请只输出 JSON，不要输出其他内容。格式如下：
{{
  "tasks": [
    {{
      "title": "精炼后的任务名",
      "raw_fragment": "对应的原始输入片段",
      "primary_spirit": "light",
      "secondary_spirits": [],
      "deadline": "2024-01-15T23:59:00 或 null",
      "estimated_hours": 5,
      "priority": "high",
      "is_recurring": false,
      "recurrence_pattern": null,
      "needs_clarification": false,
      "clarification_question": null,
      "extracted_entities": {{
        "time": "下周五",
        "location": null,
        "people": [],
        "tools": []
      }}
    }}
  ],
  "overall_confidence": 0.9,
  "suggestions": []
}}"""


# ===== 解析结果兜底 =====
FALLBACK_SPIRIT_MAP = {
    "学": "light", "考": "light", "工作": "light", "会议": "light", "开会": "light",
    "报告": "light", "项目": "light", "复习": "light", "上课": "light", "作业": "light",
    "电影": "water", "游戏": "water", "休息": "water", "度假": "water", "追剧": "water",
    "跑步": "soil", "健身": "soil", "运动": "soil", "睡": "soil", "体检": "soil",
    "瑜伽": "soil", "游泳": "soil",
    "聚会": "air", "约会": "air", "朋友": "air", "拜访": "air", "社交": "air",
    "画画": "nutrition", "弹琴": "nutrition", "读书": "nutrition", "摄影": "nutrition",
    "写作": "nutrition", "书": "nutrition", "学做": "nutrition",
}


class TaskParser:
    """任务解析器"""

    async def parse(
        self,
        user_input: str,
        current_date: str = None,
        existing_tasks: list = None,
        user_id: str = None,
    ) -> dict:
        """
        解析用户输入 → 结构化任务列表。
        LLM 不可用时走规则兜底。
        """
        if not current_date:
            current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 优先从 task_parse.md 加载 prompt，fallback 到内联硬编码
        external = load_prompt("task_parse")
        if external:
            system = external.replace("{current_date}", current_date)
        else:
            system = PARSER_SYSTEM_PROMPT.format(current_date=current_date)
        user_prompt = f"用户输入：{user_input}"

        if existing_tasks:
            task_summaries = ", ".join([t.get("title", "") for t in existing_tasks[:10]])
            user_prompt += f"\n已有任务：{task_summaries}"

        result = await llm_client.complete_json(
            system=system,
            user=user_prompt,
            temperature=0.2,
            user_id=user_id,
            purpose="task_parse",
        )

        # 如果 LLM 返回有效数据
        if result and result.get("tasks"):
            return self._validate_result(result, user_input)

        # Fallback: 规则解析
        logger.info("task_parser_fallback", input=user_input[:100])
        return self._rule_based_parse(user_input, current_date)

    def _validate_result(self, result: dict, raw_input: str) -> dict:
        """校验并补全 LLM 返回的解析结果"""
        tasks = result.get("tasks", [])
        valid_spirits = {"light", "water", "soil", "air", "nutrition"}

        for task in tasks:
            # 保证必填字段
            task.setdefault("title", raw_input[:50])
            task.setdefault("raw_fragment", raw_input)
            task.setdefault("priority", "medium")
            task.setdefault("is_recurring", False)
            task.setdefault("needs_clarification", False)
            task.setdefault("estimated_hours", 1)
            task.setdefault("secondary_spirits", [])
            task.setdefault("extracted_entities", {})

            # 校验精灵代码
            if task.get("primary_spirit") not in valid_spirits:
                task["primary_spirit"] = "light"
            task["secondary_spirits"] = [
                s for s in task.get("secondary_spirits", []) if s in valid_spirits
            ]

            # 校验优先级
            if task.get("priority") not in ("high", "medium", "low"):
                task["priority"] = "medium"

            # 校验预估时长
            hours = task.get("estimated_hours", 1)
            if not isinstance(hours, (int, float)) or hours <= 0:
                task["estimated_hours"] = 1

        result["tasks"] = tasks
        result.setdefault("overall_confidence", 0.8)
        result.setdefault("suggestions", [])
        return result

    def _rule_based_parse(self, user_input: str, current_date: str) -> dict:
        """
        规则兜底解析 — LLM 不可用时的简单关键词匹配。
        不做时间解析，标记 needs_clarification。
        """
        # 简单分句
        segments = [s.strip() for s in user_input.replace("，", ",").replace("、", ",")
                     .replace("还", ",").replace("；", ",").split(",") if s.strip()]
        if len(segments) == 1:
            segments = [user_input.strip()]

        tasks = []
        for seg in segments:
            spirit = self._guess_spirit(seg)
            needs_clarification = True
            estimated_hours = 1

            # 简单优先级推断
            priority = "medium"
            for kw in ["紧急", "重要", "必须", "赶紧"]:
                if kw in seg:
                    priority = "high"
                    break
            for kw in ["有空", "闲时", "随便"]:
                if kw in seg:
                    priority = "low"
                    break

            tasks.append({
                "title": seg[:50],
                "raw_fragment": seg,
                "primary_spirit": spirit,
                "secondary_spirits": [],
                "deadline": None,
                "estimated_hours": estimated_hours,
                "priority": priority,
                "is_recurring": "每" in seg,
                "recurrence_pattern": self._guess_recurrence(seg),
                "needs_clarification": needs_clarification,
                "clarification_question": "这个任务大概需要多长时间？什么时候需要完成？",
                "extracted_entities": {"time": None, "location": None, "people": [], "tools": []},
            })

        return {
            "tasks": tasks,
            "overall_confidence": 0.4,
            "suggestions": ["建议提供更详细的时间信息以获得更精准的安排"],
        }

    @staticmethod
    def _guess_spirit(text: str) -> str:
        for keyword, spirit in FALLBACK_SPIRIT_MAP.items():
            if keyword in text:
                return spirit
        return "light"

    @staticmethod
    def _guess_recurrence(text: str) -> Optional[str]:
        if "每天" in text:
            return "daily"
        if "工作日" in text:
            return "weekdays"
        if "每周" in text:
            return "weekly"
        return None


# 全局实例
task_parser = TaskParser()