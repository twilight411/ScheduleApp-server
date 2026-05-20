"""
精灵基类 — 所有精灵的公共接口和通用逻辑

Sprint A: 支持从 .md 文件按强度档位加载 Prompt
Sprint B: 修正 LLM 调用方法名、对齐协商接口签名
"""
import uuid
from abc import ABC, abstractmethod
from typing import Optional

import structlog

from app.ai.llm_client import llm_client
from app.utils.prompt_loader import load_spirit_prompt, intensity_to_level

logger = structlog.get_logger()


class BaseSpirit(ABC):
    """精灵 Agent 基类"""

    code: str = ""
    name: str = ""
    emoji: str = ""
    personality: str = ""
    speaking_style: str = ""
    domain_keywords: list[str] = []
    decision_principles: list[str] = []
    negotiation_style: str = ""
    catchphrases: list[str] = []

    # ========================================
    #  Prompt 构建
    # ========================================

    def _build_system_prompt(self, context: str = "", intensity: int = 50) -> str:
        """
        构建精灵的 System Prompt。

        优先从 prompts/spirits/{code}.md 加载对应强度档位的 Prompt。
        如果文件不存在或为空，fallback 到内置的简短人格描述。
        """
        spirit_prompt = load_spirit_prompt(self.code, intensity)

        if spirit_prompt:
            return f"""{spirit_prompt}

{context}""".strip()

        return self._build_fallback_prompt(context, intensity)

    def _build_fallback_prompt(self, context: str = "", intensity: int = 50) -> str:
        """内置的简短人格描述（当 .md 文件未配置时使用）"""
        principles = "\n".join(f"- {p}" for p in self.decision_principles)
        phrases = "、".join(self.catchphrases[:3])
        level = intensity_to_level(intensity)
        level_desc = {"low": "宽松模式", "mid": "日常模式", "high": "严格模式"}

        return f"""你是{self.emoji} {self.name}，精灵日程系统中负责用户{self._domain_desc()}的精灵。

## 性格
{self.personality}

## 说话风格
{self.speaking_style}
常用口头禅：{phrases}

## 当前强度档位
{level_desc.get(level, "日常模式")}（强度值: {intensity}/100）

## 核心原则
{principles}

## 协商风格
{self.negotiation_style}

{context}

重要：保持角色一致性，用你的人格说话。回复要简洁有力，不要超过100字。"""

    @abstractmethod
    def _domain_desc(self) -> str:
        """领域描述"""
        ...

    # ========================================
    #  任务拆解
    # ========================================

    async def decompose_task(self, task: dict, user_profile: dict) -> dict:
        """将任务拆解为子任务列表。"""
        intensity = user_profile.get("intensity", 50)
        peak = user_profile.get("peak_hours", ["09:00-11:00"])
        max_work = user_profile.get("max_continuous_work_minutes", 120)
        chunk_style = user_profile.get("chunk_style", "balanced")

        chunk_desc = {
            "ant": f"每个子任务 30-60 分钟（用户偏好碎片化执行）",
            "balanced": f"每个子任务 60-{max_work} 分钟（用户偏好阶段化执行）",
            "sprint": f"尽量合并为大块 120-{max_work} 分钟（用户偏好集中突破）",
        }

        system = self._build_system_prompt(f"""## 你的任务
将下面的任务拆解为可执行的子任务列表。

## 拆解规则
- {chunk_desc.get(chunk_style, chunk_desc["balanced"])}
- 标注子任务间的依赖关系
- 预留 buffer 时间（总估时×1.2）
- 用户高效时段: {peak}
- 给每个子任务一条精灵提示（tips），用你的人格语气

请只输出 JSON：
{{
  "subtasks": [
    {{
      "title": "子任务标题",
      "duration_minutes": 60,
      "suggested_time": "morning",
      "dependencies": [],
      "priority": "medium",
      "tips": "精灵提示"
    }}
  ],
  "spirit_comment": "一句话总评"
}}""", intensity=intensity)

        user_prompt = f"""任务信息：
标题: {task.get('title', '')}
预计时长: {task.get('estimated_hours', 1)}小时
截止时间: {task.get('deadline', '无')}
优先级: {task.get('priority', 'medium')}"""

        result = await llm_client.complete_json(
            system=system,
            user=user_prompt,
            purpose=f"decompose_{self.code}",
        )

        if result and result.get("subtasks"):
            return self._validate_decompose(result, task)

        return self._fallback_decompose(task)

    def _validate_decompose(self, result: dict, task: dict) -> dict:
        """校验拆解结果"""
        for st in result.get("subtasks", []):
            st.setdefault("title", task.get("title", "子任务"))
            st.setdefault("duration_minutes", 60)
            st.setdefault("suggested_time", "morning")
            st.setdefault("dependencies", [])
            st.setdefault("priority", task.get("priority", "medium"))
            st.setdefault("tips", "")

            dur = st["duration_minutes"]
            if not isinstance(dur, (int, float)) or dur < 15:
                st["duration_minutes"] = 30
            elif dur > 180:
                st["duration_minutes"] = 120

        result.setdefault("spirit_comment", f"{self.name}已为你拆解好任务！")
        return result

    def _fallback_decompose(self, task: dict) -> dict:
        """LLM 不可用时的简单拆解"""
        hours = task.get("estimated_hours", 2)
        total_minutes = int(hours * 60 * 1.2)
        chunk_size = 60
        num_chunks = max(1, total_minutes // chunk_size)

        subtasks = []
        for i in range(num_chunks):
            subtasks.append({
                "title": f"{task.get('title', '任务')} - 第{i+1}部分",
                "duration_minutes": min(chunk_size, total_minutes - i * chunk_size),
                "suggested_time": "morning" if i < num_chunks // 2 else "afternoon",
                "dependencies": [i - 1] if i > 0 else [],
                "priority": task.get("priority", "medium"),
                "tips": self.catchphrases[i % len(self.catchphrases)] if self.catchphrases else "",
            })

        return {
            "subtasks": subtasks,
            "spirit_comment": f"{self.name}为你做了基础拆解，有{num_chunks}个步骤。",
        }

    # ========================================
    #  对话
    # ========================================

    async def chat(
        self,
        message: str,
        history: list = None,
        session_id: str = None,
        user_profile: dict = None,
    ) -> dict:
        """
        与用户对话。

        user_profile 中需要包含 "intensity" 字段（0-100），
        精灵会根据强度值加载对应档位的 Prompt。
        """
        user_profile = user_profile or {}
        intensity = user_profile.get("intensity", 50)

        # 构建附加上下文
        context_parts = []
        tags = user_profile.get("user_tags", [])
        if tags:
            context_parts.append(f"用户特征标签: {', '.join(tags)}")

        annual = user_profile.get("annual_keyword", "")
        if annual:
            kw_map = {
                "breakthrough": "突破（专注事业/学业）",
                "repair": "修复（关注健康/心理）",
                "explore": "探索（尝试兴趣/社交）",
                "stable": "稳定（维系现状）",
            }
            context_parts.append(f"用户年度关键词: {kw_map.get(annual, annual)}")

        extra_context = "\n".join(context_parts)

        system = self._build_system_prompt(f"""## 对话模式
你正在和用户聊关于{self._domain_desc()}的话题。
请根据对话内容给出建议、反馈或规划。
如果对话中包含了一个明确可执行的任务安排（有做什么+大致时间+执行意愿），
请在 task_suggestion 中返回。否则 detected 设为 false。

{extra_context}

请输出 JSON：
{{
  "message": "你的回复",
  "task_suggestion": {{
    "detected": false,
    "title": "",
    "date": "",
    "time_start": "",
    "time_end": "",
    "duration_minutes": 0,
    "confidence": 0
  }}
}}""", intensity=intensity)

        # 构建对话历史
        messages_text = ""
        if history:
            for msg in history[-6:]:
                role = "用户" if msg.get("role") == "user" else self.name
                messages_text += f"{role}: {msg.get('content', '')}\n"

        user_prompt = f"{messages_text}用户: {message}"

        result = await llm_client.complete_json(
            system=system,
            user=user_prompt,
            purpose=f"chat_{self.code}",
        )

        if result:
            result.setdefault("message", "")
            result.setdefault("task_suggestion", {"detected": False})
            return result

        return {
            "message": f"抱歉，{self.name}暂时无法回复，请稍后再试。",
            "task_suggestion": {"detected": False},
        }

    # ========================================
    #  协商 — make_claim
    #  签名与 negotiation.py 的调用一致:
    #    spirit.make_claim(tasks=tasks, constraints={...})
    # ========================================

    async def make_claim(self, tasks: list, constraints: dict) -> dict:
        """在协商中声明时间诉求"""
        system = self._build_system_prompt(f"""## 协商模式
你正在精灵协商会议上为你负责的任务争取时间。
请声明你需要的时间段，说明理由，并表示是否愿意妥协。

{constraints.get('context', '')}

请输出 JSON：
{{
  "message": "你的发言（要有角色特色，不超过80字）",
  "stance": "坚持/灵活",
  "proposed_slots": [
    {{"task": "任务名", "time": "时间段", "priority": "high/medium/low", "flexible": true}}
  ],
  "compromise_willing": true,
  "compromise_condition": "妥协条件"
}}""", intensity=80)

        task_desc = "\n".join([
            f"- {t.get('title')}: 需要{t.get('duration_minutes', 60)}分钟, 优先级{t.get('priority', 'medium')}"
            for t in tasks
        ])
        user_prompt = f"你负责的任务：\n{task_desc}\n日期范围：{constraints.get('date_range', '本周')}"

        result = await llm_client.complete_json(
            system=system,
            user=user_prompt,
            purpose=f"claim_{self.code}",
        )

        if result and result.get("message"):
            return result

        return {
            "message": f"我需要时间来完成这些任务！",
            "stance": "灵活",
            "proposed_slots": [],
            "compromise_willing": True,
            "compromise_condition": "",
        }

    # ========================================
    #  协商 — respond_to_mediation
    #  签名与 negotiation.py 的调用一致:
    #    spirit.respond_to_mediation(mediation=dict, own_claims=list, conflicts=list)
    # ========================================

    async def respond_to_mediation(
        self, mediation: dict, own_claims: list, conflicts: list
    ) -> dict:
        """回应主持人的协调方案"""
        system = self._build_system_prompt(f"""## 协商回应模式
主持人提出了协调方案，请以你的角色回应。
如果方案对你有利，表示接受；如果不利，提出修改建议。

输出 JSON：
{{
  "message": "你的回应（不超过60字）",
  "stance": "accept/counter/insist",
  "adjusted_slots": []
}}""", intensity=80)

        conflicts_text = "\n".join(conflicts[:3]) if conflicts else "无直接冲突"
        user_prompt = (
            f"主持人说：{mediation.get('content', '')}\n"
            f"你的原诉求：{own_claims}\n"
            f"冲突：{conflicts_text}"
        )

        result = await llm_client.complete_json(
            system=system,
            user=user_prompt,
            purpose=f"respond_{self.code}",
        )
        return result or {
            "message": "好的，我接受这个方案。",
            "stance": "accept",
            "adjusted_slots": [],
        }

    # ========================================
    #  周评点评
    # ========================================

    async def generate_comment(self, score: float, details: dict) -> str:
        """生成周打分点评"""
        system = self._build_system_prompt(f"""## 周评模式
根据本周得分给出一句简短点评（不超过40字），用你的人格语气。""")

        user_prompt = f"得分：{score}/100\n详情：{details}"
        result = await llm_client.complete(
            system=system,
            user=user_prompt,
            max_tokens=100,
            purpose=f"comment_{self.code}",
        )
        if result.startswith("[FALLBACK]"):
            if score >= 80:
                return f"表现不错！{self.catchphrases[0] if self.catchphrases else ''}"
            elif score >= 50:
                return "还可以，下周继续努力！"
            else:
                return "这周需要更多关注哦。"
        return result.strip().strip('"')
