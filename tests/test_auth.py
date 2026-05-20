"""
鉴权接口测试
"""
import pytest


@pytest.mark.asyncio
async def test_register_success(client):
    response = await client.post("/api/v1/auth/register", json={
        "email": "newuser@test.com",
        "password": "password123",
        "name": "New User",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "access_token" in data["data"]
    assert "refresh_token" in data["data"]
    assert data["data"]["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    """重复邮箱注册应失败"""
    payload = {
        "email": "dup@test.com",
        "password": "password123",
        "name": "User",
    }
    await client.post("/api/v1/auth/register", json=payload)
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_register_invalid_email(client):
    """无效邮箱格式应返回 422"""
    response = await client.post("/api/v1/auth/register", json={
        "email": "not-an-email",
        "password": "password123",
        "name": "User",
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_short_password(client):
    """密码太短应返回 422"""
    response = await client.post("/api/v1/auth/register", json={
        "email": "short@test.com",
        "password": "123",
        "name": "User",
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_login_success(client):
    """正常登录"""
    await client.post("/api/v1/auth/register", json={
        "email": "login@test.com",
        "password": "password123",
        "name": "Login User",
    })
    response = await client.post("/api/v1/auth/login", json={
        "email": "login@test.com",
        "password": "password123",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "access_token" in data["data"]


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    """错误密码"""
    await client.post("/api/v1/auth/register", json={
        "email": "wrongpw@test.com",
        "password": "password123",
        "name": "User",
    })
    response = await client.post("/api/v1/auth/login", json={
        "email": "wrongpw@test.com",
        "password": "wrongpassword",
    })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token(client):
    """刷新 token"""
    reg = await client.post("/api/v1/auth/register", json={
        "email": "refresh@test.com",
        "password": "password123",
        "name": "User",
    })
    refresh_token = reg.json()["data"]["refresh_token"]

    response = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": refresh_token,
    })
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data["data"]
    # 旧 token 应该失效
    response2 = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": refresh_token,
    })
    assert response2.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_without_token(client):
    """访问需要鉴权的接口但不带 token"""
    response = await client.get("/api/v1/users/me")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_protected_route_with_token(client):
    """带 token 访问"""
    reg = await client.post("/api/v1/auth/register", json={
        "email": "protected@test.com",
        "password": "password123",
        "name": "Protected User",
    })
    token = reg.json()["data"]["access_token"]

    response = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["data"]["name"] == "Protected User"
    assert data["data"]["email"] == "protected@test.com"
