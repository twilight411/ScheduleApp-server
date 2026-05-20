"""
日程调度测试 — 算法 + API 端到端
"""
import pytest
from datetime import date, datetime, timedelta


# ========================================
#  纯算法测试（不需要 DB）
# ========================================

class TestSchedulerAlgorithm:
    """调度算法单元测试"""

    def test_time_slot_basic(self):
        from app.ai.scheduler import TimeSlot
        s1 = TimeSlot(datetime(2024, 1, 8, 9, 0), datetime(2024, 1, 8, 12, 0))
        assert s1.duration_minutes == 180
        s2 = TimeSlot(datetime(2024, 1, 8, 11, 0), datetime(2024, 1, 8, 13, 0))
        assert s1.overlaps(s2)
        s3 = TimeSlot(datetime(2024, 1, 8, 13, 0), datetime(2024, 1, 8, 15, 0))
        assert not s1.overlaps(s3)

    def test_time_slot_split(self):
        from app.ai.scheduler import TimeSlot
        big = TimeSlot(datetime(2024, 1, 8, 9, 0), datetime(2024, 1, 8, 17, 0))
        parts = big.split_at(datetime(2024, 1, 8, 12, 0), datetime(2024, 1, 8, 13, 0))
        assert len(parts) == 2
        assert parts[0].duration_minutes == 180  # 9-12
        assert parts[1].duration_minutes == 240  # 13-17

    def test_generate_slots(self):
        from app.ai.scheduler import generate_available_slots
        profile = {
            "wake_time": "07:00",
            "sleep_time": "23:00",
            "meal_times": ["07:30", "12:00", "18:30"],
        }
        d = date(2024, 1, 8)
        slots = generate_available_slots((d, d), profile)
        assert d in slots
        assert len(slots[d]) >= 3  # 至少有上午、下午、晚上的时间槽
        total = sum(s.duration_minutes for s in slots[d])
        assert total > 0
        # 排除了3餐各30分钟 = 90分钟，从16h(960min)中
        assert total <= 960 - 90 + 10  # 允许一点误差

    def test_sort_subtasks(self):
        from app.ai.scheduler import sort_subtasks, SubTaskInput
        tasks = [
            SubTaskInput(id="1", task_id="t", title="low", spirit="light",
                        duration_minutes=60, priority="low"),
            SubTaskInput(id="2", task_id="t", title="high", spirit="light",
                        duration_minutes=60, priority="high"),
            SubTaskInput(id="3", task_id="t", title="fixed", spirit="light",
                        duration_minutes=60, is_fixed=True,
                        fixed_start=datetime(2024, 1, 8, 9, 0),
                        fixed_end=datetime(2024, 1, 8, 10, 0)),
        ]
        sorted_t = sort_subtasks(tasks)
        assert sorted_t[0].title == "fixed"   # 固定排最前
        assert sorted_t[1].title == "high"    # 高优先级
        assert sorted_t[2].title == "low"

    def test_allocate_basic(self):
        from app.ai.scheduler import (
            allocate_schedule, SubTaskInput, TimeSlot, generate_available_slots,
        )
        profile = {
            "wake_time": "08:00", "sleep_time": "22:00",
            "meal_times": ["12:00", "18:00"],
            "peak_hours": ["09:00-11:00"],
        }
        d = date(2024, 1, 8)
        slots = generate_available_slots((d, d), profile)

        tasks = [
            SubTaskInput(id="1", task_id="t1", title="学习", spirit="light",
                        duration_minutes=90, priority="high"),
            SubTaskInput(id="2", task_id="t2", title="跑步", spirit="soil",
                        duration_minutes=30, priority="medium"),
        ]

        scheduled, unscheduled = allocate_schedule(tasks, slots, profile)
        assert len(scheduled) == 2
        assert len(unscheduled) == 0
        # 高优先级应该排在前面
        assert scheduled[0].title == "学习"

    def test_allocate_overflow(self):
        """任务超过可用时间"""
        from app.ai.scheduler import allocate_schedule, SubTaskInput, TimeSlot
        # 只给1小时可用
        d = date(2024, 1, 8)
        slots = {d: [TimeSlot(datetime(2024, 1, 8, 9, 0), datetime(2024, 1, 8, 10, 0))]}
        tasks = [
            SubTaskInput(id="1", task_id="t", title="长任务", spirit="light",
                        duration_minutes=90, priority="high"),
        ]
        scheduled, unscheduled = allocate_schedule(tasks, slots, {"peak_hours": []})
        assert len(scheduled) == 0
        assert len(unscheduled) == 1

    def test_detect_conflicts(self):
        from app.ai.scheduler import detect_conflicts, ScheduledItem, TimeSlot
        items = [
            ScheduledItem(id="1", subtask_id="s1", task_id="t1", title="任务A",
                         slot=TimeSlot(datetime(2024, 1, 8, 9, 0), datetime(2024, 1, 8, 11, 0)),
                         spirit="light", priority="high"),
            ScheduledItem(id="2", subtask_id="s2", task_id="t2", title="任务B",
                         slot=TimeSlot(datetime(2024, 1, 8, 10, 0), datetime(2024, 1, 8, 12, 0)),
                         spirit="air", priority="medium"),
        ]
        conflicts = detect_conflicts(items)
        assert len(conflicts) == 1
        assert "时间冲突" in conflicts[0].description

    def test_no_conflicts(self):
        from app.ai.scheduler import detect_conflicts, ScheduledItem, TimeSlot
        items = [
            ScheduledItem(id="1", subtask_id="s1", task_id="t1", title="A",
                         slot=TimeSlot(datetime(2024, 1, 8, 9, 0), datetime(2024, 1, 8, 10, 0)),
                         spirit="light", priority="high"),
            ScheduledItem(id="2", subtask_id="s2", task_id="t2", title="B",
                         slot=TimeSlot(datetime(2024, 1, 8, 10, 30), datetime(2024, 1, 8, 12, 0)),
                         spirit="air", priority="medium"),
        ]
        conflicts = detect_conflicts(items)
        assert len(conflicts) == 0

    def test_health_rules(self):
        from app.ai.scheduler import check_health_rules, ScheduledItem, TimeSlot
        # 没有运动的一天
        items = [
            ScheduledItem(id="1", subtask_id="s1", task_id="t1", title="工作",
                         slot=TimeSlot(datetime(2024, 1, 8, 9, 0), datetime(2024, 1, 8, 17, 0)),
                         spirit="light", priority="high"),
        ]
        profile = {"max_continuous_work_minutes": 120, "daily_exercise_target_minutes": 30, "max_daily_work_hours": 8}
        warnings = check_health_rules(items, profile)
        # 应有：没运动 + 总时长超标(8h)
        assert len(warnings) >= 1
        types = [w.description for w in warnings]
        assert any("运动" in t for t in types)

    def test_full_pipeline(self):
        from app.ai.scheduler import run_scheduling_pipeline, SubTaskInput
        profile = {
            "wake_time": "07:00", "sleep_time": "23:00",
            "meal_times": ["07:30", "12:00", "18:30"],
            "peak_hours": ["09:00-11:00", "15:00-17:00"],
            "max_continuous_work_minutes": 120,
            "daily_exercise_target_minutes": 30,
            "max_daily_work_hours": 10,
        }
        d = date(2024, 1, 8)
        tasks = [
            SubTaskInput(id="1", task_id="t1", title="复习大纲", spirit="light",
                        duration_minutes=60, priority="high", suggested_time="morning"),
            SubTaskInput(id="2", task_id="t1", title="做题", spirit="light",
                        duration_minutes=90, priority="high"),
            SubTaskInput(id="3", task_id="t2", title="跑步", spirit="soil",
                        duration_minutes=30, priority="medium"),
            SubTaskInput(id="4", task_id="t3", title="聚会", spirit="air",
                        duration_minutes=180, priority="medium",
                        suggested_time="evening"),
        ]
        result = run_scheduling_pipeline(tasks, (d, d), profile)
        assert result["stats"]["total_scheduled"] == 4
        assert result["stats"]["total_unscheduled"] == 0
        assert len(result["scheduled"]) == 4
        # 每个都应有精灵提示
        for item in result["scheduled"]:
            assert item.spirit_tip

    def test_multi_day(self):
        from app.ai.scheduler import run_scheduling_pipeline, SubTaskInput
        profile = {
            "wake_time": "08:00", "sleep_time": "22:00",
            "meal_times": ["12:00", "18:00"],
            "peak_hours": ["09:00-11:00"],
            "max_continuous_work_minutes": 120,
            "daily_exercise_target_minutes": 30,
            "max_daily_work_hours": 10,
        }
        start = date(2024, 1, 8)
        end = date(2024, 1, 10)
        tasks = [
            SubTaskInput(id=str(i), task_id="t", title=f"任务{i}", spirit="light",
                        duration_minutes=120, priority="medium")
            for i in range(6)
        ]
        result = run_scheduling_pipeline(tasks, (start, end), profile)
        assert result["stats"]["total_scheduled"] >= 4  # 3天应该能排6个


# ========================================
#  API 端到端测试
# ========================================

async def _register_and_get_token(client, email="sched@test.com"):
    reg = await client.post("/api/v1/auth/register", json={
        "email": email, "password": "password123", "name": "Sched User",
    })
    return reg.json()["data"]["access_token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_get_today_empty(client):
    """空日程"""
    token = await _register_and_get_token(client, "today@test.com")
    resp = await client.get("/api/v1/schedule/today", headers=_h(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_generate_schedule(client):
    """生成日程"""
    token = await _register_and_get_token(client, "gen@test.com")

    # 先创建任务
    await client.post("/api/v1/tasks", headers=_h(token),
                      json={"user_input": "明天准备考试，需要3小时"})
    await client.post("/api/v1/tasks", headers=_h(token),
                      json={"user_input": "跑步30分钟"})

    # 生成日程
    from datetime import date, timedelta
    start = date.today()
    end = start + timedelta(days=2)

    resp = await client.post("/api/v1/schedule/generate", headers=_h(token), json={
        "start_date": str(start),
        "end_date": str(end),
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["stats"]["total_scheduled"] >= 1
    assert "schedule" in data


@pytest.mark.asyncio
async def test_generate_and_query(client):
    """生成后查询"""
    token = await _register_and_get_token(client, "genq@test.com")

    await client.post("/api/v1/tasks", headers=_h(token),
                      json={"user_input": "写报告"})

    from datetime import date, timedelta
    start = date.today()
    end = start + timedelta(days=1)

    await client.post("/api/v1/schedule/generate", headers=_h(token), json={
        "start_date": str(start), "end_date": str(end),
    })

    # 查询今天的日程
    resp = await client.get("/api/v1/schedule/today", headers=_h(token))
    assert resp.status_code == 200

    # 查询日期范围
    resp2 = await client.get(
        f"/api/v1/schedule/range?start={start}&end={end}",
        headers=_h(token),
    )
    assert resp2.status_code == 200
    assert "days" in resp2.json()["data"]


@pytest.mark.asyncio
async def test_generate_invalid_range(client):
    """无效日期范围"""
    token = await _register_and_get_token(client, "invalid@sched.com")

    resp = await client.post("/api/v1/schedule/generate", headers=_h(token), json={
        "start_date": "2024-03-01",
        "end_date": "2024-01-01",  # 结束早于开始
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_check_conflicts(client):
    """冲突检测"""
    token = await _register_and_get_token(client, "conflict@test.com")

    from datetime import date, timedelta
    start = date.today()
    end = start + timedelta(days=1)

    resp = await client.post("/api/v1/schedule/check-conflicts", headers=_h(token), json={
        "start_date": str(start), "end_date": str(end),
    })
    assert resp.status_code == 200
    assert "total_issues" in resp.json()["data"]


@pytest.mark.asyncio
async def test_suggest_slot(client):
    """推荐时间槽"""
    token = await _register_and_get_token(client, "suggest@test.com")

    resp = await client.post("/api/v1/ai/suggest-slot", headers=_h(token), json={
        "duration_minutes": 60,
        "spirit": "light",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "suggestions" in data
    assert len(data["suggestions"]) >= 1


@pytest.mark.asyncio
async def test_get_week_schedule(client):
    """获取周日程"""
    token = await _register_and_get_token(client, "week@test.com")

    from datetime import date, timedelta
    # 找到本周一
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    resp = await client.get(f"/api/v1/schedule/week/{monday}", headers=_h(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "days" in data
    assert len(data["days"]) == 7
