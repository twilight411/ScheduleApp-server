"""
AI 服务接口 — 解析、精灵对话、任务拆解、协商、时间槽推荐

[P0 修复 v2]
  - /negotiate, /free-chat 不再用 Depends(get_db)
    改为传入 async_session_factory，引擎内部按需开短事务
    SSE 期间 SQLite 不被霸占，避免死锁

  - /spirits/{code}/chat 在调用 chat_to_task 校验时传入 history_text
    让多轮对话中"上轮说日期、本轮说事项"的场景能被正确识别
"""
import uuid
import json
import asyncio
from datetime import date as date_type, datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session_factory
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.schemas.ai import (
    ParseRequest,
    SpiritChatRequest,
    DecomposeRequest,
    NegotiateRequest,
    NegotiateResolveRequest,
    SuggestSlotRequest,
)
from app.services.task_service import TaskService
from app.services.profile_service import ProfileService
from app.services.conversation_service import ConversationService
from app.services.schedule_service import ScheduleService
from app.services.event_service import EventService
from app.ai.task_parser import task_parser
from app.ai.spirits import get_spirit, VALID_SPIRIT_CODES
from app.ai.spirits.chat_to_task import chat_to_task_detector
from app.ai.free_chat import FreeChatEngine

import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/ai", tags=["AI"])


# ====================================================================
#  解析
# ====================================================================

@router.post("/parse")
async def parse_input(
    body: ParseRequest,
    current_user: User = Depends(get_current_user),
):
    result = await task_parser.parse(
        user_input=body.user_input,
        user_id=str(current_user.id),
    )
    return success_response(data=result)


# ====================================================================
#  精灵对话
# ====================================================================

@router.post("/spirits/{spirit_code}/chat")
async def spirit_chat(
    spirit_code: str,
    body: SpiritChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    单精灵对话 — 支持多轮 + Chat-to-Task 自动识别。

    [P0 修复] validate_llm_suggestion 现在接收 history_text，
    可以从历史消息中补全当前消息缺失的日期/时间。
    """
    if spirit_code not in VALID_SPIRIT_CODES:
        raise HTTPException(
            status_code=400,
            detail=error_response("VALIDATION_ERROR", f"无效精灵: {spirit_code}"),
        )

    spirit = get_spirit(spirit_code)
    conv_svc = ConversationService(db)
    profile_svc = ProfileService(db)

    session_uuid = None
    if body.session_id:
        try:
            session_uuid = uuid.UUID(body.session_id)
        except ValueError:
            pass

    conv = await conv_svc.get_or_create_session(
        current_user.id, spirit_code, session_uuid
    )

    await conv_svc.append_message(conv, "user", body.message)

    spirit_params = await profile_svc.get_spirit_params(current_user.id, spirit_code)

    result = await spirit.chat(
        message=body.message,
        history=conv.messages or [],
        session_id=str(conv.id),
        user_profile=spirit_params,
    )

    await conv_svc.append_message(conv, "assistant", result.get("message", ""))

    suggestion_out = {"detected": False}
    raw_suggestion = result.get("task_suggestion", {})

    if raw_suggestion.get("detected"):
        # ===== P0 修复：把对话历史也传给 validator =====
        history_text = ""
        for m in (conv.messages or [])[-10:]:
            if m.get("role") == "user":
                history_text += m.get("content", "") + "\n"

        validated = chat_to_task_detector.validate_llm_suggestion(
            suggestion=raw_suggestion,
            spirit_code=spirit_code,
            user_message=body.message,
            history_text=history_text,
        )

        if validated.get("detected"):
            saved = await conv_svc.save_task_suggestion(
                user_id=current_user.id,
                session_id=conv.id,
                spirit_code=validated.get("spirit", spirit_code),
                suggestion=validated,
            )
            if saved:
                suggestion_out = {
                    "detected": True,
                    "suggestion_id": str(saved.id),
                    "title": saved.title,
                    "spirit": validated.get("spirit", spirit_code),
                    "date": str(saved.suggested_date) if saved.suggested_date else None,
                    "time_start": saved.time_start,
                    "time_end": saved.time_end,
                    "duration_minutes": saved.duration_minutes,
                    "confidence": validated.get("confidence", 0),
                }

    return success_response(data={
        "spirit": spirit_code,
        "spirit_name": spirit.name,
        "spirit_emoji": spirit.emoji,
        "message": result.get("message", ""),
        "session_id": str(conv.id),
        "task_suggestion": suggestion_out,
    })


# ====================================================================
#  手动拆解
# ====================================================================

@router.post("/spirits/decompose")
async def decompose(
    body: DecomposeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task_svc = TaskService(db)
    profile_svc = ProfileService(db)

    task = await task_svc.get_task(body.task_id, current_user.id)
    if not task:
        raise HTTPException(
            status_code=404,
            detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"),
        )

    if task.subtasks:
        return success_response(
            data={
                "task_id": str(task.id),
                "spirit": task.primary_spirit,
                "subtasks": [
                    {
                        "title": st.title,
                        "duration_minutes": st.duration_minutes,
                        "suggested_time": st.suggested_time,
                        "priority": st.priority,
                        "tips": st.spirit_tip or "",
                    }
                    for st in task.subtasks
                ],
                "spirit_comment": "任务已拆解过，如需重新拆解请先删除现有子任务。",
                "already_decomposed": True,
            }
        )

    spirit = get_spirit(task.primary_spirit)
    spirit_params = await profile_svc.get_spirit_params(current_user.id, task.primary_spirit)
    sched_params = await profile_svc.get_scheduling_params(current_user.id)
    merged_params = {**sched_params, **spirit_params}

    decomposed = await spirit.decompose_task(
        task={
            "title": task.title,
            "estimated_hours": task.estimated_hours or 1,
            "deadline": str(task.deadline) if task.deadline else None,
            "priority": task.priority,
        },
        user_profile=merged_params,
    )

    subtasks = await task_svc.create_subtasks(task, decomposed)

    return success_response(data={
        "task_id": str(task.id),
        "spirit": task.primary_spirit,
        "subtasks": decomposed.get("subtasks", []),
        "spirit_comment": decomposed.get("spirit_comment", ""),
        "already_decomposed": False,
    })


# ====================================================================
#  协商引擎 — SSE 流式输出（P0 改造）
# ====================================================================

@router.post("/negotiate")
async def negotiate(
    body: NegotiateRequest,
    current_user: User = Depends(get_current_user),
):
    """
    发起精灵协商 — SSE 流式输出。

    [P0 修复] 不再用 Depends(get_db)；引擎内部用 session_factory 开短事务，
    SSE 期间 DB 连接不会被一直霸占。
    """
    from app.ai.negotiation import NegotiationEngine
    from datetime import timedelta

    # 解析日期范围
    if body.date:
        try:
            target = dt.strptime(body.date, "%Y-%m-%d").date()
        except ValueError:
            target = date_type.today()
    else:
        target = date_type.today()
    date_range = (target, target + timedelta(days=6))

    # 解析任务 ID
    task_ids = []
    for tid in body.task_ids:
        try:
            task_ids.append(uuid.UUID(tid))
        except ValueError:
            continue

    # 没指定任务时，开短事务查询用户的待处理任务
    if not task_ids:
        async with async_session_factory() as db:
            task_svc = TaskService(db)
            pending_tasks, _ = await task_svc.list_tasks(
                user_id=current_user.id, status="pending", page=1, page_size=20,
            )
            in_progress, _ = await task_svc.list_tasks(
                user_id=current_user.id, status="in_progress", page=1, page_size=20,
            )
            task_ids = [t.id for t in (pending_tasks + in_progress)]

    if not task_ids:
        return success_response(
            data={"message": "没有需要协商的任务"},
            message="当前没有待处理的任务",
        )

    # 短事务：记录事件
    async with async_session_factory() as db:
        evt_svc = EventService(db)
        await evt_svc.record_event(
            current_user.id,
            "negotiation_started",
            {
                "task_ids": [str(t) for t in task_ids],
                "date_range": [str(date_range[0]), str(date_range[1])],
                "trigger_reason": body.trigger_reason,
            },
        )
        await db.commit()

    # 引擎用 session_factory（SSE 期间不持有连接）
    engine = NegotiationEngine(async_session_factory)

    async def sse_generator():
        try:
            async for event in engine.run(
                user_id=current_user.id,
                task_ids=task_ids,
                date_range=date_range,
            ):
                yield event.to_sse()
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.error("sse_generator_error", error=str(e))
            error_event = (
                f"event: error\n"
                f"data: {json.dumps({'message': '协商过程异常', 'fallback': True}, ensure_ascii=False)}\n\n"
                f"event: done\n"
                f"data: {{}}\n\n"
            )
            yield error_event

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/negotiate/resolve")
async def resolve_negotiation(
    body: NegotiateResolveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户介入协商 — 选方案或提交自定义决策。非流式，用单 session。"""
    from app.ai.negotiation import NegotiationEngine

    # 这里传入的是 session（非 factory），引擎兼容模式工作
    engine = NegotiationEngine(db)

    try:
        selected_option = -1
        custom_message = ""

        if body.decision.startswith("option_"):
            try:
                selected_option = int(body.decision.split("_")[1])
            except (IndexError, ValueError):
                selected_option = 0
        else:
            custom_message = body.decision
            selected_option = 0

        result = await engine.resolve_by_user(
            user_id=current_user.id,
            negotiation_id=body.negotiation_id,
            selected_option=selected_option,
            custom_message=custom_message,
        )

        if result.get("schedule"):
            sched_svc = ScheduleService(db)
            await _apply_negotiation_schedule(
                sched_svc, current_user.id, result["schedule"]
            )

        return success_response(data=result, message="协商已解决")

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=error_response("VALIDATION_ERROR", str(e)),
        )


@router.get("/negotiate/status/{negotiation_id}")
async def get_negotiation_status(
    negotiation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.conversation import Conversation
    from sqlalchemy import select

    try:
        neg_uuid = uuid.UUID(negotiation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=error_response("VALIDATION_ERROR", "无效的协商 ID"))

    result = await db.execute(
        select(Conversation).where(
            Conversation.id == neg_uuid,
            Conversation.user_id == current_user.id,
            Conversation.session_type == "negotiation",
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(
            status_code=404,
            detail=error_response("RESOURCE_NOT_FOUND", "协商记录不存在"),
        )

    messages = conv.messages or []

    has_consensus = any(
        isinstance(m, dict) and m.get("consensus", False)
        for m in messages
    )
    needs_input = any(
        isinstance(m, dict) and m.get("type") == "need_user_input"
        for m in messages
    )
    is_resolved = any(
        isinstance(m, dict) and m.get("type") == "user_resolution"
        for m in messages
    )

    if is_resolved:
        state = "resolved"
    elif has_consensus:
        state = "consensus_reached"
    elif needs_input:
        state = "need_user_input"
    else:
        state = "in_progress"

    return success_response(data={
        "negotiation_id": negotiation_id,
        "state": state,
        "rounds": len([
            m for m in messages
            if isinstance(m, dict) and "round" in m
        ]),
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    })


# ====================================================================
#  时间槽推荐
# ====================================================================

@router.post("/suggest-slot")
async def suggest_slot(
    body: SuggestSlotRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = ScheduleService(db)
    target = None
    if body.date:
        try:
            target = dt.strptime(body.date, "%Y-%m-%d").date()
        except ValueError:
            pass

    suggestions = await svc.suggest_slot(
        user_id=current_user.id,
        duration_minutes=body.duration_minutes,
        spirit=body.spirit,
        target_date=target,
    )
    return success_response(data={"suggestions": suggestions})


# ====================================================================
#  辅助
# ====================================================================

async def _apply_negotiation_schedule(
    sched_svc: ScheduleService,
    user_id: uuid.UUID,
    schedule_items: list[dict],
):
    by_date: dict[str, list] = {}
    for item in schedule_items:
        date_str = item.get("date", "")
        if not date_str:
            continue
        by_date.setdefault(date_str, []).append(item)

    for date_str, items in by_date.items():
        try:
            target_date = dt.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        existing = await sched_svc.get_day_schedule(user_id, target_date)
        existing_items = existing.get("items", []) if existing else []

        existing_titles = {it.get("title", ""): i for i, it in enumerate(existing_items)}
        merged = list(existing_items)

        for neg_item in items:
            title = neg_item.get("task", "")
            time_str = neg_item.get("time", "")
            time_parts = time_str.split("-") if "-" in time_str else ["", ""]

            new_item = {
                "title": title,
                "spirit": neg_item.get("spirit", "light"),
                "time_start": time_parts[0].strip() if time_parts[0] else "",
                "time_end": time_parts[1].strip() if len(time_parts) > 1 else "",
                "priority": neg_item.get("priority", "medium"),
                "from_negotiation": True,
            }

            if title in existing_titles:
                merged[existing_titles[title]] = new_item
            else:
                merged.append(new_item)

        merged.sort(key=lambda it: it.get("time_start", "99:99"))
        await sched_svc.save_day_schedule(user_id, target_date, merged)

    logger.info(
        "negotiation_schedule_applied",
        user_id=str(user_id),
        dates=list(by_date.keys()),
        total_items=len(schedule_items),
    )


# ========================================
#  自由群聊（P0 改造）
# ========================================

@router.post("/free-chat")
async def start_free_chat(
    current_user: User = Depends(get_current_user),
    topic: str = None,
    spirit_codes: str = None,
):
    """
    启动自由群聊模式 - SSE 流式响应。

    [P0 修复] 不再用 Depends(get_db)；引擎用 session_factory 内部按需开会话。

    NOTE：群聊语义重构（让精灵真正互怼/共识收敛）会在 P1 进行。
    """
    spirit_list = None
    if spirit_codes:
        spirit_list = [code.strip() for code in spirit_codes.split(",") if code.strip()]

    engine = FreeChatEngine(async_session_factory)

    async def sse_generator():
        try:
            async for event in engine.run(
                user_id=current_user.id,
                topic=topic,
                spirit_codes=spirit_list,
            ):
                yield event
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.error("free_chat_error", error=str(e))
            error_event = (
                f"event: error\n"
                f"data: {json.dumps({'message': '群聊过程中出现错误'}, ensure_ascii=False)}\n\n"
                f"event: done\n"
                f"data: {{}}\n\n"
            )
            yield error_event

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )