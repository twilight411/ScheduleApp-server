"""
任务管理接口 — 完整 CRUD + 状态流转 + Chat-to-Task

Sprint B 变更:
  - /from-chat/resolve-conflict → 真实冲突解决实现（替换 placeholder）
  - /from-chat 链路中增加自动排入日程
  - 创建任务时使用 get_spirit_params（含 intensity）
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.schemas.task import (
    TaskCreateRequest,
    TaskUpdateRequest,
    TaskCompleteRequest,
    TaskCancelRequest,
    TaskRescheduleRequest,
    TaskOut,
    SubTaskOut,
    ChatTaskCreateRequest,
    ChatTaskResolveRequest,
    BatchCompleteRequest,
    SubTaskCompletionUpdateRequest,  # Sprint 1
)
from app.services.task_service import TaskService
from app.services.profile_service import ProfileService
from app.services.schedule_service import ScheduleService
from app.ai.task_parser import task_parser
from app.ai.spirits import get_spirit, VALID_SPIRIT_CODES

import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/tasks", tags=["Tasks"])


def _task_to_dict(task) -> dict:
    """将 Task ORM 对象转为响应字典"""
    subtasks = []
    for st in (task.subtasks or []):
        subtasks.append({
            "id": str(st.id),
            "task_id": str(st.task_id),
            "spirit": st.spirit,
            "title": st.title,
            "duration_minutes": st.duration_minutes,
            "scheduled_start": st.scheduled_start.isoformat() if st.scheduled_start else None,
            "scheduled_end": st.scheduled_end.isoformat() if st.scheduled_end else None,
            "status": st.status,
            "priority": st.priority,
            "spirit_tip": st.spirit_tip,
            "suggested_time": st.suggested_time,
            # ─── Sprint 1 新增 ───
            "completion_percent": st.completion_percent or 0,
            "quality_note": st.quality_note,
            "user_feedback": st.user_feedback,
            "self_reported_at": st.self_reported_at.isoformat() if st.self_reported_at else None,
        })
    return {
        "id": str(task.id),
        "title": task.title,
        "raw_input": task.raw_input,
        "primary_spirit": task.primary_spirit,
        "secondary_spirits": task.secondary_spirits or [],
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "estimated_hours": task.estimated_hours,
        "priority": task.priority,
        "is_recurring": task.is_recurring,
        "recurrence_pattern": task.recurrence_pattern,
        "status": task.status,
        "source": task.source,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "subtasks": subtasks,
    }


# ========================================
#  创建
# ========================================

@router.post("")
async def create_task(
    body: TaskCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    创建任务（自然语言输入）。

    [P0 修复] 创建完成后调用 check_trigger_after_create，
    消费 8 种群聊触发场景的检测结果，让前端有"启动协商"入口。
    """
    task_svc = TaskService(db)
    profile_svc = ProfileService(db)

    # 1. 解析自然语言
    parsed = await task_parser.parse(
        user_input=body.user_input,
        user_id=str(current_user.id),
    )

    needs_clarification = any(t.get("needs_clarification") for t in parsed.get("tasks", []))

    # 2. 创建每个任务 + 拆解
    created_tasks = []
    aggregated_trigger: dict | None = None  # 跨多任务汇总的触发结果

    for task_data in parsed.get("tasks", []):
        task = await task_svc.create_task(current_user.id, task_data, source="parsed")

        spirit_params = await profile_svc.get_spirit_params(
            current_user.id, task.primary_spirit
        )
        sched_params = await profile_svc.get_scheduling_params(current_user.id)
        merged_params = {**sched_params, **spirit_params}

        spirit = get_spirit(task.primary_spirit)
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

        # ===== P0 新增：检测群聊触发器 =====
        try:
            trigger_result = await task_svc.check_trigger_after_create(
                user_id=current_user.id,
                task=task,
            )
        except Exception as e:
            logger.warning("trigger_check_failed", task_id=str(task.id), error=str(e))
            trigger_result = None

        # 多任务时取最严重的那个触发
        if trigger_result and trigger_result.get("should_negotiate"):
            severity_rank = {"low": 0, "medium": 1, "high": 2}
            cur_sev = severity_rank.get(trigger_result.get("severity", "low"), 0)
            prev_sev = severity_rank.get(
                (aggregated_trigger or {}).get("severity", "low"), -1
            ) if aggregated_trigger else -1
            if cur_sev > prev_sev:
                aggregated_trigger = trigger_result

        # 重新加载任务（含子任务）
        task = await task_svc.get_task(task.id, current_user.id)
        created_tasks.append({
            **_task_to_dict(task),
            "spirit_comment": decomposed.get("spirit_comment", ""),
        })

    # 3. 组装响应
    response_data = {
        "tasks": created_tasks,
        "parse_confidence": parsed.get("overall_confidence", 0),
        "suggestions": parsed.get("suggestions", []),
        "needs_clarification": needs_clarification,
        # ===== P0 新增字段 =====
        "should_negotiate": bool(aggregated_trigger),
        "negotiation_suggestion": aggregated_trigger,
        # aggregated_trigger 结构：
        #   {
        #     "should_negotiate": True,
        #     "severity": "high|medium|low",
        #     "reason": "....",
        #     "involved_spirits": ["light","soil"],
        #     "trigger_types": ["time_conflict", "resource_surge"],
        #     "suggested_task_ids": [...],
        #   }
    }

    return success_response(data=response_data)


# ========================================
#  查询
# ========================================

@router.get("")
async def list_tasks(
    status: str = Query(None, description="过滤状态: pending/in_progress/completed/cancelled"),
    spirit: str = Query(None, description="过滤精灵: light/water/soil/air/nutrition"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取任务列表（分页 + 过滤）"""
    svc = TaskService(db)
    tasks, total = await svc.list_tasks(
        user_id=current_user.id,
        status=status,
        spirit=spirit,
        page=page,
        page_size=page_size,
    )
    return success_response(data={
        "items": [_task_to_dict(t) for t in tasks],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    })


@router.get("/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取单个任务详情（含子任务）"""
    svc = TaskService(db)
    task = await svc.get_task(task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"))
    return success_response(data=_task_to_dict(task))


# ========================================
#  更新 / 删除
# ========================================

@router.patch("/{task_id}")
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新任务（标题、优先级、deadline 等）"""
    svc = TaskService(db)
    task = await svc.get_task(task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"))

    updates = body.model_dump(exclude_unset=True)
    task = await svc.update_task(task, updates)
    return success_response(data=_task_to_dict(task))


@router.delete("/{task_id}")
async def delete_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除任务（级联删除子任务）"""
    svc = TaskService(db)
    ok = await svc.delete_task(task_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"))
    return success_response(message="任务已删除")


# ========================================
#  状态流转
# ========================================

@router.post("/{task_id}/start")
async def start_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = TaskService(db)
    task = await svc.get_task(task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"))
    try:
        task = await svc.start_task(task)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=error_response("INVALID_STATE_TRANSITION", str(e)))
    return success_response(data=_task_to_dict(task))


@router.post("/{task_id}/pause")
async def pause_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = TaskService(db)
    task = await svc.get_task(task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"))
    try:
        task = await svc.pause_task(task)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=error_response("INVALID_STATE_TRANSITION", str(e)))
    return success_response(data=_task_to_dict(task))


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: uuid.UUID,
    body: TaskCompleteRequest = TaskCompleteRequest(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = TaskService(db)
    task = await svc.get_task(task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"))
    try:
        task = await svc.complete_task(task, body.feedback)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=error_response("INVALID_STATE_TRANSITION", str(e)))
    return success_response(data=_task_to_dict(task), message="任务已完成 🎉")


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID,
    body: TaskCancelRequest = TaskCancelRequest(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = TaskService(db)
    task = await svc.get_task(task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"))
    try:
        task = await svc.cancel_task(task, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=error_response("INVALID_STATE_TRANSITION", str(e)))
    return success_response(data=_task_to_dict(task), message="任务已取消")


@router.post("/{task_id}/reschedule")
async def reschedule_task(
    task_id: uuid.UUID,
    body: TaskRescheduleRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = TaskService(db)
    task = await svc.get_task(task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"))
    task = await svc.reschedule_task(task, body.new_start, body.new_end, body.reason)
    return success_response(data=_task_to_dict(task), message="任务已改期")


# ========================================
#  Chat-to-Task + 冲突解决
# ========================================

@router.post("/from-chat")
async def create_from_chat(
    body: ChatTaskCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    从对话中创建任务。

    Sprint B: 创建后自动尝试排入日程，返回冲突信息。
    """
    task_svc = TaskService(db)
    sched_svc = ScheduleService(db)
    profile_svc = ProfileService(db)

    try:
        task, has_conflict, conflicts = await task_svc.create_from_chat(
            user_id=current_user.id,
            suggestion_id=body.suggestion_id,
            data=body.model_dump(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=error_response("VALIDATION_ERROR", str(e)))

    # Sprint B: 如果无冲突，自动排入日程
    scheduled = False
    schedule_info = None
    if not has_conflict and body.time_start:
        try:
            from datetime import date as date_cls, datetime as dt_cls

            target_date = None
            if body.date:
                try:
                    target_date = dt_cls.strptime(body.date, "%Y-%m-%d").date()
                except ValueError:
                    target_date = date_cls.today()
            else:
                target_date = date_cls.today()

            # 获取当前日程并添加新条目
            existing = await sched_svc.get_day_schedule(current_user.id, target_date)
            items = existing.get("items", []) if existing else []

            new_item = {
                "title": task.title,
                "spirit": task.primary_spirit,
                "time_start": body.time_start or "",
                "time_end": body.time_end or "",
                "priority": body.priority or "medium",
                "task_id": str(task.id),
                "from_chat": True,
            }
            items.append(new_item)

            # 按时间排序
            items.sort(key=lambda x: x.get("time_start", "99:99"))

            await sched_svc.save_day_schedule(current_user.id, target_date, items)
            scheduled = True
            schedule_info = {
                "date": str(target_date),
                "time_start": body.time_start,
                "time_end": body.time_end,
            }

            logger.info(
                "chat_task_scheduled",
                task_id=str(task.id),
                date=str(target_date),
            )
        except Exception as e:
            logger.warning("chat_task_schedule_failed", error=str(e))

    return success_response(data={
        "task_id": str(task.id),
        "scheduled": scheduled,
        "schedule_info": schedule_info,
        "conflicts": conflicts,
        "has_conflict": has_conflict,
    })


@router.post("/from-chat/resolve-conflict")
async def resolve_chat_conflict(
    body: ChatTaskResolveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    解决 Chat-to-Task 产生的日程冲突。

    Sprint B: 真实实现（替换 placeholder）。
    支持三种冲突解决策略:
      - replace:    替换冲突的已有条目
      - reschedule: 将新任务改到用户指定的新时间
      - cancel:     取消新任务创建
    """
    task_svc = TaskService(db)
    sched_svc = ScheduleService(db)

    try:
        task_uuid = uuid.UUID(body.task_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=error_response("VALIDATION_ERROR", "无效的任务 ID"),
        )

    task = await task_svc.get_task(task_uuid, current_user.id)
    if not task:
        raise HTTPException(
            status_code=404,
            detail=error_response("RESOURCE_NOT_FOUND", "任务不存在"),
        )

    resolution = body.conflict_resolution

    if resolution == "cancel":
        # 取消任务
        await task_svc.cancel_task(task, "用户取消—日程冲突")
        return success_response(
            data={"task_id": str(task.id), "action": "cancelled"},
            message="任务已取消",
        )

    elif resolution == "replace":
        # 替换冲突条目：找到冲突时段，移除旧条目，插入新条目
        from datetime import date as date_cls, datetime as dt_cls

        target_date = date_cls.today()
        if body.new_start:
            # 从新时间推算日期（如果有的话）
            pass

        existing = await sched_svc.get_day_schedule(current_user.id, target_date)
        items = existing.get("items", []) if existing else []

        # 移除与新任务时间段冲突的条目
        new_start = body.new_start or ""
        new_end = body.new_end or ""
        filtered = []
        replaced = []
        for item in items:
            if _times_overlap(
                item.get("time_start", ""),
                item.get("time_end", ""),
                new_start,
                new_end,
            ):
                replaced.append(item)
            else:
                filtered.append(item)

        # 添加新任务
        filtered.append({
            "title": task.title,
            "spirit": task.primary_spirit,
            "time_start": new_start,
            "time_end": new_end,
            "priority": task.priority,
            "task_id": str(task.id),
            "from_chat": True,
        })
        filtered.sort(key=lambda x: x.get("time_start", "99:99"))

        await sched_svc.save_day_schedule(current_user.id, target_date, filtered)

        return success_response(
            data={
                "task_id": str(task.id),
                "action": "replaced",
                "replaced_items": replaced,
                "schedule_info": {"date": str(target_date), "time_start": new_start, "time_end": new_end},
            },
            message=f"已替换 {len(replaced)} 个冲突条目",
        )

    elif resolution == "reschedule":
        # 改期到新时间
        if not body.new_start or not body.new_end:
            raise HTTPException(
                status_code=400,
                detail=error_response("VALIDATION_ERROR", "reschedule 需要提供 new_start 和 new_end"),
            )

        task = await task_svc.reschedule_task(task, body.new_start, body.new_end, "冲突改期")

        return success_response(
            data={
                "task_id": str(task.id),
                "action": "rescheduled",
                "new_start": body.new_start,
                "new_end": body.new_end,
            },
            message="任务已改期",
        )

    else:
        raise HTTPException(
            status_code=400,
            detail=error_response("VALIDATION_ERROR", f"不支持的解决策略: {resolution}，可选: replace/reschedule/cancel"),
        )


@router.post("/batch-complete")
async def batch_complete(
    body: BatchCompleteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量标记完成"""
    svc = TaskService(db)
    completed = await svc.batch_complete(current_user.id, body.task_ids, body.feedback)
    return success_response(
        data={"completed_count": len(completed), "task_ids": [str(t.id) for t in completed]},
    )


# ========================================
#  从自由群聊创建任务
# ========================================

@router.post("/from-free-chat")
async def create_from_free_chat(
    title: str,
    spirit: str,
    date: Optional[str] = None,
    time_start: Optional[str] = None,
    time_end: Optional[str] = None,
    duration_minutes: int = 60,
    priority: str = "medium",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    从自由群聊的任务建议直接创建任务并添加到日程。
    """
    task_svc = TaskService(db)
    sched_svc = ScheduleService(db)

    # 创建任务
    from datetime import date as date_cls, datetime as dt_cls, timedelta

    target_date = date_cls.today()
    if date:
        try:
            target_date = dt_cls.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            pass

    # 创建任务数据
    task_data = {
        "title": title,
        "primary_spirit": spirit,
        "priority": priority,
        "estimated_hours": duration_minutes / 60,
    }

    # 创建任务
    task = await task_svc.create_task(
        user_id=current_user.id,
        task_data=task_data,
        source="free_chat",
    )

    # 如果有时间信息，添加到日程
    scheduled = False
    schedule_info = None
    if time_start:
        try:
            # 获取当前日程并添加新条目
            existing = await sched_svc.get_day_schedule(current_user.id, target_date)
            items = existing.get("items", []) if existing else []

            # 如果没有提供结束时间，自动计算
            if not time_end and duration_minutes:
                sh, sm = map(int, time_start.split(":"))
                total_min = sh * 60 + sm + duration_minutes
                eh, em = divmod(total_min, 60)
                eh = eh % 24
                time_end = f"{eh:02d}:{em:02d}"

            new_item = {
                "title": task.title,
                "spirit": task.primary_spirit,
                "time_start": time_start,
                "time_end": time_end or "",
                "priority": priority,
                "task_id": str(task.id),
                "from_chat": True,
                "from_free_chat": True,
            }
            items.append(new_item)

            # 按时间排序
            items.sort(key=lambda x: x.get("time_start", "99:99"))

            await sched_svc.save_day_schedule(current_user.id, target_date, items)
            scheduled = True
            schedule_info = {
                "date": str(target_date),
                "time_start": time_start,
                "time_end": time_end,
            }

            logger.info(
                "free_chat_task_scheduled",
                task_id=str(task.id),
                date=str(target_date),
            )
        except Exception as e:
            logger.warning("free_chat_task_schedule_failed", error=str(e))

    return success_response(data={
        "task_id": str(task.id),
        "scheduled": scheduled,
        "schedule_info": schedule_info,
    })


# ========================================
#  Sprint 1: 子任务完成度
# ========================================

def _subtask_to_dict(st) -> dict:
    """单个子任务序列化"""
    return {
        "id": str(st.id),
        "task_id": str(st.task_id),
        "spirit": st.spirit,
        "title": st.title,
        "duration_minutes": st.duration_minutes,
        "scheduled_start": st.scheduled_start.isoformat() if st.scheduled_start else None,
        "scheduled_end": st.scheduled_end.isoformat() if st.scheduled_end else None,
        "status": st.status,
        "priority": st.priority,
        "spirit_tip": st.spirit_tip,
        "suggested_time": st.suggested_time,
        "completion_percent": st.completion_percent or 0,
        "quality_note": st.quality_note,
        "user_feedback": st.user_feedback,
        "self_reported_at": st.self_reported_at.isoformat() if st.self_reported_at else None,
        "actual_start": st.actual_start.isoformat() if st.actual_start else None,
        "actual_end": st.actual_end.isoformat() if st.actual_end else None,
    }


@router.patch("/subtasks/{subtask_id}/completion")
async def update_subtask_completion(
    subtask_id: uuid.UUID,
    body: SubTaskCompletionUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    更新单个子任务的连续完成度。

    Body:
      completion_percent: 0 / 25 / 50 / 75 / 100  (必填)
      quality_note:       部分完成时的说明                   (可选, 给周末 AI)
      user_feedback:      easy / just_right / hard           (可选)
      auto_advance_status: 默认 True, 由完成度自动联动 status

    返回更新后的 SubTask。
    """
    svc = TaskService(db)
    try:
        st = await svc.update_subtask_completion(
            subtask_id=subtask_id,
            user_id=current_user.id,
            completion_percent=body.completion_percent,
            quality_note=body.quality_note,
            user_feedback=body.user_feedback,
            auto_advance_status=body.auto_advance_status,
        )
    except ValueError as e:
        msg = str(e)
        code = "RESOURCE_NOT_FOUND" if "不存在" in msg else "VALIDATION_ERROR"
        http_code = 404 if code == "RESOURCE_NOT_FOUND" else 400
        raise HTTPException(http_code, detail=error_response(code, msg))

    return success_response(
        data=_subtask_to_dict(st),
        message="完成度已更新",
    )


# ========================================
#  辅助函数
# ========================================

def _times_overlap(
    start_a: str, end_a: str, start_b: str, end_b: str
) -> bool:
    """检查两个时间段是否重叠"""
    if not start_a or not end_a or not start_b or not end_b:
        return False
    try:
        a_start = int(start_a.replace(":", ""))
        a_end = int(end_a.replace(":", ""))
        b_start = int(start_b.replace(":", ""))
        b_end = int(end_b.replace(":", ""))
        return a_start < b_end and b_start < a_end
    except (ValueError, AttributeError):
        return False
