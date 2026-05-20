"""
对话自动识别任务 (Chat-to-Task)

[P0 修复 v2]
  - _parse_fuzzy_date 修复"下周X"被双重 +7 的 bug；引入"以本周一为基准"的稳定算法
  - _calculate_confidence: 仅当无正向意图时才扣负向意图分（之前两者叠加错杀）
  - validate_llm_suggestion 接收 history_text，从对话历史中补全日期/时间
  - 当 LLM 自报 confidence ≥ 0.7 且有标题时，不被规则否决（只补全字段）
  - 阈值从 0.30 降至 0.25，扩大召回
"""
import re
from datetime import date, datetime, timedelta
from typing import Optional

import structlog

from app.ai.llm_client import llm_client
from app.utils.prompt_loader import load_prompt

logger = structlog.get_logger()


# ===== 星期映射 =====
WEEKDAY_MAP = {
    "周一": 0, "星期一": 0, "礼拜一": 0,
    "周二": 1, "星期二": 1, "礼拜二": 1,
    "周三": 2, "星期三": 2, "礼拜三": 2,
    "周四": 3, "星期四": 3, "礼拜四": 3,
    "周五": 4, "星期五": 4, "礼拜五": 4,
    "周六": 5, "星期六": 5, "礼拜六": 5,
    "周日": 6, "星期日": 6, "星期天": 6, "礼拜天": 6, "礼拜日": 6,
}

RELATIVE_DATE_MAP = {
    "今天": 0, "今日": 0,
    "明天": 1, "明日": 1,
    "后天": 2, "后日": 2,
    "大后天": 3,
}

TIME_PERIOD_MAP = {
    "早上": ("07:00", "09:00"),
    "上午": ("09:00", "11:30"),
    "中午": ("11:30", "13:00"),
    "下午": ("14:00", "17:00"),
    "傍晚": ("17:00", "19:00"),
    "晚上": ("19:00", "21:30"),
    "深夜": ("22:00", "23:59"),
}

SPIRIT_KEYWORDS = {
    "light": [
        "工作", "学习", "考试", "会议", "报告", "项目", "代码", "编程",
        "作业", "论文", "培训", "面试", "复习", "备考", "PPT", "文档",
        "需求", "开发", "上班", "加班", "deadline", "答辩",
    ],
    "water": [
        "电影", "游戏", "追剧", "旅游", "逛街", "购物", "休息", "放松",
        "音乐", "综艺", "聚餐", "玩", "娱乐", "看书", "小说", "度假",
    ],
    "soil": [
        "跑步", "运动", "健身", "游泳", "瑜伽", "拉伸", "散步", "体检",
        "锻炼", "篮球", "足球", "羽毛球", "爬山", "骑车", "早睡",
        "减肥", "饮食", "睡眠", "冥想", "打球",
    ],
    "air": [
        "聚会", "约会", "见面", "社交", "朋友", "同事", "家人", "回家",
        "拜访", "电话", "视频通话", "团建", "生日", "婚礼", "送礼",
        "请客", "饭局", "约了",
    ],
    "nutrition": [
        "画画", "弹琴", "练琴", "吉他", "摄影", "手工", "烘焙",
        "写作", "博客", "副业", "课程", "兴趣", "创作", "设计",
        "读书", "心理学", "哲学", "理财",
    ],
}

# ===== 执行意愿关键词（扩展） =====
INTENT_POSITIVE = [
    "要", "想", "打算", "准备", "计划", "安排", "得", "必须",
    "需要", "应该", "别忘了", "记得", "提醒我", "帮我安排",
    "约了", "定了", "报名了", "预约了", "去", "做",
]

INTENT_NEGATIVE = [
    "如果", "假如", "要是", "万一", "可能", "也许", "大概",
    "不知道", "考虑", "看看", "想想", "犹豫", "还没决定",
]

# ===== 置信度阈值 =====
CONFIDENCE_THRESHOLD = 0.25
LLM_TRUST_THRESHOLD = 0.70  # LLM 自报置信度高于此值时，规则不否决


class ChatToTaskDetector:
    """对话任务检测器"""

    # ========================================
    #  模式 1: 校验 LLM 返回的 task_suggestion
    # ========================================

    def validate_llm_suggestion(
        self,
        suggestion: dict,
        spirit_code: str,
        user_message: str,
        history_text: str = "",
    ) -> dict:
        """
        校验并补全 LLM 在对话中返回的 task_suggestion。

        [P0 修复] 接收 history_text 用于补全日期/时间——LLM 看的是整段对话，
        但当前消息可能不含日期（如：上一句说"明天"，本句说"上午去图书馆"）。
        """
        if not suggestion.get("detected"):
            return {"detected": False}

        title = (suggestion.get("title") or "").strip()
        if not title:
            title = self._extract_title_from_text(user_message)
            if not title:
                return {"detected": False}

        combined_text = f"{user_message} {history_text}".strip()

        # 解析日期：优先用 LLM 给的，缺失或无效则从消息+历史回溯
        raw_date = suggestion.get("date", "")
        parsed_date = self._parse_fuzzy_date(raw_date) or self._extract_date_from_text(combined_text)

        # 时间标准化
        time_start = self._normalize_time(suggestion.get("time_start", ""))
        time_end = self._normalize_time(suggestion.get("time_end", ""))
        duration = suggestion.get("duration_minutes")

        if not time_start:
            time_start, time_end = self._extract_time_from_text(combined_text)

        if time_start and time_end and not duration:
            duration = self._calc_duration(time_start, time_end)
        elif time_start and duration and not time_end:
            time_end = self._calc_end_time(time_start, duration)
        elif not duration:
            duration = 60

        # 推断精灵
        detected_spirit = self._infer_spirit(title + " " + combined_text)
        final_spirit = detected_spirit or spirit_code

        llm_confidence = float(suggestion.get("confidence", 0) or 0)

        # ===== P0 修复：高置信度 LLM 信号直接放行 =====
        if llm_confidence >= LLM_TRUST_THRESHOLD and title:
            return {
                "detected": True,
                "title": title,
                "spirit": final_spirit,
                "date": parsed_date.isoformat() if parsed_date else None,
                "time_start": time_start,
                "time_end": time_end,
                "duration_minutes": duration,
                "confidence": round(llm_confidence, 2),
                "source_quote": user_message[:200],
            }

        # ===== 否则走规则置信度 =====
        confidence = self._calculate_confidence(
            has_title=bool(title),
            has_date=parsed_date is not None,
            has_time=bool(time_start),
            has_intent=self._has_positive_intent(combined_text),
            has_negative_intent=self._has_negative_intent(combined_text),
            llm_confidence=llm_confidence,
        )

        if confidence < CONFIDENCE_THRESHOLD:
            return {"detected": False}

        return {
            "detected": True,
            "title": title,
            "spirit": final_spirit,
            "date": parsed_date.isoformat() if parsed_date else None,
            "time_start": time_start,
            "time_end": time_end,
            "duration_minutes": duration,
            "confidence": round(confidence, 2),
            "source_quote": user_message[:200],
        }

    # ========================================
    #  模式 2: 独立分析单条消息
    # ========================================

    async def detect_from_message(
        self,
        message: str,
        spirit_code: str,
        user_profile: dict = None,
    ) -> dict:
        external = load_prompt("chat_to_task")
        if external:
            system_prompt = external
        else:
            system_prompt = """你是一个任务检测助手。分析用户消息，判断是否包含一个明确的、可执行的任务安排。

判断标准 — 必须同时满足：
1. 有明确的事项（做什么）
2. 有大致的时间（什么时候）
3. 有执行意愿（用户主动提出，而非假设性讨论）

如果检测到任务，输出 JSON：
{
  "detected": true,
  "title": "任务标题（简洁、动词开头）",
  "date": "YYYY-MM-DD 或相对日期如 '明天'",
  "time_start": "HH:MM 或空",
  "time_end": "HH:MM 或空",
  "duration_minutes": 60,
  "confidence": 0.85,
  "source_quote": "触发识别的原文片段"
}

如果没有检测到任务：
{"detected": false}

注意：
- "我要准备考试" → 不算（没有具体时间）
- "明天下午我要去图书馆复习" → 算（有事项+时间+意愿）
- "如果有空的话周末想跑步" → 不算（意愿不明确）
- "周六约了朋友吃饭" → 算（有事项+时间+已确定）"""

        result = await llm_client.complete_json(
            system=system_prompt,
            user=message,
            purpose="chat_to_task_detect",
        )

        if not result or not result.get("detected"):
            return {"detected": False}

        return self.validate_llm_suggestion(result, spirit_code, message)

    # ========================================
    #  模式 3: 回顾对话历史
    # ========================================

    async def detect_from_history(
        self,
        messages: list[dict],
        spirit_code: str,
    ) -> list[dict]:
        if not messages:
            return []

        user_messages = [
            m.get("content", "")
            for m in messages
            if m.get("role") == "user" and m.get("content")
        ]
        if not user_messages:
            return []

        combined = "\n".join(user_messages[-10:])

        system_prompt = """你是一个任务检测助手。分析以下对话中用户说的全部内容，
识别所有明确的、可执行的任务安排。

每个任务必须同时满足：有明确事项 + 有大致时间 + 有执行意愿。

输出 JSON 数组:
{
  "tasks": [
    {
      "title": "任务标题",
      "date": "日期",
      "time_start": "HH:MM 或空",
      "time_end": "HH:MM 或空",
      "duration_minutes": 60,
      "confidence": 0.85,
      "source_quote": "原文片段"
    }
  ]
}

没有检测到任何任务则返回：{"tasks": []}"""

        result = await llm_client.complete_json(
            system=system_prompt,
            user=f"用户在与{spirit_code}精灵对话中说了：\n{combined}",
            purpose="chat_to_task_history",
        )

        if not result or not result.get("tasks"):
            return []

        validated = []
        for task in result["tasks"]:
            task["detected"] = True
            processed = self.validate_llm_suggestion(
                task, spirit_code, task.get("source_quote", ""), history_text=combined
            )
            if processed.get("detected"):
                validated.append(processed)

        return validated

    # ========================================
    #  中文日期模糊解析（P0 重写）
    # ========================================

    def _parse_fuzzy_date(self, text: str) -> Optional[date]:
        """
        解析各种中文日期表达。

        [P0 修复] 周X 解析改用"本周一为基准"算法，
        消除原版"下" 关键字双重 +7 的 bug。
        """
        if not text:
            return None

        text = text.strip()
        today = date.today()

        # 1. 标准格式 YYYY-MM-DD
        iso_match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if iso_match:
            try:
                return date(
                    int(iso_match.group(1)),
                    int(iso_match.group(2)),
                    int(iso_match.group(3)),
                )
            except ValueError:
                pass

        # 2. 中文格式 X月X日 / X月X号
        cn_match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
        if cn_match:
            month, day = int(cn_match.group(1)), int(cn_match.group(2))
            year = today.year
            try:
                target = date(year, month, day)
                if target < today:
                    target = date(year + 1, month, day)
                return target
            except ValueError:
                pass

        # 3. 相对日期
        for keyword, delta in RELATIVE_DATE_MAP.items():
            if keyword in text:
                return today + timedelta(days=delta)

        # 4. 周X：基于"本周一"的稳定算法
        for keyword, weekday in WEEKDAY_MAP.items():
            if keyword in text:
                # 判断"这周/下周/下下周"
                idx = text.find(keyword)
                prefix = text[:idx]

                is_next_next = ("下下周" in text) or ("下下" in prefix)
                is_next_week = (not is_next_next) and (
                    "下周" in text or "下星期" in text or "下礼拜" in text
                    or (prefix.endswith("下"))
                )
                is_this_week = (
                    "这周" in text or "本周" in text or "这星期" in text
                    or "这礼拜" in text
                )

                # 本周一日期
                this_monday = today - timedelta(days=today.weekday())
                target = this_monday + timedelta(days=weekday)

                if is_next_next:
                    target += timedelta(days=14)
                elif is_next_week:
                    target += timedelta(days=7)
                elif is_this_week:
                    pass  # 本周该日（即便已过去也指本周）
                else:
                    # 默认："最近的下一个该星期几"
                    if target < today:
                        target += timedelta(days=7)

                return target

        # 5. "下周" 不带具体星期 → 下周一
        if "下周" in text:
            this_monday = today - timedelta(days=today.weekday())
            return this_monday + timedelta(days=7)

        # 6. "周末"
        if "周末" in text:
            this_saturday = today + timedelta(days=(5 - today.weekday()) % 7)
            if this_saturday < today:
                this_saturday += timedelta(days=7)
            if "下" in text[:text.find("周末")]:
                this_saturday += timedelta(days=7)
            return this_saturday

        return None

    def _extract_date_from_text(self, text: str) -> Optional[date]:
        """从自由文本中提取日期"""
        # 直接尝试 _parse_fuzzy_date 处理整段文本
        return self._parse_fuzzy_date(text)

    # ========================================
    #  时间解析
    # ========================================

    def _normalize_time(self, time_str: str) -> str:
        if not time_str:
            return ""

        time_str = time_str.strip()

        if re.match(r"^\d{1,2}:\d{2}$", time_str):
            h, m = time_str.split(":")
            return f"{int(h):02d}:{int(m):02d}"

        match = re.search(r"(\d{1,2})\s*[点时：:]\s*(\d{1,2})?", time_str)
        if match:
            h = int(match.group(1))
            m = int(match.group(2)) if match.group(2) else 0
            if "下午" in time_str or "晚上" in time_str or "晚" in time_str:
                if h < 12:
                    h += 12
            return f"{h:02d}:{m:02d}"

        return ""

    def _extract_time_from_text(self, text: str) -> tuple[str, str]:
        times = re.findall(
            r"(\d{1,2})\s*[点时：:]\s*(\d{1,2})?(?:\s*分)?",
            text,
        )
        if len(times) >= 2:
            h1, m1 = int(times[0][0]), int(times[0][1]) if times[0][1] else 0
            h2, m2 = int(times[1][0]), int(times[1][1]) if times[1][1] else 0
            return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"
        elif len(times) == 1:
            h, m = int(times[0][0]), int(times[0][1]) if times[0][1] else 0
            if "下午" in text or "晚上" in text:
                if h < 12:
                    h += 12
            return f"{h:02d}:{m:02d}", ""

        for period, (start, end) in TIME_PERIOD_MAP.items():
            if period in text:
                return start, end

        return "", ""

    # ========================================
    #  辅助
    # ========================================

    def _extract_title_from_text(self, text: str) -> str:
        patterns = [
            r"(?:要|想|打算|准备|计划|得|需要|去|约了)(.{2,20}?)(?:[，。！？,\.!?]|$)",
            r"(?:帮我|记得|别忘了|提醒我)(.{2,20}?)(?:[，。！？,\.!?]|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                title = match.group(1).strip()
                if len(title) >= 2:
                    return title
        return ""

    def _infer_spirit(self, text: str) -> Optional[str]:
        scores = {}
        for spirit, keywords in SPIRIT_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                scores[spirit] = score
        if not scores:
            return None
        return max(scores, key=scores.get)

    def _has_positive_intent(self, text: str) -> bool:
        return any(kw in text for kw in INTENT_POSITIVE)

    def _has_negative_intent(self, text: str) -> bool:
        return any(kw in text for kw in INTENT_NEGATIVE)

    def _calculate_confidence(
        self,
        has_title: bool,
        has_date: bool,
        has_time: bool,
        has_intent: bool,
        has_negative_intent: bool,
        llm_confidence: float = 0,
    ) -> float:
        """
        综合置信度计算。

        [P0 修复] 仅当无正向意图时才扣负向意图分；
        否则正面表达的"想去"会被同时存在的"如果"误杀。
        """
        score = 0.0

        if has_title:
            score += 0.30
        if has_date:
            score += 0.20
        if has_time:
            score += 0.10

        if has_intent:
            score += 0.20
        elif has_negative_intent:
            # 仅在没有正向意图的纯犹豫表达里扣分
            score -= 0.20

        if llm_confidence > 0:
            score += min(0.20, llm_confidence * 0.20)

        return max(0.0, min(1.0, score))

    @staticmethod
    def _calc_duration(start: str, end: str) -> int:
        try:
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            diff = (eh * 60 + em) - (sh * 60 + sm)
            return max(15, diff) if diff > 0 else 60
        except (ValueError, AttributeError):
            return 60

    @staticmethod
    def _calc_end_time(start: str, duration: int) -> str:
        try:
            sh, sm = map(int, start.split(":"))
            total = sh * 60 + sm + duration
            eh, em = divmod(total, 60)
            eh = min(eh, 23)
            return f"{eh:02d}:{em:02d}"
        except (ValueError, AttributeError):
            return ""


chat_to_task_detector = ChatToTaskDetector()