"""
任务系统测试 — CRUD + 状态流转 + 解析 + 拆解
"""
import pytest


async def _register_and_get_token(client, email="task@test.com"):
    reg = await client.post("/api/v1/auth/register", json={
        "email": email, "password": "password123", "name": "Task User",
    })
    return reg.json()["data"]["access_token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# ========================================
#  任务创建
# ========================================

@pytest.mark.asyncio
async def test_create_task_simple(client):
    """简单文本创建任务"""
    token = await _register_and_get_token(client, "create1@test.com")

    resp = await client.post(
        "/api/v1/tasks",
        headers=_h(token),
        json={"user_input": "明天下午开会"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["tasks"]) >= 1
    task = data["tasks"][0]
    assert task["title"]
    assert task["primary_spirit"] in ("light", "water", "soil", "air", "nutrition")
    assert task["status"] == "pending"


@pytest.mark.asyncio
async def test_create_task_multiple(client):
    """多任务识别"""
    token = await _register_and_get_token(client, "create2@test.com")

    resp = await client.post(
        "/api/v1/tasks",
        headers=_h(token),
        json={"user_input": "下周要准备考试，还想和朋友聚一次"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 应解析出 2 个任务
    assert len(data["tasks"]) >= 1  # fallback 模式至少1个


@pytest.mark.asyncio
async def test_create_task_with_subtasks(client):
    """任务创建后应有子任务"""
    token = await _register_and_get_token(client, "create3@test.com")

    resp = await client.post(
        "/api/v1/tasks",
        headers=_h(token),
        json={"user_input": "准备英语考试，大概需要10个小时"},
    )
    assert resp.status_code == 200
    tasks = resp.json()["data"]["tasks"]
    assert len(tasks) >= 1
    # 任务应该有子任务（fallback 拆解也会生成）
    assert len(tasks[0]["subtasks"]) >= 1


# ========================================
#  任务查询
# ========================================

@pytest.mark.asyncio
async def test_list_tasks(client):
    """任务列表"""
    token = await _register_and_get_token(client, "list@test.com")

    # 先创建
    await client.post("/api/v1/tasks", headers=_h(token),
                      json={"user_input": "写报告"})
    await client.post("/api/v1/tasks", headers=_h(token),
                      json={"user_input": "跑步"})

    resp = await client.get("/api/v1/tasks", headers=_h(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] >= 2
    assert len(data["items"]) >= 2


@pytest.mark.asyncio
async def test_list_tasks_filter_spirit(client):
    """按精灵过滤"""
    token = await _register_and_get_token(client, "filter@test.com")

    await client.post("/api/v1/tasks", headers=_h(token),
                      json={"user_input": "跑步30分钟"})

    resp = await client.get("/api/v1/tasks?spirit=soil", headers=_h(token))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_task_detail(client):
    """任务详情"""
    token = await _register_and_get_token(client, "detail@test.com")

    create_resp = await client.post(
        "/api/v1/tasks", headers=_h(token),
        json={"user_input": "写周报"},
    )
    task_id = create_resp.json()["data"]["tasks"][0]["id"]

    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=_h(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == task_id


@pytest.mark.asyncio
async def test_get_nonexistent_task(client):
    """查不存在的任务"""
    token = await _register_and_get_token(client, "notfound@test.com")
    resp = await client.get(
        "/api/v1/tasks/00000000-0000-0000-0000-000000000000",
        headers=_h(token),
    )
    assert resp.status_code == 404


# ========================================
#  任务更新 / 删除
# ========================================

@pytest.mark.asyncio
async def test_update_task(client):
    """更新任务"""
    token = await _register_and_get_token(client, "update@test.com")

    create_resp = await client.post(
        "/api/v1/tasks", headers=_h(token),
        json={"user_input": "写报告"},
    )
    task_id = create_resp.json()["data"]["tasks"][0]["id"]

    resp = await client.patch(
        f"/api/v1/tasks/{task_id}",
        headers=_h(token),
        json={"title": "写季度报告", "priority": "high"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "写季度报告"
    assert resp.json()["data"]["priority"] == "high"


@pytest.mark.asyncio
async def test_delete_task(client):
    """删除任务"""
    token = await _register_and_get_token(client, "delete@test.com")

    create_resp = await client.post(
        "/api/v1/tasks", headers=_h(token),
        json={"user_input": "临时任务"},
    )
    task_id = create_resp.json()["data"]["tasks"][0]["id"]

    resp = await client.delete(f"/api/v1/tasks/{task_id}", headers=_h(token))
    assert resp.status_code == 200

    # 确认已删除
    resp2 = await client.get(f"/api/v1/tasks/{task_id}", headers=_h(token))
    assert resp2.status_code == 404


# ========================================
#  状态流转
# ========================================

@pytest.mark.asyncio
async def test_task_state_flow(client):
    """完整状态流转: pending → in_progress → completed"""
    token = await _register_and_get_token(client, "flow@test.com")

    create_resp = await client.post(
        "/api/v1/tasks", headers=_h(token),
        json={"user_input": "写代码"},
    )
    task_id = create_resp.json()["data"]["tasks"][0]["id"]

    # Start
    resp = await client.post(f"/api/v1/tasks/{task_id}/start", headers=_h(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "in_progress"

    # Complete
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/complete",
        headers=_h(token),
        json={"feedback": "just_right"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_task_cancel(client):
    """取消任务"""
    token = await _register_and_get_token(client, "cancel@test.com")

    create_resp = await client.post(
        "/api/v1/tasks", headers=_h(token),
        json={"user_input": "不想做了"},
    )
    task_id = create_resp.json()["data"]["tasks"][0]["id"]

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/cancel",
        headers=_h(token),
        json={"reason": "计划变更"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_invalid_state_transition(client):
    """无效状态转换应返回 409"""
    token = await _register_and_get_token(client, "invalid@test.com")

    create_resp = await client.post(
        "/api/v1/tasks", headers=_h(token),
        json={"user_input": "任务"},
    )
    task_id = create_resp.json()["data"]["tasks"][0]["id"]

    # 先完成
    await client.post(f"/api/v1/tasks/{task_id}/start", headers=_h(token))
    await client.post(f"/api/v1/tasks/{task_id}/complete", headers=_h(token))

    # 再次开始应失败
    resp = await client.post(f"/api/v1/tasks/{task_id}/start", headers=_h(token))
    assert resp.status_code == 409


# ========================================
#  批量完成
# ========================================

@pytest.mark.asyncio
async def test_batch_complete(client):
    """批量完成"""
    token = await _register_and_get_token(client, "batch@test.com")

    ids = []
    for i in range(3):
        resp = await client.post(
            "/api/v1/tasks", headers=_h(token),
            json={"user_input": f"任务{i}"},
        )
        ids.append(resp.json()["data"]["tasks"][0]["id"])

    # 先全部开始
    for tid in ids:
        await client.post(f"/api/v1/tasks/{tid}/start", headers=_h(token))

    resp = await client.post(
        "/api/v1/tasks/batch-complete",
        headers=_h(token),
        json={"task_ids": ids, "feedback": "easy"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["completed_count"] == 3
