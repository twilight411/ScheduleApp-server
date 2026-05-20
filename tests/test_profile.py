"""
用户画像 + 精灵强度测试
"""
import pytest


async def _register_and_get_token(client, email="profile@test.com"):
    """辅助函数：注册并返回 token"""
    reg = await client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "password123",
        "name": "Test User",
    })
    return reg.json()["data"]["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ========================================
#  画像测试
# ========================================

@pytest.mark.asyncio
async def test_get_profile(client):
    """获取画像 — 注册后应有默认画像"""
    token = await _register_and_get_token(client, "getprofile@test.com")

    response = await client.get("/api/v1/profile", headers=_auth_headers(token))
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["onboarding_completed"] is False
    assert len(data["spirit_intensities"]) == 5
    # 默认强度均为 50
    for si in data["spirit_intensities"]:
        assert si["base_intensity"] == 50
        assert si["effective_intensity"] == 50


@pytest.mark.asyncio
async def test_update_preferences(client):
    """更新偏好 — 增量合并"""
    token = await _register_and_get_token(client, "updatepref@test.com")

    response = await client.patch(
        "/api/v1/profile",
        headers=_auth_headers(token),
        json={"preferences": {"wake_time": "06:30", "sleep_time": "22:30"}},
    )
    assert response.status_code == 200
    prefs = response.json()["data"]["preferences"]
    assert prefs["wake_time"] == "06:30"
    assert prefs["sleep_time"] == "22:30"
    # 未修改的字段应保留默认值
    assert prefs["energy_pattern"] == "balanced"


@pytest.mark.asyncio
async def test_update_preferences_partial(client):
    """多次部分更新应正确合并"""
    token = await _register_and_get_token(client, "partialpref@test.com")

    # 第一次更新
    await client.patch(
        "/api/v1/profile",
        headers=_auth_headers(token),
        json={"preferences": {"wake_time": "05:00"}},
    )
    # 第二次更新
    await client.patch(
        "/api/v1/profile",
        headers=_auth_headers(token),
        json={"preferences": {"min_sleep_hours": 8}},
    )
    # 验证两次更新都保留
    response = await client.get("/api/v1/profile", headers=_auth_headers(token))
    prefs = response.json()["data"]["preferences"]
    assert prefs["wake_time"] == "05:00"
    assert prefs["min_sleep_hours"] == 8


# ========================================
#  Onboarding 测试
# ========================================

@pytest.mark.asyncio
async def test_onboarding(client):
    """新用户引导 — 应自动设置偏好和精灵强度"""
    token = await _register_and_get_token(client, "onboarding@test.com")

    response = await client.post(
        "/api/v1/profile/onboarding",
        headers=_auth_headers(token),
        json={
            "work_schedule": "9to5",
            "energy_pattern": "morning",
            "exercise_habit": "daily",
            "social_frequency": "weekly",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["onboarding_completed"] is True

    # 偏好应已被设置
    assert data["preferences"]["energy_pattern"] == "morning"

    # 精灵强度应已调整
    intensities = {si["spirit_code"]: si["base_intensity"] for si in data["spirit_intensities"]}
    assert intensities["light"] == 70  # 9to5 默认
    assert intensities["soil"] == 80   # 50 base + 30 daily exercise boost


@pytest.mark.asyncio
async def test_onboarding_idempotent(client):
    """重复引导应返回已完成提示"""
    token = await _register_and_get_token(client, "obidempotent@test.com")

    await client.post(
        "/api/v1/profile/onboarding",
        headers=_auth_headers(token),
        json={"work_schedule": "flexible", "energy_pattern": "balanced",
              "exercise_habit": "sometimes", "social_frequency": "weekly"},
    )
    response = await client.post(
        "/api/v1/profile/onboarding",
        headers=_auth_headers(token),
        json={"work_schedule": "9to5", "energy_pattern": "morning",
              "exercise_habit": "daily", "social_frequency": "daily"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["already_completed"] is True


# ========================================
#  精灵强度测试
# ========================================

@pytest.mark.asyncio
async def test_get_intensities(client):
    """获取精灵强度 — 应返回5个精灵"""
    token = await _register_and_get_token(client, "getintensity@test.com")

    response = await client.get("/api/v1/profile/intensity", headers=_auth_headers(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 5
    codes = {si["spirit_code"] for si in data}
    assert codes == {"light", "water", "soil", "air", "nutrition"}


@pytest.mark.asyncio
async def test_update_single_intensity(client):
    """更新单个精灵强度"""
    token = await _register_and_get_token(client, "singleintensity@test.com")

    response = await client.patch(
        "/api/v1/profile/intensity",
        headers=_auth_headers(token),
        json={"intensities": [{"spirit_code": "light", "base_intensity": 80}]},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 1
    assert data[0]["spirit_code"] == "light"
    assert data[0]["base_intensity"] == 80
    assert data[0]["effective_intensity"] == 80


@pytest.mark.asyncio
async def test_update_batch_intensity(client):
    """批量更新精灵强度"""
    token = await _register_and_get_token(client, "batchintensity@test.com")

    response = await client.patch(
        "/api/v1/profile/intensity",
        headers=_auth_headers(token),
        json={"intensities": [
            {"spirit_code": "light", "base_intensity": 90},
            {"spirit_code": "soil", "base_intensity": 70},
            {"spirit_code": "water", "base_intensity": 30},
        ]},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    result_map = {si["spirit_code"]: si["base_intensity"] for si in data}
    assert result_map["light"] == 90
    assert result_map["soil"] == 70
    assert result_map["water"] == 30


@pytest.mark.asyncio
async def test_lock_intensity(client):
    """锁定精灵强度 — 系统不再自动调整"""
    token = await _register_and_get_token(client, "lockintensity@test.com")

    response = await client.patch(
        "/api/v1/profile/intensity",
        headers=_auth_headers(token),
        json={"intensities": [
            {"spirit_code": "light", "base_intensity": 80, "is_locked": True},
        ]},
    )
    assert response.status_code == 200
    assert response.json()["data"][0]["is_locked"] is True


@pytest.mark.asyncio
async def test_invalid_spirit_code(client):
    """无效精灵代码应返回 422"""
    token = await _register_and_get_token(client, "invalidspirit@test.com")

    response = await client.patch(
        "/api/v1/profile/intensity",
        headers=_auth_headers(token),
        json={"intensities": [{"spirit_code": "fire", "base_intensity": 50}]},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_intensity_out_of_range(client):
    """强度超范围应返回 422"""
    token = await _register_and_get_token(client, "outofrange@test.com")

    response = await client.patch(
        "/api/v1/profile/intensity",
        headers=_auth_headers(token),
        json={"intensities": [{"spirit_code": "light", "base_intensity": 150}]},
    )
    assert response.status_code == 422


# ========================================
#  模板测试
# ========================================

@pytest.mark.asyncio
async def test_list_templates(client):
    """获取模板列表"""
    token = await _register_and_get_token(client, "listtmpl@test.com")
    response = await client.get(
        "/api/v1/profile/intensity/templates",
        headers=_auth_headers(token),
    )
    assert response.status_code == 200
    # 模板可能为空（需要运行 init_templates 脚本）
    assert isinstance(response.json()["data"], list)
