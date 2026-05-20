"""
AI 服务测试 — 解析、精灵对话、拆解
"""
import pytest


async def _register_and_get_token(client, email="ai@test.com"):
    reg = await client.post("/api/v1/auth/register", json={
        "email": email, "password": "password123", "name": "AI User",
    })
    return reg.json()["data"]["access_token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# ========================================
#  解析测试
# ========================================

@pytest.mark.asyncio
async def test_parse_simple(client):
    """简单解析 — fallback 模式也应返回有效结构"""
    token = await _register_and_get_token(client, "parse1@test.com")

    resp = await client.post(
        "/api/v1/ai/parse",
        headers=_h(token),
        json={"user_input": "明天去跑步"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "tasks" in data
    assert len(data["tasks"]) >= 1
    assert data["tasks"][0]["primary_spirit"] == "soil"  # 跑步 → 土壤精灵


@pytest.mark.asyncio
async def test_parse_multi(client):
    """多任务解析"""
    token = await _register_and_get_token(client, "parse2@test.com")

    resp = await client.post(
        "/api/v1/ai/parse",
        headers=_h(token),
        json={"user_input": "复习考试，还要和朋友聚餐"},
    )
    assert resp.status_code == 200
    tasks = resp.json()["data"]["tasks"]
    assert len(tasks) >= 1


@pytest.mark.asyncio
async def test_parse_empty_input(client):
    """空输入应返回 422"""
    token = await _register_and_get_token(client, "parse3@test.com")

    resp = await client.post(
        "/api/v1/ai/parse",
        headers=_h(token),
        json={"user_input": ""},
    )
    assert resp.status_code == 422


# ========================================
#  精灵对话测试
# ========================================

@pytest.mark.asyncio
async def test_spirit_chat(client):
    """精灵对话基本流程"""
    token = await _register_and_get_token(client, "chat1@test.com")

    resp = await client.post(
        "/api/v1/ai/spirits/light/chat",
        headers=_h(token),
        json={"message": "帮我规划一下这周的学习"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["spirit"] == "light"
    assert data["spirit_name"] == "光精灵"
    assert data["spirit_emoji"] == "💡"
    assert data["message"]
    assert data["session_id"]


@pytest.mark.asyncio
async def test_spirit_chat_multi_turn(client):
    """多轮对话"""
    token = await _register_and_get_token(client, "chat2@test.com")

    # 第一轮
    resp1 = await client.post(
        "/api/v1/ai/spirits/soil/chat",
        headers=_h(token),
        json={"message": "最近想开始健身"},
    )
    session_id = resp1.json()["data"]["session_id"]

    # 第二轮（带 session_id）
    resp2 = await client.post(
        "/api/v1/ai/spirits/soil/chat",
        headers=_h(token),
        json={"message": "每周三次可以吗？", "session_id": session_id},
    )
    assert resp2.status_code == 200
    assert resp2.json()["data"]["session_id"] == session_id


@pytest.mark.asyncio
async def test_spirit_chat_invalid_code(client):
    """无效精灵代码"""
    token = await _register_and_get_token(client, "chat3@test.com")

    resp = await client.post(
        "/api/v1/ai/spirits/fire/chat",
        headers=_h(token),
        json={"message": "你好"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_all_spirits_chat(client):
    """五个精灵都能对话"""
    token = await _register_and_get_token(client, "chat4@test.com")

    for code in ["light", "water", "soil", "air", "nutrition"]:
        resp = await client.post(
            f"/api/v1/ai/spirits/{code}/chat",
            headers=_h(token),
            json={"message": "你好"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["spirit"] == code


# ========================================
#  拆解测试
# ========================================

@pytest.mark.asyncio
async def test_decompose(client):
    """手动触发拆解"""
    token = await _register_and_get_token(client, "decompose@test.com")

    # 先创建任务
    create_resp = await client.post(
        "/api/v1/tasks",
        headers=_h(token),
        json={"user_input": "准备数学考试"},
    )
    task = create_resp.json()["data"]["tasks"][0]

    # 已经自动拆解了，再次调用应提示已拆解
    resp = await client.post(
        "/api/v1/ai/spirits/decompose",
        headers=_h(token),
        json={"task_id": task["id"]},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["task_id"] == task["id"]
    assert data.get("already_decomposed") is True
