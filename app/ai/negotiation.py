"""
协商引擎 (Module 4) — 多精灵冲突协商状态机

[P0 修复 v2]
  - 改用 session_factory 而非长期持有的 session（SSE 期间不再霸占 SQLite 写锁）
  - 读取上下文 / 加载任务 / 保存会话 各自开启短事务
  - LLM 对话期间完全不持有数据库连接
  - resolve_by_user 仍用单 session（非流式）

状态机:
  初始化 → 收集诉求 → 检测冲突 → 主持协商 → 精灵回应 → 评估共识
                                    ↑                        ↓
                                    └──── 未达成 + 轮次<3 ───┘
                                                             ↓ 超过3轮
                                                        提交用户决策
"""
import uuid
import json
from datetime import date, datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional, Callable

import structlog

from app.ai.llm_client import llm_client
from app.ai.spirits import get_spirit, VALID_SPIRIT_CODES
from app.ai.context_builder import ContextBuilder, SPIRIT_NAMES, SPIRIT_EMOJIS
from app.utils.prompt_loader import load_prompt

logger = structlog.get_logger()

MAX_ROUNDS = 3


# ====================================================================
#  数据结构
# ====================================================================

class NegotiationState(str, Enum):
    INIT = "init"
    COLLECTING_CLAIMS = "collecting_claims"
    DETECTING_CONFLICTS = "detecting_conflicts"
    MEDIATING = "mediating"
    AWAITING_RESPONSES = "awaiting_responses"
    EVALUATING = "evaluating"
    CONSENSUS_REACHED = "consensus_reached"
    NEED_USER_INPUT = "need_user_input"
    RESOLVED = "resolved"
    FAILED = "failed"


@dataclass
class SSEEvent:
    event: str
    data: dict

    def to_sse(self) -> str:
        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"


@dataclass
class SpiritClaim:
    spirit_code: str
    message: str
    stance: str
    proposed_slots: list
    compromise_willing: bool
    compromise_condition: str = ""


@dataclass
class SpiritResponse:
    spirit_code: str
    message: str
    stance: str
    adjusted_slots: list


@dataclass
class NegotiationRound:
    round_number: int
    claims: list[SpiritClaim] = field(default_factory=list)
    mediation: dict = field(default_factory=dict)
    responses: list[SpiritResponse] = field(default_factory=list)
    consensus: bool = False


@dataclass
class NegotiationSession:
    id: str
    user_id: str
    task_ids: list[str]
    date_range: tuple[date, date]
    state: NegotiationState = NegotiationState.INIT
    current_round: int = 0
    rounds: list[NegotiationRound] = field(default_factory=list)
    involved_spirits: list[str] = field(default_factory=list)
    final_schedule: list = field(default_factory=list)
    options: list = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ====================================================================
#  协商引擎 — 改造为 session_factory 模式
# ====================================================================

class NegotiationEngine:
    """
    协商引擎核心。

    [P0 改动]
      - 构造参数从 db (单 session) 改为 db_or_factory（兼容两种）
      - 推荐传入 session_factory（SSE 场景必需）
      - 传入 session 时退化为旧行为（非 SSE 场景如 resolve_by_user）
    """

    def __init__(self, db_or_factory):
        # 判断是 session 还是 session factory
        # async_session_factory 是 callable 且会返回 session
        # AsyncSession 实例不是 callable
        if callable(db_or_factory) and not hasattr(db_or_factory, "execute"):
            self._session_factory: Optional[Callable] = db_or_factory
            self._db = None
        else:
            self._session_factory = None
            self._db = db_or_factory  # AsyncSession 实例

    def _open_session(self):
        """
        返回一个 async context manager，yield 一个 session。
        - 有 factory 时用 factory 开新 session
        - 否则用持有的 session（不释放）
        """
        if self._session_factory is not None:
            return self._session_factory()
        # 兼容模式：包装现有 session 为不会关闭它的 context
        engine = self

        class _NoCloseCtx:
            async def __aenter__(self_inner):
                return engine._db
            async def __aexit__(self_inner, *args):
                return False
        return _NoCloseCtx()

    async def run(
        self,
        user_id: uuid.UUID,
        task_ids: list[uuid.UUID],
        date_range: tuple[date, date],
        stream: bool = True,
    ) -> AsyncGenerator[SSEEvent, None]:
        session = NegotiationSession(
            id=str(uuid.uuid4()),
            user_id=str(user_id),
            task_ids=[str(t) for t in task_ids],
            date_range=date_range,
        )

        try:
            # ========== Phase 1: 短事务读取上下文 ==========
            session.state = NegotiationState.INIT
            tasks_data = await self._load_tasks_short(user_id, task_ids)

            if not tasks_data:
                yield SSEEvent("error", {"message": "没有找到需要协商的任务"})
                yield SSEEvent("done", {})
                return

            session.involved_spirits = list(set(
                t.get("primary_spirit", "light") for t in tasks_data
            ))

            neg_ctx = await self._build_neg_ctx_short(user_id, tasks_data, date_range)

            # ========== Phase 2-5: 协商循环（无 DB 连接持有） ==========
            for round_num in range(1, MAX_ROUNDS + 1):
                session.current_round = round_num
                current_round = NegotiationRound(round_number=round_num)

                session.state = NegotiationState.COLLECTING_CLAIMS

                if round_num == 1:
                    async for event in self._collect_claims(session, current_round, neg_ctx):
                        yield event
                # 后续轮：responses 已在上一轮尾部收集

                session.state = NegotiationState.DETECTING_CONFLICTS
                conflicts = self._detect_claim_conflicts(current_round.claims)

                if not conflicts:
                    session.state = NegotiationState.CONSENSUS_REACHED
                    schedule = await self._build_consensus_schedule(current_round.claims, neg_ctx)
                    session.final_schedule = schedule

                    yield SSEEvent("consensus", {
                        "reached": True,
                        "round": round_num,
                        "schedule": schedule,
                        "summary": "所有精灵的诉求没有时间冲突，已生成日程。",
                    })
                    current_round.consensus = True
                    session.rounds.append(current_round)
                    break

                session.state = NegotiationState.MEDIATING
                mediation = await self._orchestrate_mediation(
                    session, current_round, conflicts, neg_ctx
                )
                current_round.mediation = mediation

                yield SSEEvent("orchestrator", {
                    "speaker": "orchestrator",
                    "content": mediation.get("content", ""),
                    "type": "mediation",
                    "round": round_num,
                    "conflicts_detected": len(conflicts),
                    "proposed_resolution": mediation.get("proposed_resolution", ""),
                })

                session.state = NegotiationState.AWAITING_RESPONSES
                async for event in self._collect_responses(
                    session, current_round, mediation, neg_ctx
                ):
                    yield event

                session.state = NegotiationState.EVALUATING
                consensus_reached = self._evaluate_consensus(current_round.responses)
                session.rounds.append(current_round)

                if consensus_reached:
                    session.state = NegotiationState.CONSENSUS_REACHED
                    schedule = await self._build_consensus_schedule(
                        current_round.claims, neg_ctx,
                        mediation=mediation, responses=current_round.responses,
                    )
                    session.final_schedule = schedule

                    yield SSEEvent("consensus", {
                        "reached": True,
                        "round": round_num,
                        "schedule": schedule,
                        "summary": self._build_consensus_summary(
                            current_round, session.involved_spirits
                        ),
                    })
                    break

                if round_num < MAX_ROUNDS:
                    new_claims = self._responses_to_claims(
                        current_round.responses, current_round.claims
                    )
                    next_round = NegotiationRound(round_number=round_num + 1)
                    next_round.claims = new_claims

            # ========== Phase 6: 超时 ==========
            if session.state != NegotiationState.CONSENSUS_REACHED:
                session.state = NegotiationState.NEED_USER_INPUT
                options = await self._generate_options(session, neg_ctx)
                session.options = options

                yield SSEEvent("need_user_input", {
                    "negotiation_id": session.id,
                    "message": f"经过{MAX_ROUNDS}轮协商，精灵们未能达成完全共识。请你来做最终决定：",
                    "options": options,
                    "rounds_completed": session.current_round,
                })

            # ========== 短事务保存 ==========
            await self._save_session_short(session)

        except Exception as e:
            logger.error("negotiation_error", error=str(e), session_id=session.id)
            yield SSEEvent("error", {
                "message": "协商过程中出现错误，已降级为自动排程",
                "fallback": True,
            })

            try:
                fallback = await self._fallback_schedule_short(user_id, task_ids, date_range)
                yield SSEEvent("consensus", {
                    "reached": True,
                    "round": 0,
                    "schedule": fallback,
                    "summary": "已按优先级自动排程（协商引擎降级）",
                    "is_fallback": True,
                })
            except Exception as fe:
                logger.error("fallback_also_failed", error=str(fe))

        yield SSEEvent("done", {"negotiation_id": session.id})

    # ========================================
    #  短事务版本的 IO 方法（P0 新增）
    # ========================================

    async def _load_tasks_short(
        self, user_id: uuid.UUID, task_ids: list[uuid.UUID]
    ) -> list[dict]:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.models.task import Task

        async with self._open_session() as db:
            result = await db.execute(
                select(Task)
                .where(Task.id.in_(task_ids), Task.user_id == user_id)
                .options(selectinload(Task.subtasks))
            )
            tasks = []
            for task in result.scalars().all():
                total_minutes = sum(
                    st.duration_minutes or 60
                    for st in task.subtasks
                    if st.status not in ("completed", "cancelled")
                )
                tasks.append({
                    "id": str(task.id),
                    "title": task.title,
                    "primary_spirit": task.primary_spirit,
                    "spirit": task.primary_spirit,
                    "priority": task.priority,
                    "deadline": str(task.deadline) if task.deadline else None,
                    "duration_minutes": total_minutes or 60,
                    "subtask_count": len(task.subtasks),
                })
            return tasks

    async def _build_neg_ctx_short(
        self, user_id: uuid.UUID, tasks_data: list[dict], date_range: tuple[date, date]
    ) -> dict:
        async with self._open_session() as db:
            ctx_builder = ContextBuilder(db)
            return await ctx_builder.build_negotiation_context(
                user_id, tasks_data, date_range
            )

    async def _save_session_short(self, session: NegotiationSession):
        """短事务保存协商会话"""
        from app.models.conversation import Conversation
        from app.services.event_service import EventService

        rounds_data = []
        for r in session.rounds:
            rounds_data.append({
                "round": r.round_number,
                "claims": [
                    {
                        "spirit": c.spirit_code,
                        "message": c.message,
                        "stance": c.stance,
                        "proposed_slots": c.proposed_slots,
                    }
                    for c in r.claims
                ],
                "mediation": r.mediation,
                "responses": [
                    {"spirit": rs.spirit_code, "message": rs.message, "stance": rs.stance}
                    for rs in r.responses
                ],
                "consensus": r.consensus,
            })

        messages = rounds_data
        if session.options:
            messages.append({"type": "need_user_input", "options": session.options})

        async with self._open_session() as db:
            try:
                conv = Conversation(
                    id=uuid.UUID(session.id),
                    user_id=uuid.UUID(session.user_id),
                    spirit_code=None,
                    session_type="negotiation",
                    messages=messages,
                )
                db.add(conv)

                evt_svc = EventService(db)
                await evt_svc.record_event(
                    uuid.UUID(session.user_id),
                    "negotiation_completed",
                    {
                        "negotiation_id": session.id,
                        "rounds": session.current_round,
                        "consensus": session.state == NegotiationState.CONSENSUS_REACHED,
                        "involved_spirits": session.involved_spirits,
                    },
                )
                await db.commit() if self._session_factory else await db.flush()
            except Exception:
                if self._session_factory:
                    await db.rollback()
                raise

    async def _fallback_schedule_short(
        self,
        user_id: uuid.UUID,
        task_ids: list[uuid.UUID],
        date_range: tuple[date, date],
    ) -> list[dict]:
        from app.services.schedule_service import ScheduleService

        async with self._open_session() as db:
            svc = ScheduleService(db)
            result = await svc.generate_schedule(
                user_id=user_id,
                start_date=date_range[0],
                end_date=date_range[1],
                task_ids=task_ids,
            )
            if self._session_factory:
                await db.commit()

            schedule = []
            for day_key, day_data in result.get("schedule", {}).items():
                for item in day_data.get("items", []):
                    schedule.append({
                        "date": day_key,
                        "task": item.get("title", ""),
                        "spirit": item.get("spirit", "light"),
                        "time": f"{item.get('time_start', '')}-{item.get('time_end', '')}",
                        "priority": item.get("priority", "medium"),
                        "from_negotiation": False,
                    })
            return schedule

    # ========================================
    #  用户介入（非 SSE，单 session 即可）
    # ========================================

    async def resolve_by_user(
        self,
        user_id: uuid.UUID,
        negotiation_id: str,
        selected_option: int,
        custom_message: str = "",
    ) -> dict:
        from app.models.conversation import Conversation
        from sqlalchemy import select

        async with self._open_session() as db:
            result = await db.execute(
                select(Conversation).where(
                    Conversation.id == uuid.UUID(negotiation_id),
                    Conversation.user_id == user_id,
                    Conversation.session_type == "negotiation",
                )
            )
            conv = result.scalar_one_or_none()
            if not conv:
                raise ValueError("协商记录不存在")

            messages = list(conv.messages or [])
            negotiation_data = {}
            for msg in messages:
                if msg.get("type") == "need_user_input":
                    negotiation_data = msg
                    break

            options = negotiation_data.get("options", [])
            if not options or selected_option < 0 or selected_option >= len(options):
                raise ValueError("无效的选项")

            chosen = options[selected_option]

            messages.append({
                "type": "user_resolution",
                "selected_option": selected_option,
                "custom_message": custom_message,
                "schedule": chosen.get("schedule", []),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            conv.messages = messages
            conv.updated_at = datetime.now(timezone.utc)

            if self._session_factory:
                await db.commit()
            else:
                await db.flush()

            return {
                "negotiation_id": negotiation_id,
                "resolved": True,
                "schedule": chosen.get("schedule", []),
                "summary": chosen.get("description", "用户已选择方案"),
            }

    # ========================================
    #  以下方法不做 DB 访问，保持原样
    # ========================================

    async def _collect_claims(
        self,
        session: NegotiationSession,
        current_round: NegotiationRound,
        neg_ctx: dict,
    ) -> AsyncGenerator[SSEEvent, None]:
        for spirit_code in session.involved_spirits:
            spirit = get_spirit(spirit_code)
            tasks = neg_ctx.get("spirit_tasks", {}).get(spirit_code, [])
            if not tasks:
                continue

            spirit_ctx_str = ContextBuilder.__dict__["build_spirit_claim_context"](
                None, spirit_code, neg_ctx
            ) if False else self._build_claim_ctx_static(spirit_code, neg_ctx)

            claim_result = await spirit.make_claim(
                tasks=tasks,
                constraints={
                    "date_range": neg_ctx["date_range"],
                    "context": spirit_ctx_str,
                },
            )

            claim = SpiritClaim(
                spirit_code=spirit_code,
                message=claim_result.get("message", ""),
                stance=claim_result.get("stance", "灵活"),
                proposed_slots=claim_result.get("proposed_slots", []),
                compromise_willing=claim_result.get("compromise_willing", True),
                compromise_condition=claim_result.get("compromise_condition", ""),
            )
            current_round.claims.append(claim)

            yield SSEEvent("spirit_message", {
                "speaker": spirit_code,
                "speaker_name": spirit.name,
                "speaker_emoji": spirit.emoji,
                "content": claim.message,
                "type": "claim",
                "round": session.current_round,
                "stance": claim.stance,
                "proposed_slots": claim.proposed_slots,
            })

    @staticmethod
    def _build_claim_ctx_static(spirit_code: str, neg_ctx: dict) -> str:
        """无需 DB 的 spirit_claim_context 静态构建（替代 ContextBuilder.build_spirit_claim_context）"""
        # ContextBuilder.build_spirit_claim_context 本身是纯计算，可以静态调用
        # 这里复用其逻辑：按精灵权重 + 任务列表生成简短文本
        weights = neg_ctx.get("spirit_weights", {})
        weight = weights.get(spirit_code, 50)
        tasks = neg_ctx.get("spirit_tasks", {}).get(spirit_code, [])
        task_lines = [
            f"- {t.get('title')}（{t.get('duration_minutes', 60)}分钟，优先级{t.get('priority','medium')}）"
            for t in tasks
        ]
        return f"你的领域权重：{weight}/100\n你负责的任务：\n" + "\n".join(task_lines)

    def _detect_claim_conflicts(self, claims: list[SpiritClaim]) -> list[dict]:
        conflicts = []
        all_slots = []
        for claim in claims:
            for slot in claim.proposed_slots:
                all_slots.append({
                    "spirit": claim.spirit_code,
                    "task": slot.get("task", ""),
                    "time": slot.get("time", ""),
                    "priority": slot.get("priority", "medium"),
                    "flexible": slot.get("flexible", True),
                })

        time_groups = {}
        for slot in all_slots:
            time_key = slot.get("time", "unspecified")
            time_groups.setdefault(time_key, []).append(slot)

        for time_key, group in time_groups.items():
            if len(group) > 1 and time_key != "" and time_key != "unspecified":
                spirits_involved = list(set(s["spirit"] for s in group))
                if len(spirits_involved) > 1:
                    conflicts.append({
                        "time": time_key,
                        "spirits": spirits_involved,
                        "tasks": [s["task"] for s in group],
                        "details": group,
                    })

        total_requested_minutes = 0
        for claim in claims:
            for slot in claim.proposed_slots:
                total_requested_minutes += self._estimate_slot_duration(slot)

        if total_requested_minutes > 600:
            conflicts.append({
                "time": "overall",
                "spirits": [c.spirit_code for c in claims],
                "tasks": ["总时长超限"],
                "details": {"total_minutes": total_requested_minutes},
            })

        return conflicts

    async def _orchestrate_mediation(
        self,
        session: NegotiationSession,
        current_round: NegotiationRound,
        conflicts: list[dict],
        neg_ctx: dict,
    ) -> dict:
        # 这里不需要 DB，build_orchestrator_prompt_context 是静态方法
        from app.ai.context_builder import ContextBuilder as CB
        # 静态调用：用一次性的实例（不会调 db）
        cb = CB.__new__(CB)
        cb.db = None
        orchestrator_ctx = cb.build_orchestrator_prompt_context(neg_ctx)

        conflict_desc = []
        for c in conflicts:
            spirits_str = "、".join(SPIRIT_NAMES.get(s, s) for s in c.get("spirits", []))
            conflict_desc.append(f"- {spirits_str} 在 {c.get('time', '?')} 时段存在冲突")

        claims_desc = []
        for claim in current_round.claims:
            name = SPIRIT_NAMES.get(claim.spirit_code, claim.spirit_code)
            emoji = SPIRIT_EMOJIS.get(claim.spirit_code, "")
            claims_desc.append(f"{emoji}{name}说：「{claim.message}」（态度：{claim.stance}）")

        external = load_prompt("orchestrator")
        if external:
            system_prompt = f"{external}\n\n{orchestrator_ctx}"
        else:
            system_prompt = f"""你是精灵日程系统的协商主持人。
你的角色是中立的调停者，帮助精灵们解决时间冲突。

{orchestrator_ctx}

## 你的任务
分析精灵们的诉求和冲突，提出公平的调停方案。

## 调停原则
1. 高优先级任务优先
2. 有deadline的任务优先
3. 参考精灵强度权重——权重高的精灵更应被满足
4. 健康规则不可违反（连续工作上限、运动最低要求）
5. 态度"坚持"的精灵需要更强的理由才能让步
6. 尝试找到双赢方案（如调整时间段、压缩时长、拆分任务）

请输出 JSON：
{{
  "content": "你的调停发言（不超过120字，要有条理，指名道姓对精灵提建议）",
  "proposed_resolution": "方案摘要",
  "adjustments": [
    {{"spirit": "精灵代码", "task": "任务名", "original_time": "原时间", "suggested_time": "建议时间", "reason": "原因"}}
  ]
}}"""

        claims_text = "\n".join(claims_desc)
        conflicts_text = "\n".join(conflict_desc) if conflict_desc else "无明显时间段冲突，但总时长可能超限"

        user_prompt = f"""这是第{session.current_round}轮协商。

## 各精灵诉求：
{claims_text}

## 检测到的冲突：
{conflicts_text}

请提出调停方案。"""

        result = await llm_client.complete_json(
            system=system_prompt,
            user=user_prompt,
            purpose="negotiation_mediation",
        )

        if result and result.get("content"):
            return result

        weights = neg_ctx.get("spirit_weights", {})
        sorted_spirits = sorted(
            session.involved_spirits,
            key=lambda s: weights.get(s, 50),
            reverse=True,
        )
        winner = SPIRIT_NAMES.get(sorted_spirits[0], sorted_spirits[0])
        return {
            "content": f"看来大家的时间安排有些紧张。我建议优先满足{winner}的需求，其他精灵的任务调整到次日或其他时段。",
            "proposed_resolution": f"按权重优先排序: {', '.join(SPIRIT_NAMES.get(s, s) for s in sorted_spirits)}",
            "adjustments": [],
        }

    async def _collect_responses(
        self,
        session: NegotiationSession,
        current_round: NegotiationRound,
        mediation: dict,
        neg_ctx: dict,
    ) -> AsyncGenerator[SSEEvent, None]:
        for claim in current_round.claims:
            spirit_code = claim.spirit_code
            spirit = get_spirit(spirit_code)

            related_conflicts = [
                f"与{SPIRIT_NAMES.get(c.spirit_code, '?')}的「{s.get('task', '?')}」冲突"
                for c in current_round.claims
                if c.spirit_code != spirit_code
                for s in c.proposed_slots
            ]

            response_result = await spirit.respond_to_mediation(
                mediation=mediation,
                own_claims=claim.proposed_slots,
                conflicts=related_conflicts[:3],
            )

            response = SpiritResponse(
                spirit_code=spirit_code,
                message=response_result.get("message", ""),
                stance=response_result.get("stance", "accept"),
                adjusted_slots=response_result.get("adjusted_slots", []),
            )
            current_round.responses.append(response)

            yield SSEEvent("spirit_message", {
                "speaker": spirit_code,
                "speaker_name": spirit.name,
                "speaker_emoji": spirit.emoji,
                "content": response.message,
                "type": "response",
                "round": session.current_round,
                "stance": response.stance,
            })

    def _evaluate_consensus(self, responses: list[SpiritResponse]) -> bool:
        if not responses:
            return False
        accept_count = sum(1 for r in responses if r.stance == "accept")
        insist_count = sum(1 for r in responses if r.stance == "insist")
        if accept_count == len(responses):
            return True
        if accept_count > len(responses) * 0.6 and insist_count == 0:
            return True
        return False

    async def _build_consensus_schedule(
        self,
        claims: list[SpiritClaim],
        neg_ctx: dict,
        mediation: dict = None,
        responses: list[SpiritResponse] = None,
    ) -> list[dict]:
        schedule_items = []
        final_slots = []

        if responses:
            for resp in responses:
                if resp.adjusted_slots:
                    final_slots.extend([
                        {**s, "spirit": resp.spirit_code}
                        for s in resp.adjusted_slots
                    ])

        if not final_slots:
            for claim in claims:
                for slot in claim.proposed_slots:
                    final_slots.append({**slot, "spirit": claim.spirit_code})

        for slot in final_slots:
            schedule_items.append({
                "id": str(uuid.uuid4()),
                "task": slot.get("task", ""),
                "spirit": slot.get("spirit", "light"),
                "spirit_name": SPIRIT_NAMES.get(slot.get("spirit", ""), ""),
                "spirit_emoji": SPIRIT_EMOJIS.get(slot.get("spirit", ""), ""),
                "time": slot.get("time", ""),
                "priority": slot.get("priority", "medium"),
                "from_negotiation": True,
            })

        return schedule_items

    async def _generate_options(
        self, session: NegotiationSession, neg_ctx: dict
    ) -> list[dict]:
        options = []
        weights = neg_ctx.get("spirit_weights", {})

        sorted_by_weight = sorted(
            session.involved_spirits,
            key=lambda s: weights.get(s, 50),
            reverse=True,
        )
        options.append({
            "label": "按精灵权重优先",
            "description": f"优先满足: {'→'.join(SPIRIT_NAMES.get(s, s) for s in sorted_by_weight)}",
            "strategy": "weight_priority",
            "spirit_order": sorted_by_weight,
            "schedule": [],
        })

        spirit_tasks = neg_ctx.get("spirit_tasks", {})
        urgency_order = []
        for code in session.involved_spirits:
            tasks = spirit_tasks.get(code, [])
            has_deadline = any(t.get("deadline") for t in tasks)
            has_high = any(t.get("priority") == "high" for t in tasks)
            score = (2 if has_deadline else 0) + (1 if has_high else 0)
            urgency_order.append((code, score))
        urgency_order.sort(key=lambda x: -x[1])

        options.append({
            "label": "按紧急度优先",
            "description": "有截止日期和高优先级的任务先排",
            "strategy": "urgency_priority",
            "spirit_order": [s[0] for s in urgency_order],
            "schedule": [],
        })

        options.append({
            "label": "均衡分配",
            "description": "每个精灵平均分配可用时间，牺牲低优先级任务时长",
            "strategy": "balanced",
            "spirit_order": session.involved_spirits,
            "schedule": [],
        })

        return options

    def _responses_to_claims(
        self,
        responses: list[SpiritResponse],
        prev_claims: list[SpiritClaim],
    ) -> list[SpiritClaim]:
        new_claims = []
        prev_map = {c.spirit_code: c for c in prev_claims}
        for resp in responses:
            prev = prev_map.get(resp.spirit_code)
            slots = resp.adjusted_slots if resp.adjusted_slots else (
                prev.proposed_slots if prev else []
            )
            new_claims.append(SpiritClaim(
                spirit_code=resp.spirit_code,
                message=resp.message,
                stance="坚持" if resp.stance == "insist" else "灵活",
                proposed_slots=slots,
                compromise_willing=(resp.stance != "insist"),
            ))
        return new_claims

    def _build_consensus_summary(
        self, current_round: NegotiationRound, spirits: list[str]
    ) -> str:
        parts = []
        for resp in current_round.responses:
            name = SPIRIT_NAMES.get(resp.spirit_code, resp.spirit_code)
            if resp.stance == "accept":
                parts.append(f"{name}同意")
            elif resp.stance == "counter":
                parts.append(f"{name}提出调整")
            else:
                parts.append(f"{name}坚持")
        return f"第{current_round.round_number}轮达成共识：{'，'.join(parts)}。"

    def _estimate_slot_duration(self, slot: dict) -> int:
        time_str = slot.get("time", "")
        if "-" in time_str:
            parts = time_str.split("-")
            try:
                sh, sm = map(int, parts[0].strip().split(":"))
                eh, em = map(int, parts[1].strip().split(":"))
                return max(0, (eh * 60 + em) - (sh * 60 + sm))
            except (ValueError, IndexError):
                pass
        return 60