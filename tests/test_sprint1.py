"""
Sprint 1 单测

覆盖:
  1. WeeklyFocusUpsertRequest 校验 (周一/范围/未知精灵/custom 必填 label)
  2. SubTaskCompletionUpdateRequest 校验 (离散档位)
  3. WeeklyFocusService 主流程 (upsert / get / get_or_default_weights / delete)
  4. THEME_PRESETS 完整性 (5 个精灵全有, 权重在范围内)

不依赖真实数据库, 使用 sqlite in-memory + Base.metadata.create_all。
运行:
  pytest tests/test_sprint1.py -v
"""
import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# 必须先 import 所有模型, 让 Base.metadata 包含它们
import app.models  # noqa: F401
from app.database import Base
from app.schemas.weekly_focus import WeeklyFocusUpsertRequest
from app.schemas.task import SubTaskCompletionUpdateRequest
from app.services.weekly_focus_service import (
    WeeklyFocusService,
    THEME_PRESETS,
    SPIRIT_CODES,
    get_week_start,
)


# ====================================================================
#  Fixture: 临时内存 DB
# ====================================================================

@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session

    await engine.dispose()


# ====================================================================
#  Schema 校验
# ====================================================================

class TestWeeklyFocusSchema:

    def _base_payload(self, **overrides):
        monday = get_week_start()
        payload = {
            "week_start": monday,
            "theme": "balanced",
            "spirit_weights": {
                "light": 1.0, "water": 1.0, "soil": 1.0,
                "air": 1.0, "nutrition": 1.0,
            },
            "key_spirits": [],
        }
        payload.update(overrides)
        return payload

    def test_valid_balanced(self):
        req = WeeklyFocusUpsertRequest(**self._base_payload())
        assert req.theme == "balanced"
        assert all(w == 1.0 for w in req.spirit_weights.values())

    def test_week_start_must_be_monday(self):
        tuesday = get_week_start() + timedelta(days=1)
        with pytest.raises(ValidationError, match="周一"):
            WeeklyFocusUpsertRequest(**self._base_payload(week_start=tuesday))

    def test_theme_invalid(self):
        with pytest.raises(ValidationError):
            WeeklyFocusUpsertRequest(**self._base_payload(theme="not_a_theme"))

    def test_weight_out_of_range_low(self):
        weights = {c: 1.0 for c in SPIRIT_CODES}
        weights["light"] = 0.3  # 低于 0.5
        with pytest.raises(ValidationError, match="超出范围"):
            WeeklyFocusUpsertRequest(**self._base_payload(spirit_weights=weights))

    def test_weight_out_of_range_high(self):
        weights = {c: 1.0 for c in SPIRIT_CODES}
        weights["light"] = 2.5  # 超过 2.0
        with pytest.raises(ValidationError, match="超出范围"):
            WeeklyFocusUpsertRequest(**self._base_payload(spirit_weights=weights))

    def test_weight_unknown_spirit(self):
        weights = {c: 1.0 for c in SPIRIT_CODES}
        weights["unknown"] = 1.0
        with pytest.raises(ValidationError, match="未知精灵"):
            WeeklyFocusUpsertRequest(**self._base_payload(spirit_weights=weights))

    def test_weight_missing_spirits_filled_default(self):
        """缺失的精灵应该自动补 1.0"""
        weights = {"light": 1.8}  # 只传一个
        req = WeeklyFocusUpsertRequest(**self._base_payload(spirit_weights=weights))
        assert req.spirit_weights["light"] == 1.8
        assert req.spirit_weights["water"] == 1.0  # 自动补默认

    def test_key_spirits_too_many(self):
        with pytest.raises(ValidationError):
            WeeklyFocusUpsertRequest(**self._base_payload(
                key_spirits=["light", "soil", "water"]
            ))

    def test_key_spirits_invalid_code(self):
        with pytest.raises(ValidationError, match="不在合法"):
            WeeklyFocusUpsertRequest(**self._base_payload(
                key_spirits=["light", "unknown"]
            ))

    def test_key_spirits_dedup(self):
        req = WeeklyFocusUpsertRequest(**self._base_payload(
            key_spirits=["light", "light"]
        ))
        assert req.key_spirits == ["light"]

    def test_custom_theme_requires_label(self):
        with pytest.raises(ValidationError, match="custom_label"):
            WeeklyFocusUpsertRequest(**self._base_payload(
                theme="custom", custom_label=None
            ))

    def test_custom_theme_with_label_ok(self):
        req = WeeklyFocusUpsertRequest(**self._base_payload(
            theme="custom", custom_label="备考周"
        ))
        assert req.theme == "custom"
        assert req.custom_label == "备考周"


class TestSubTaskCompletionSchema:

    def test_valid_percents(self):
        for p in (0, 25, 50, 75, 100):
            req = SubTaskCompletionUpdateRequest(completion_percent=p)
            assert req.completion_percent == p

    def test_invalid_percent(self):
        with pytest.raises(ValidationError):
            SubTaskCompletionUpdateRequest(completion_percent=33)
        with pytest.raises(ValidationError):
            SubTaskCompletionUpdateRequest(completion_percent=-1)
        with pytest.raises(ValidationError):
            SubTaskCompletionUpdateRequest(completion_percent=101)

    def test_invalid_feedback(self):
        with pytest.raises(ValidationError):
            SubTaskCompletionUpdateRequest(
                completion_percent=100, user_feedback="amazing"
            )

    def test_valid_feedback(self):
        for fb in ("easy", "just_right", "hard"):
            req = SubTaskCompletionUpdateRequest(
                completion_percent=100, user_feedback=fb
            )
            assert req.user_feedback == fb


# ====================================================================
#  Preset 完整性
# ====================================================================

class TestThemePresets:

    def test_all_themes_have_full_spirit_set(self):
        for theme, preset in THEME_PRESETS.items():
            weights = preset["spirit_weights"]
            assert set(weights.keys()) == set(SPIRIT_CODES), (
                f"主题 {theme} 缺少精灵: {set(SPIRIT_CODES) - set(weights.keys())}"
            )

    def test_all_weights_in_valid_range(self):
        for theme, preset in THEME_PRESETS.items():
            for code, w in preset["spirit_weights"].items():
                assert 0.5 <= w <= 2.0, (
                    f"主题 {theme} 的 {code} 权重 {w} 越界"
                )

    def test_key_spirits_subset_of_all(self):
        for theme, preset in THEME_PRESETS.items():
            for k in preset["key_spirits"]:
                assert k in SPIRIT_CODES, f"主题 {theme} 的 key_spirits 含未知 {k}"

    def test_balanced_is_neutral(self):
        bal = THEME_PRESETS["balanced"]
        assert all(w == 1.0 for w in bal["spirit_weights"].values())
        assert bal["key_spirits"] == []


# ====================================================================
#  Service 主流程
# ====================================================================

@pytest.mark.asyncio
class TestWeeklyFocusService:

    USER_ID = uuid.uuid4()

    async def test_get_focus_none_initially(self, db_session):
        svc = WeeklyFocusService(db_session)
        focus = await svc.get_focus(self.USER_ID, get_week_start())
        assert focus is None

    async def test_get_default_weights_when_unset(self, db_session):
        svc = WeeklyFocusService(db_session)
        weights = await svc.get_or_default_weights(self.USER_ID)
        assert weights == {c: 1.0 for c in SPIRIT_CODES}

    async def test_upsert_creates_then_updates(self, db_session):
        svc = WeeklyFocusService(db_session)
        ws = get_week_start()
        preset = THEME_PRESETS["exam_prep"]

        # 创建
        f1 = await svc.upsert_focus(
            user_id=self.USER_ID,
            week_start=ws,
            theme="exam_prep",
            spirit_weights=preset["spirit_weights"],
            key_spirits=preset["key_spirits"],
            reason="周六考试",
        )
        assert f1.theme == "exam_prep"
        assert f1.spirit_weights["light"] == 1.8

        # 同一周再 upsert → 覆盖, 不新建
        f2 = await svc.upsert_focus(
            user_id=self.USER_ID,
            week_start=ws,
            theme="recovery",
            spirit_weights=THEME_PRESETS["recovery"]["spirit_weights"],
            key_spirits=THEME_PRESETS["recovery"]["key_spirits"],
            reason="周一感冒了改成休整",
        )
        assert f2.id == f1.id  # 同一条
        assert f2.theme == "recovery"
        assert f2.reason == "周一感冒了改成休整"

    async def test_get_or_default_after_set(self, db_session):
        svc = WeeklyFocusService(db_session)
        ws = get_week_start()
        await svc.upsert_focus(
            user_id=self.USER_ID,
            week_start=ws,
            theme="exam_prep",
            spirit_weights=THEME_PRESETS["exam_prep"]["spirit_weights"],
            key_spirits=["light"],
        )
        weights = await svc.get_or_default_weights(self.USER_ID, ws)
        assert weights["light"] == 1.8
        assert weights["water"] < 1.0  # 备考期娱乐被压

    async def test_delete_focus(self, db_session):
        svc = WeeklyFocusService(db_session)
        ws = get_week_start()
        await svc.upsert_focus(
            user_id=self.USER_ID,
            week_start=ws,
            theme="balanced",
            spirit_weights=THEME_PRESETS["balanced"]["spirit_weights"],
            key_spirits=[],
        )
        ok = await svc.delete_focus(self.USER_ID, ws)
        assert ok is True

        # 二次删除应返回 False
        ok2 = await svc.delete_focus(self.USER_ID, ws)
        assert ok2 is False

        # 删除后回到默认权重
        weights = await svc.get_or_default_weights(self.USER_ID, ws)
        assert weights == {c: 1.0 for c in SPIRIT_CODES}

    async def test_focus_snapshot_when_unset(self, db_session):
        svc = WeeklyFocusService(db_session)
        snap = await svc.get_focus_snapshot(self.USER_ID, get_week_start())
        assert snap["theme"] is None
        assert snap["key_spirits"] == []
        assert snap["weights"] == {c: 1.0 for c in SPIRIT_CODES}

    async def test_focus_snapshot_when_set(self, db_session):
        svc = WeeklyFocusService(db_session)
        ws = get_week_start()
        preset = THEME_PRESETS["project_sprint"]
        await svc.upsert_focus(
            user_id=self.USER_ID,
            week_start=ws,
            theme="project_sprint",
            spirit_weights=preset["spirit_weights"],
            key_spirits=preset["key_spirits"],
        )
        snap = await svc.get_focus_snapshot(self.USER_ID, ws)
        assert snap["theme"] == "project_sprint"
        assert snap["label"] == "项目冲刺"
        assert "light" in snap["key_spirits"]
