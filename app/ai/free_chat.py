"""
自由群聊引擎 — 头脑风暴模式（P1 重构版）

【设计理念】
之前版本的问题：每轮随机抽一个精灵独立生成 JSON，精灵之间不会真正对话。
现在改为显式状态机，让精灵真的相互响应、表达立场，最终收敛成一个共识任务。

【状态机】

[INIT]
  ↓ 选定 2-3 个精灵
  ↓ 短事务读 profile context

[PROPOSALS]   每个参与精灵独立提议（事项 + 大致时间 + 理由）
  ↓ 输出 N 条 spirit_message: type=proposal

[DISCUSSION]  每个精灵针对 *其他* 精灵的提议明确表态
  ↓ stance ∈ {support, oppose, blend}
  ↓ 输出 N 条 spirit_message: type=discussion

[SYNTHESIS]   主持人综合所有立场，给出最终任务建议
  ↓ 输出 1 条 orchestrator: type=synthesis
  ↓ 输出 1 条 task_suggestion（前端弹窗确认）

[CONSENSUS_CHECK]  各精灵对最终方案的最终表态
  ↓ 全员同意 → done
  ↓ 有人反对 → need_user_input

[DONE]

【与 P0 的兼容性】
  - 保留 session_factory 模式，SSE 期间不持有 DB 连接
  - 兼容旧的 `db_or_factory` 构造参数

【注意】
  task_suggestion 由 orchestrator 综合输出一次，不是各精灵零散输出。
  这样前端只需要处理一个明确的 task_suggestion 事件。
"""
import uuid
import json
import random
import asyncio
from typing import AsyncGenerator, Optional, Callable

import structlog

from app.ai.llm_client import llm_client
from app.ai.spirits import get_spirit, VALID_SPIRIT_CODES
from app.ai.context_builder import ContextBuilder, SPIRIT_NAMES, SPIRIT_EMOJIS
from app.utils.prompt_loader import load_prompt

logger = structlog.get_logger()

# 默认参与精灵数（2-3 之间最佳，多了对话太散）
DEFAULT_SPIRIT_COUNT = 3


class FreeChatEngine:
    """自由群聊引擎 — 头脑风暴模式"""

    def __init__(self, db_or_factory):
        if callable(db_or_factory) and not hasattr(db_or_factory, "execute"):
            self._session_factory: Optional[Callable] = db_or_factory
            self._db = None
        else:
            self._session_factory = None
            self._db = db_or_factory

    def _open_session(self):
        if self._session_factory is not None:
            return self._session_factory()
        engine = self

        class _NoCloseCtx:
            async def __aenter__(self_inner):
                return engine._db

            async def __aexit__(self_inner, *args):
                return False
        return _NoCloseCtx()

    # ====================================================================
    #  主流程
    # ====================================================================

    async def run(
        self,
        user_id: uuid.UUID,
        topic: str = None,
        spirit_codes: list[str] = None,
        stream: bool = True,
    ) -> AsyncGenerator:
        # 1. 选定参与精灵
        spirit_codes = self._select_spirits(spirit_codes)
        session_id = str(uuid.uuid4())

        # 2. 短事务：读 profile context
        profile_ctx = await self._build_profile_ctx_short(user_id)

        # 3. 生成话题
        current_topic = topic or await self._generate_topic(spirit_codes, profile_ctx)

        yield self._sse_event("topic_suggestion", {
            "session_id": session_id,
            "topic": current_topic,
            "spirits": [
                {"code": c, "name": SPIRIT_NAMES.get(c, c), "emoji": SPIRIT_EMOJIS.get(c, "")}
                for c in spirit_codes
            ],
            "phase": "init",
        })

        # ============== Phase 1: PROPOSALS ==============
        # 每个精灵独立提议
        proposals: list[dict] = []
        async for event in self._phase_proposals(
            session_id, spirit_codes, current_topic, profile_ctx
        ):
            proposals.append(event["_proposal"]) if "_proposal" in event else None
            if "_proposal" in event:
                # 仅 yield 标准 SSE，不暴露内部数据
                clean = {k: v for k, v in event.items() if not k.startswith("_")}
                yield self._sse_event("spirit_message", clean)
            else:
                yield self._sse_event("spirit_message", event)

        if not proposals:
            yield self._sse_event("error", {"message": "没有精灵给出提议"})
            yield self._sse_event("done", {"session_id": session_id})
            return

        # ============== Phase 2: DISCUSSION ==============
        # 每个精灵对其他人的提议表态
        discussions: list[dict] = []
        async for event in self._phase_discussion(
            session_id, spirit_codes, proposals, current_topic, profile_ctx
        ):
            if "_discussion" in event:
                discussions.append(event["_discussion"])
                clean = {k: v for k, v in event.items() if not k.startswith("_")}
                yield self._sse_event("spirit_message", clean)
            else:
                yield self._sse_event("spirit_message", event)

        # ============== Phase 3: SYNTHESIS ==============
        # 主持人综合 → 一个最终任务
        synthesis = await self._phase_synthesis(
            session_id, spirit_codes, proposals, discussions, current_topic, profile_ctx
        )
        yield self._sse_event("orchestrator", {
            "session_id": session_id,
            "speaker": "orchestrator",
            "speaker_name": "主持人",
            "speaker_emoji": "🎙️",
            "content": synthesis.get("summary", ""),
            "type": "synthesis",
            "phase": "synthesis",
        })

        # ============== Phase 4: CONSENSUS CHECK ==============
        # 各精灵对最终方案的最终表态
        final_task = synthesis.get("final_task")
        if not final_task or not final_task.get("title"):
            yield self._sse_event("done", {
                "session_id": session_id,
                "consensus": False,
                "reason": "未能形成有效任务建议",
            })
            return

        # 让每个精灵对 final_task 表态
        all_accept = True
        objections: list[dict] = []
        async for event in self._phase_consensus_check(
            session_id, spirit_codes, final_task, profile_ctx
        ):
            if "_consensus" in event:
                if not event["_consensus"]["accept"]:
                    all_accept = False
                    objections.append(event["_consensus"])
                clean = {k: v for k, v in event.items() if not k.startswith("_")}
                yield self._sse_event("spirit_message", clean)
            else:
                yield self._sse_event("spirit_message", event)

        # ============== 输出 task_suggestion 或 need_user_input ==============
        if all_accept:
            yield self._sse_event("task_suggestion", {
                "session_id": session_id,
                "consensus": True,
                "title": final_task.get("title", ""),
                "spirit": final_task.get("spirit", spirit_codes[0]),
                "date": final_task.get("date", ""),
                "time_start": final_task.get("time_start", ""),
                "time_end": final_task.get("time_end", ""),
                "duration_minutes": final_task.get("duration_minutes", 60),
                "priority": final_task.get("priority", "medium"),
                "rationale": synthesis.get("rationale", ""),
                "supporting_spirits": spirit_codes,
            })
        else:
            yield self._sse_event("need_user_input", {
                "session_id": session_id,
                "consensus": False,
                "message": "精灵们对方案有不同意见，请你来决定：",
                "proposed_task": final_task,
                "objections": objections,
                "options": [
                    {"label": "采纳主持人方案", "value": "accept_synthesis"},
                    {"label": "放弃此次讨论", "value": "abort"},
                ],
            })

        yield self._sse_event("done", {
            "session_id": session_id,
            "phases_completed": ["proposals", "discussion", "synthesis", "consensus_check"],
            "consensus_reached": all_accept,
        })

    # ====================================================================
    #  阶段实现
    # ====================================================================

    async def _phase_proposals(
        self,
        session_id: str,
        spirit_codes: list[str],
        topic: str,
        profile_ctx: str,
    ) -> AsyncGenerator[dict, None]:
        """Phase 1: 每个精灵独立提议"""
        for code in spirit_codes:
            spirit = get_spirit(code)
            proposal = await self._spirit_propose(spirit, topic, profile_ctx)

            yield {
                "session_id": session_id,
                "speaker": code,
                "speaker_name": spirit.name,
                "speaker_emoji": spirit.emoji,
                "content": proposal.get("message", ""),
                "type": "proposal",
                "phase": "proposals",
                "proposal": {
                    "title": proposal.get("title", ""),
                    "date": proposal.get("date", ""),
                    "time_start": proposal.get("time_start", ""),
                    "time_end": proposal.get("time_end", ""),
                    "duration_minutes": proposal.get("duration_minutes", 60),
                    "rationale": proposal.get("rationale", ""),
                },
                # 内部传递（不会被 SSE 输出）
                "_proposal": {
                    "spirit": code,
                    "spirit_name": spirit.name,
                    **proposal,
                },
            }
            await asyncio.sleep(0.2)

    async def _phase_discussion(
        self,
        session_id: str,
        spirit_codes: list[str],
        proposals: list[dict],
        topic: str,
        profile_ctx: str,
    ) -> AsyncGenerator[dict, None]:
        """Phase 2: 每个精灵对别人的提议表态"""
        for code in spirit_codes:
            spirit = get_spirit(code)
            # 找出别人的提议
            others = [p for p in proposals if p.get("spirit") != code]
            if not others:
                continue

            discussion = await self._spirit_discuss(
                spirit, others, topic, profile_ctx
            )

            # discussion 可能包含多个表态（针对每个 other proposal 一个）
            for stance in discussion.get("stances", []):
                yield {
                    "session_id": session_id,
                    "speaker": code,
                    "speaker_name": spirit.name,
                    "speaker_emoji": spirit.emoji,
                    "content": stance.get("message", ""),
                    "type": "discussion",
                    "phase": "discussion",
                    "stance": stance.get("stance", "support"),  # support/oppose/blend
                    "target_spirit": stance.get("target_spirit", ""),
                    "_discussion": {
                        "spirit": code,
                        "target_spirit": stance.get("target_spirit", ""),
                        "stance": stance.get("stance", ""),
                        "message": stance.get("message", ""),
                    },
                }
                await asyncio.sleep(0.2)

    async def _phase_synthesis(
        self,
        session_id: str,
        spirit_codes: list[str],
        proposals: list[dict],
        discussions: list[dict],
        topic: str,
        profile_ctx: str,
    ) -> dict:
        """Phase 3: 主持人综合所有意见，给出最终任务"""
        # 整理输入材料
        proposals_text = "\n".join(
            f"- {SPIRIT_NAMES.get(p['spirit'], p['spirit'])}提议「{p.get('title','')}」"
            f"（{p.get('date','')} {p.get('time_start','')}）"
            f"理由：{p.get('rationale','')}"
            for p in proposals
        )

        discussions_text = "\n".join(
            f"- {SPIRIT_NAMES.get(d['spirit'], d['spirit'])}对"
            f"{SPIRIT_NAMES.get(d['target_spirit'], d['target_spirit'])}的提议"
            f"表示「{d['stance']}」：{d['message']}"
            for d in discussions
        )

        # 优先用 prompts/orchestrator.md，否则内联
        external = load_prompt("orchestrator")
        base_prompt = external if external else """你是精灵日程系统的协商主持人。
你的角色是中立的调停者，帮助精灵们形成共识。"""

        system_prompt = f"""{base_prompt}

## 当前任务：综合各精灵的提议和讨论，形成 ONE 个最优任务建议

## 综合原则
1. 优先吸收"被多数精灵支持"的提议
2. 如果有"blend"立场，尝试融合两个提议（比如：先运动再聚餐）
3. 时间冲突时倾向于"早做的事先做"，给后续留 buffer
4. 任务要具体可执行：标题动词开头、有日期、有时段
5. 用户上下文：{profile_ctx[:300]}

## 输出格式
请输出 JSON：
{{
  "summary": "你对各方意见的简短总结 + 给出最终建议（80字以内）",
  "rationale": "为什么这是最优方案（50字以内）",
  "final_task": {{
    "title": "具体任务标题",
    "spirit": "主负责精灵代码",
    "date": "YYYY-MM-DD 或相对日期如'明天'",
    "time_start": "HH:MM",
    "time_end": "HH:MM",
    "duration_minutes": 60,
    "priority": "high/medium/low"
  }}
}}"""

        user_prompt = f"""话题：{topic}

【各精灵提议】
{proposals_text or '（无）'}

【精灵讨论】
{discussions_text or '（无明显异议）'}

请综合这些意见，输出最终任务建议。"""

        result = await llm_client.complete_json(
            system=system_prompt,
            user=user_prompt,
            purpose="brainstorm_synthesis",
        )

        if result and result.get("final_task"):
            return result

        # Fallback：取第一个提议
        if proposals:
            first = proposals[0]
            return {
                "summary": f"主持人综合后建议先采纳{SPIRIT_NAMES.get(first['spirit'])}的提议",
                "rationale": "保守起见取第一提议",
                "final_task": {
                    "title": first.get("title", ""),
                    "spirit": first.get("spirit", spirit_codes[0]),
                    "date": first.get("date", ""),
                    "time_start": first.get("time_start", ""),
                    "time_end": first.get("time_end", ""),
                    "duration_minutes": first.get("duration_minutes", 60),
                    "priority": "medium",
                },
            }

        return {"summary": "未能形成共识", "rationale": "", "final_task": None}

    async def _phase_consensus_check(
        self,
        session_id: str,
        spirit_codes: list[str],
        final_task: dict,
        profile_ctx: str,
    ) -> AsyncGenerator[dict, None]:
        """Phase 4: 各精灵对最终方案的最终表态"""
        task_text = (
            f"{final_task.get('title','')}（{final_task.get('date','')} "
            f"{final_task.get('time_start','')}-{final_task.get('time_end','')}）"
        )

        for code in spirit_codes:
            spirit = get_spirit(code)
            verdict = await self._spirit_consensus_vote(
                spirit, task_text, final_task, profile_ctx
            )

            yield {
                "session_id": session_id,
                "speaker": code,
                "speaker_name": spirit.name,
                "speaker_emoji": spirit.emoji,
                "content": verdict.get("message", ""),
                "type": "consensus_vote",
                "phase": "consensus_check",
                "accept": verdict.get("accept", True),
                "_consensus": {
                    "spirit": code,
                    "accept": verdict.get("accept", True),
                    "message": verdict.get("message", ""),
                    "objection": verdict.get("objection", ""),
                },
            }
            await asyncio.sleep(0.2)

    # ====================================================================
    #  单精灵 LLM 调用
    # ====================================================================

    async def _spirit_propose(self, spirit, topic: str, profile_ctx: str) -> dict:
        """让精灵针对话题提出一个具体建议"""
        spirit_prompt = load_prompt(spirit.code) or ""

        system_prompt = f"""你是 {spirit.emoji} {spirit.name}，负责{spirit._domain_desc()}。

{spirit_prompt}

## 当前场景：精灵头脑风暴
其他精灵在场，话题：「{topic}」

## 你的任务
基于你的领域立场，给用户提出一个具体可执行的任务建议。
不要空泛建议（如"多锻炼"），要有具体动作和大致时间。

## 用户上下文
{profile_ctx[:300]}

## 输出格式（JSON）
{{
  "message": "你的发言（不超过60字，符合你的人格）",
  "title": "任务标题（动词开头）",
  "date": "YYYY-MM-DD 或'明天'/'后天'/'周六'等",
  "time_start": "HH:MM",
  "time_end": "HH:MM",
  "duration_minutes": 60,
  "rationale": "为什么提议这个（30字内）"
}}"""

        result = await llm_client.complete_json(
            system=system_prompt,
            user=f"话题：{topic}\n\n请给出你的提议：",
            purpose=f"brainstorm_propose_{spirit.code}",
        )

        if result and result.get("title"):
            return result

        # Fallback
        return {
            "message": f"我建议针对「{topic}」做点具体的事情。",
            "title": f"{spirit.name}建议的任务",
            "date": "",
            "time_start": "",
            "time_end": "",
            "duration_minutes": 60,
            "rationale": "保持领域均衡",
        }

    async def _spirit_discuss(
        self, spirit, others_proposals: list[dict], topic: str, profile_ctx: str
    ) -> dict:
        """让精灵对其他精灵的提议表态"""
        spirit_prompt = load_prompt(spirit.code) or ""

        others_text = "\n".join(
            f"- {SPIRIT_NAMES.get(p['spirit'])}提议「{p.get('title','')}」"
            f"（{p.get('date','')} {p.get('time_start','')}-{p.get('time_end','')}）"
            f"，理由：{p.get('rationale','')}"
            for p in others_proposals
        )

        system_prompt = f"""你是 {spirit.emoji} {spirit.name}，负责{spirit._domain_desc()}。

{spirit_prompt}

## 当前场景
你正在和其他精灵讨论。其他精灵已提出他们的方案。
你需要从你的领域立场出发，对每个提议明确表态。

## 表态规则
- support：你支持这个提议（说明为什么对你的领域也有益）
- oppose：你反对（说明它对你的领域有什么负面影响）
- blend：你想融合（提出怎么把这个提议和你的领域结合）

请保持你的人格风格，发言要有立场不要和稀泥。

## 输出格式（JSON）
{{
  "stances": [
    {{
      "target_spirit": "被评论的精灵代码",
      "stance": "support/oppose/blend",
      "message": "你的表态（40字内，要有理由）"
    }}
  ]
}}"""

        user_prompt = f"""话题：{topic}

其他精灵的提议：
{others_text}

请逐一表态："""

        result = await llm_client.complete_json(
            system=system_prompt,
            user=user_prompt,
            purpose=f"brainstorm_discuss_{spirit.code}",
        )

        if result and result.get("stances"):
            return result

        # Fallback：默认全部 support
        return {
            "stances": [
                {
                    "target_spirit": p["spirit"],
                    "stance": "support",
                    "message": f"我也认同{SPIRIT_NAMES.get(p['spirit'])}的看法。",
                }
                for p in others_proposals[:1]  # 只对第一个表态，避免太啰嗦
            ]
        }

    async def _spirit_consensus_vote(
        self, spirit, task_text: str, final_task: dict, profile_ctx: str
    ) -> dict:
        """让精灵对最终方案投票"""
        spirit_prompt = load_prompt(spirit.code) or ""

        system_prompt = f"""你是 {spirit.emoji} {spirit.name}，负责{spirit._domain_desc()}。

{spirit_prompt}

## 当前场景：最终表态
经过讨论，主持人综合出一个方案，你需要给出最终意见。

## 输出格式（JSON）
{{
  "accept": true,
  "message": "你的表态（30字内，要有人格）",
  "objection": "如果反对，简述原因；接受则留空"
}}"""

        user_prompt = f"""最终方案：{task_text}

负责精灵：{SPIRIT_NAMES.get(final_task.get('spirit',''),'?')}

请投票（默认接受，除非明显违背你的领域原则）："""

        result = await llm_client.complete_json(
            system=system_prompt,
            user=user_prompt,
            purpose=f"brainstorm_vote_{spirit.code}",
        )

        if result is not None:
            result.setdefault("accept", True)
            result.setdefault("message", "我同意。")
            result.setdefault("objection", "")
            return result

        return {"accept": True, "message": "我同意这个安排。", "objection": ""}

    # ====================================================================
    #  辅助
    # ====================================================================

    def _select_spirits(self, spirit_codes: Optional[list[str]]) -> list[str]:
        """选定 2-3 个参与精灵"""
        if spirit_codes:
            valid = [c for c in spirit_codes if c in VALID_SPIRIT_CODES]
            if len(valid) >= 2:
                return valid[:DEFAULT_SPIRIT_COUNT]
        # 随机选 3 个
        return random.sample(VALID_SPIRIT_CODES, min(DEFAULT_SPIRIT_COUNT, len(VALID_SPIRIT_CODES)))

    async def _build_profile_ctx_short(self, user_id: uuid.UUID) -> str:
        async with self._open_session() as db:
            ctx_builder = ContextBuilder(db)
            return await ctx_builder.build_profile_context(user_id)

    async def _generate_topic(self, spirit_codes: list[str], profile_ctx: str) -> str:
        spirit_names = "、".join(SPIRIT_NAMES.get(c, c) for c in spirit_codes)

        system_prompt = """你是精灵日程系统的话题策划者。
为参与的精灵生成一个能引发讨论的日常话题。
话题要：贴近生活、能让不同领域的精灵从各自角度发表看法、有具体决策点。
只返回话题文本，不要其他内容。"""

        user_prompt = f"""参与精灵：{spirit_names}
用户上下文：{profile_ctx[:200]}

生成话题（20字以内，要有具体决策点而非空泛讨论）："""

        result = await llm_client.complete(
            system=system_prompt,
            user=user_prompt,
            max_tokens=50,
            purpose="brainstorm_topic",
        )

        fallback = [
            "本周末怎么安排最舒服？",
            "今晚想做点什么放松？",
            "下班后留一个小时该干嘛？",
            "周日早上排什么活动？",
        ]

        if not result or result.startswith("[FALLBACK]"):
            return random.choice(fallback)
        return result.strip().strip('"').strip("「").strip("」")

    def _sse_event(self, event_type: str, data: dict) -> str:
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"