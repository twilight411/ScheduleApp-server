"""
P0-4 补丁：在 routers/tasks.py 的 create_task 中消费 GroupChatTrigger 的检测结果

【现状】task_service.check_trigger_after_create() 已经实现，但 routers/tasks.py
的 POST /tasks 创建完任务后**没有调用它**——前端永远拿不到"该不该协商"的提示。

【改动位置】replace 现有的 routers/tasks.py 中 `async def create_task(...)` 函数体
（大约 82-141 行）为下面这段。其余 import 不需要新增。

【效果】响应里多两个字段：
  - "negotiation_suggestion": 可能为 null 或 dict
  - "should_negotiate":      bool

前端 UI 据此决定要不要提示用户"是否启动精灵协商"。
"""

# ↓↓↓ 替换原有 create_task 函数  ↓↓↓

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

# ... 其他 imports 保持原样 ...


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
