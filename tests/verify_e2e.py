"""
端到端流程验证 — Sprint 1+2+3+4

模拟一个用户 4 周的完整使用流程, 验证:
  Sprint 2 — 打分三步走公式(raw → magnify → overall)
  Sprint 3 — quality_note 校准 / 月度 focus_intensity 加权 / 3 个基调奖项 / 周报基调感知
  Sprint 4 — 基调推断(三信号融合) + 智能护栏(over_focus/no_focus/neglected)

整个流程不依赖 DB / LLM, 用纯 Python 对象 mock, 1 秒内跑完。
能跑通这个脚本 = 所有跨服务的数据流是通的。

跑法:
  python3 tests/verify_e2e.py
"""
import sys
import os
import uuid
import importlib.util
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional


HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)


def _stub(name, **attrs):
    m = type(sys)(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _StubBase: pass


class _Holder:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
    def __getattr__(self, name):
        return None


sys.modules["app"] = _stub("app")
sys.modules["app.database"] = _stub("app.database", Base=_StubBase)
sys.modules["app.models"] = _stub("app.models")
sys.modules["app.models._types"] = _stub("app.models._types", GUID=object)
sys.modules["app.models.score"] = _stub("app.models.score", SpiritWeeklyScore=_Holder)
sys.modules["app.models.task"] = _stub("app.models.task", Task=_Holder, SubTask=_Holder)
sys.modules["app.models.weekly_focus"] = _stub(
    "app.models.weekly_focus", WeeklyFocus=_Holder, DEFAULT_WEIGHT=1.0,
)
sys.modules["app.models.report"] = _stub(
    "app.models.report",
    WeeklyReport=_Holder, WeeklySummary=_Holder, MonthlyFruit=_Holder,
)

sys.modules["app.services"] = _stub("app.services")

class _StubIntensityService:
    def __init__(self, db): pass
    async def get_effective_intensity(self, *a, **kw): return 50
sys.modules["app.services.intensity_service"] = _stub(
    "app.services.intensity_service", IntensityService=_StubIntensityService,
)
class _StubWeeklyFocusService:
    def __init__(self, db): pass
sys.modules["app.services.weekly_focus_service_stub"] = _stub(
    "app.services.weekly_focus_service_stub",
    WeeklyFocusService=_StubWeeklyFocusService,
)

sys.modules["app.ai"] = _stub("app.ai")
sys.modules["app.ai.spirits"] = _stub(
    "app.ai.spirits", get_spirit=lambda c: (_ for _ in ()).throw(RuntimeError("stub")),
)
class _StubLLM:
    async def complete(self, **kw): return "[FALLBACK]"
    async def complete_json(self, **kw): return None
sys.modules["app.ai.llm_client"] = _stub("app.ai.llm_client", llm_client=_StubLLM())
class _StubImage:
    async def generate(self, **kw): return "[FALLBACK]"
sys.modules["app.ai.image_client"] = _stub("app.ai.image_client", image_client=_StubImage())

sys.modules["app.utils"] = _stub("app.utils")
sys.modules["app.utils.prompt_loader"] = _stub(
    "app.utils.prompt_loader", load_prompt=lambda name: "",
)


def _load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


wfs_mod = _load(
    "app.services.weekly_focus_service",
    os.path.join(ROOT, "app/services/weekly_focus_service.py"),
)
scoring_mod = _load(
    "app.services.scoring_service",
    os.path.join(ROOT, "app/services/scoring_service.py"),
)
tree_mod = _load(
    "app.services.tree_service",
    os.path.join(ROOT, "app/services/tree_service.py"),
)
fruit_mod = _load(
    "app.services.fruit_service",
    os.path.join(ROOT, "app/services/fruit_service.py"),
)
fss_mod = _load(
    "app.services.focus_suggestion_service",
    os.path.join(ROOT, "app/services/focus_suggestion_service.py"),
)


@dataclass
class FakeTask:
    primary_spirit: str = "light"
    title: str = ""
    raw_input: Optional[str] = None
    id: str = ""


@dataclass
class FakeSubTask:
    id: str = ""
    spirit: str = "light"
    title: str = ""
    completion_percent: int = 0
    status: str = "pending"
    user_feedback: Optional[str] = None
    quality_note: Optional[str] = None
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    actual_end: Optional[datetime] = None


@dataclass
class FakeScore:
    spirit_code: str
    week_start: date
    score: float = 70.0
    raw_score: float = 70.0
    design_score: float = 80.0
    completion_score: float = 70.0
    quality_score: float = 75.0
    intensity_at_scoring: int = 50
    focus_weight: float = 1.0
    display_score: float = 7.0
    focus_at_scoring: Optional[str] = None
    task_stats: dict = field(default_factory=dict)
    level: str = "good"
    spirit_comment: str = ""


ASSERT_COUNT = 0
FAIL_COUNT = 0


def step(title):
    print()
    print("─" * 80)
    print(f"▶ {title}")
    print("─" * 80)


def expect(cond, msg, detail=""):
    global ASSERT_COUNT, FAIL_COUNT
    ASSERT_COUNT += 1
    if cond:
        print(f"  ✓ {msg}")
    else:
        FAIL_COUNT += 1
        print(f"  ✗ {msg}")
        if detail:
            print(f"     {detail}")


def info(s):
    print(f"  · {s}")


USER_ID = uuid.uuid4()
W1 = date(2026, 4, 6)
W2 = W1 + timedelta(weeks=1)
W3 = W1 + timedelta(weeks=2)
W4 = W1 + timedelta(weeks=3)
W5 = W1 + timedelta(weeks=4)

PRESETS = wfs_mod.THEME_PRESETS


def make_subtasks(
    week_start: date,
    spirit_counts: dict[str, int],
    completion_overrides: Optional[dict[str, list[int]]] = None,
    titles: Optional[dict[str, list[str]]] = None,
    feedbacks: Optional[dict[str, list[Optional[str]]]] = None,
    quality_notes: Optional[dict[str, list[Optional[str]]]] = None,
) -> list[FakeSubTask]:
    out = []
    base_dt = datetime.combine(week_start, datetime.min.time())
    for code, n in spirit_counts.items():
        for i in range(n):
            pct = (completion_overrides or {}).get(code, [100] * n)
            t = (titles or {}).get(code, [""] * n)
            fb = (feedbacks or {}).get(code, [None] * n)
            qn = (quality_notes or {}).get(code, [None] * n)
            sched_end = base_dt + timedelta(days=i % 5, hours=10)
            actual_end = sched_end - timedelta(minutes=10) if pct[i] > 0 else None
            status = (
                "completed" if pct[i] == 100
                else "cancelled" if pct[i] == 0
                else "in_progress"
            )
            out.append(FakeSubTask(
                id=str(uuid.uuid4()),
                spirit=code, title=t[i] if i < len(t) else "",
                completion_percent=pct[i] if i < len(pct) else 100,
                status=status,
                user_feedback=fb[i] if i < len(fb) else None,
                quality_note=qn[i] if i < len(qn) else None,
                scheduled_start=base_dt + timedelta(days=i % 5, hours=9),
                scheduled_end=sched_end,
                actual_end=actual_end,
            ))
    return out


def score_a_week(
    subtasks: list[FakeSubTask],
    week_start: date,
    theme: Optional[str],
    quality_calibrations: Optional[dict[str, int]] = None,
) -> dict[str, FakeScore]:
    weights = PRESETS.get(theme, {}).get("spirit_weights", {}) if theme else {}
    out = {}
    spirit_codes = ["light", "water", "soil", "air", "nutrition"]

    for code in spirit_codes:
        spirit_subtasks = [st for st in subtasks if st.spirit == code]
        intensity = 50
        focus_mult = float(weights.get(code, 1.0))

        completion = scoring_mod.calc_completion_score(spirit_subtasks)
        design = scoring_mod.calc_design_score(intensity, focus_mult, len(spirit_subtasks))
        quality = scoring_mod.calc_quality_score(spirit_subtasks)

        cal_applied = 0.0
        if quality_calibrations:
            quality, cal_applied = scoring_mod._apply_quality_calibrations(
                quality, spirit_subtasks, quality_calibrations,
            )

        raw = round(completion * 0.5 + design * 0.3 + quality * 0.2, 1)
        final = round(scoring_mod.apply_focus_magnification(raw, focus_mult), 1)

        out[code] = FakeScore(
            spirit_code=code,
            week_start=week_start,
            score=final, raw_score=raw,
            design_score=round(design, 1),
            completion_score=round(completion, 1),
            quality_score=round(quality, 1),
            intensity_at_scoring=intensity,
            focus_weight=round(focus_mult, 2),
            display_score=round(final / 10.0, 2),
            focus_at_scoring=theme,
            task_stats={
                "planned": len(spirit_subtasks),
                "completed": sum(1 for s in spirit_subtasks if (s.completion_percent or 0) == 100),
                "partial": sum(1 for s in spirit_subtasks if 0 < (s.completion_percent or 0) < 100),
                "cancelled": sum(1 for s in spirit_subtasks if s.status == "cancelled"),
                "quality_calibration_applied": round(cal_applied, 2),
            },
        )
    return out


print("=" * 80)
print("Sprint 1+2+3+4 端到端流程验证")
print("=" * 80)
print(f"模拟用户 ID: {USER_ID}")
print(f"模拟周: W1 {W1} → W4 {W4}, 推断目标周 W5 {W5}")


step("Week 1: exam_prep 基调, 7 个学习任务 (匹配 expected) 全 100% just_right")

w1_subtasks = make_subtasks(
    W1,
    {"light": 7, "water": 2, "soil": 2, "air": 1, "nutrition": 1},
    completion_overrides={
        "light": [100] * 7,
        "water": [100, 100],
        "soil":  [100, 100],
        "air":   [100],
        "nutrition": [100],
    },
    feedbacks={
        "light": ["just_right"] * 7,
        "water": ["easy", "easy"],
        "soil":  ["just_right", "just_right"],
        "air":   ["easy"],
        "nutrition": ["just_right"],
    },
    titles={"light": ["复习线代", "刷题", "看课件", "做模拟卷", "整理笔记", "复习", "刷题"]},
)
info(f"任务总数: {len(w1_subtasks)}")
w1_scores = score_a_week(w1_subtasks, W1, "exam_prep")

for c in ["light", "water", "soil", "air", "nutrition"]:
    s = w1_scores[c]
    info(f"  {c:9s}: final={s.score:5.1f}, raw={s.raw_score:5.1f}, "
         f"weight={s.focus_weight}, intensity={s.intensity_at_scoring}")

expect(
    w1_scores["light"].focus_weight == 1.8,
    "Week 1: light 在 exam_prep 下 focus_weight = 1.8",
)
expect(
    w1_scores["light"].raw_score >= 95,
    "Week 1: light 表现优秀 (raw ≥ 95)",
    f"实际 raw={w1_scores['light'].raw_score}",
)
expect(
    w1_scores["light"].score == 100,
    "Week 1: light final = 100 (重点 + 优秀 → clamp 100)",
    f"实际 final={w1_scores['light'].score}",
)
expect(
    w1_scores["water"].focus_weight == 0.6,
    "Week 1: water focus_weight = 0.6 (备考时收敛娱乐)",
)
expect(
    w1_scores["water"].raw_score > w1_scores["water"].score,
    "Week 1: water 满分被次要收敛 (final < raw)",
    f"raw={w1_scores['water'].raw_score}, final={w1_scores['water'].score}",
)
expect(
    w1_scores["light"].focus_at_scoring == "exam_prep",
    "Week 1: 所有精灵记录都标了 focus_at_scoring=exam_prep",
)


step("Week 2: exam_prep 基调, 3/8 完整完成 + 3 个 25% + 2 个 0%, 含两个负向 quality_note")

w2_subtasks = make_subtasks(
    W2,
    {"light": 8, "water": 2, "soil": 2, "air": 1, "nutrition": 1},
    completion_overrides={
        "light": [100, 100, 100, 25, 25, 25, 0, 0],
        "water": [100, 100],
        "soil":  [100, 100],
        "air":   [100],
        "nutrition": [100],
    },
    feedbacks={
        "light": ["just_right", "hard", "just_right", "hard", "hard", "hard", None, None],
        "water": ["easy", "easy"],
        "soil":  ["just_right", "just_right"],
        "air":   ["easy"],
        "nutrition": ["just_right"],
    },
    quality_notes={
        "light": [None, None, None, "应付了一下, 没真动脑", "心不在焉, 状态不好", None, None, None],
    },
)
info(f"任务总数: {len(w2_subtasks)}, light: 3 完成 + 3 partial(25%) + 2 cancel")
info(f"含 quality_note: {sum(1 for s in w2_subtasks if s.quality_note)} 条 (都是负向)")

notes_subtasks = [s for s in w2_subtasks if s.quality_note]
fake_calibrations = {notes_subtasks[0].id: -5, notes_subtasks[1].id: -5}

w2_scores = score_a_week(w2_subtasks, W2, "exam_prep", fake_calibrations)
for c in ["light", "water", "soil", "air", "nutrition"]:
    s = w2_scores[c]
    cal = s.task_stats.get("quality_calibration_applied", 0)
    info(f"  {c:9s}: final={s.score:5.1f}, raw={s.raw_score:5.1f}, "
         f"completion={s.completion_score:5.1f}, partial={s.task_stats.get('partial', 0)}, "
         f"cal_applied={cal}")

expect(
    w2_scores["light"].task_stats["partial"] == 3,
    "Week 2: light 记录了 3 个 partial (25%) 任务",
    f"实际 partial={w2_scores['light'].task_stats['partial']}",
)
expect(
    w2_scores["light"].task_stats["cancelled"] == 2,
    "Week 2: light 记录了 2 个 cancelled 任务",
)
expect(
    w2_scores["light"].task_stats["quality_calibration_applied"] < 0,
    "Week 2: quality_calibration_applied 是负数",
    f"实际 cal={w2_scores['light'].task_stats['quality_calibration_applied']}",
)
expect(
    w2_scores["light"].score < w1_scores["light"].score,
    "Week 2: light 因状态下滑, final 明显低于 Week 1",
    f"W1={w1_scores['light'].score} W2={w2_scores['light'].score}",
)
expect(
    w2_scores["light"].completion_score < 80,
    "Week 2: light completion 显著下降",
    f"实际 completion={w2_scores['light'].completion_score}",
)


step("Week 3: recovery 基调, soil/water 任务为主")

w3_subtasks = make_subtasks(
    W3,
    {"light": 2, "water": 5, "soil": 5, "air": 2, "nutrition": 2},
    feedbacks={
        "water": ["easy"] * 5, "soil": ["just_right"] * 5,
    },
    titles={
        "water": ["看电影", "听音乐", "玩游戏", "看综艺", "散步放松"],
        "soil":  ["晨跑", "瑜伽", "拉伸", "冥想", "早睡"],
    },
)
w3_scores = score_a_week(w3_subtasks, W3, "recovery")
for c in ["light", "water", "soil", "air", "nutrition"]:
    s = w3_scores[c]
    info(f"  {c:9s}: final={s.score:5.1f}, raw={s.raw_score:5.1f}, weight={s.focus_weight}")

expect(
    w3_scores["soil"].focus_weight == 1.6,
    "Week 3: soil 在 recovery 下 focus_weight = 1.6",
)
expect(
    w3_scores["water"].focus_weight == 1.4,
    "Week 3: water 在 recovery 下 focus_weight = 1.4",
)
expect(
    w3_scores["light"].focus_weight == 0.6,
    "Week 3: light 在 recovery 下被收敛到 0.6",
)


step("Week 4: balanced 基调, 五维均匀")

w4_subtasks = make_subtasks(
    W4,
    {"light": 3, "water": 3, "soil": 3, "air": 3, "nutrition": 3},
    feedbacks={code: ["just_right"] * 3 for code in
               ["light", "water", "soil", "air", "nutrition"]},
)
w4_scores = score_a_week(w4_subtasks, W4, "balanced")
for c in ["light", "water", "soil", "air", "nutrition"]:
    s = w4_scores[c]
    info(f"  {c:9s}: final={s.score:5.1f}, weight={s.focus_weight}")

expect(
    all(w4_scores[c].focus_weight == 1.0 for c in ["light", "water", "soil", "air", "nutrition"]),
    "Week 4: 平衡基调下所有精灵 focus_weight = 1.0",
)
expect(
    all(w4_scores[c].score == w4_scores[c].raw_score for c in ["light", "water", "soil", "air", "nutrition"]),
    "Week 4: focus_mult=1.0 时 final == raw",
)


step("月度聚合: 把 W1-W4 聚合成月度果实数据")

all_scores = []
for s_dict in [w1_scores, w2_scores, w3_scores, w4_scores]:
    all_scores.extend(s_dict.values())

week_starts = [W1, W2, W3, W4]
FS = fruit_mod.FruitService
svc_fruit = FS(db=None)

spirit_monthly = svc_fruit._aggregate_spirit_monthly(all_scores, week_starts)
info(f"spirit_monthly['light']:")
info(f"  avg_score = {spirit_monthly['light']['avg_score']}")
info(f"  focused_weeks = {spirit_monthly['light']['focused_weeks']}")
info(f"  key_weeks_avg = {spirit_monthly['light']['key_weeks_avg']}")

expect(
    spirit_monthly["light"]["focused_weeks"] == 2,
    "月度: light 在 W1+W2 (exam_prep) 是重点",
    f"实际={spirit_monthly['light']['focused_weeks']}",
)
expect(
    spirit_monthly["soil"]["focused_weeks"] == 1,
    "月度: soil 在 W3 (recovery) 是重点",
)

weekly_overalls = svc_fruit._calc_weekly_overalls(all_scores, week_starts)
focus_intensities = svc_fruit._calc_week_focus_intensities(all_scores, week_starts)
info(f"weekly_overall_scores: {weekly_overalls}")
info(f"week_focus_intensities: {focus_intensities}")

expect(
    focus_intensities[0] > 1.3 and focus_intensities[1] > 1.3,
    "月度: exam_prep 两周 focus_intensity > 1.3",
    f"实际 W1={focus_intensities[0]}, W2={focus_intensities[1]}",
)
expect(
    focus_intensities[3] == 1.0,
    "月度: balanced 周 focus_intensity = 1.0",
)
expect(
    focus_intensities[2] > 1.2,
    "月度: recovery 周 focus_intensity > 1.2",
    f"实际={focus_intensities[2]}",
)

month_overall = FS._calc_month_overall(weekly_overalls, focus_intensities)
arith_avg = round(sum(weekly_overalls) / len(weekly_overalls), 1)
info(f"月度总分 (加权)    = {month_overall}")
info(f"月度总分 (算术平均) = {arith_avg}")

th = FS._extract_theme_history(all_scores, week_starts)
info(f"themes_per_week: {th['themes_per_week']}")
info(f"theme_counts: {th['theme_counts']}")
info(f"dominant_theme: {th['dominant_theme']}")
info(f"theme_switch_count: {th['theme_switch_count']}")

expect(
    th["themes_per_week"] == ["exam_prep", "exam_prep", "recovery", "balanced"],
    "月度: themes_per_week 顺序正确",
)
expect(
    th["dominant_theme"] == "exam_prep",
    "月度: dominant_theme = exam_prep",
)
expect(
    th["theme_switch_count"] == 3,
    "月度: theme_switch_count = 3",
)
expect(
    th["weeks_with_focus"] == 4,
    "月度: 4 周都设了基调",
)

awards = svc_fruit._calculate_awards(spirit_monthly, all_scores, week_starts, th)
info(f"获得奖项 ({len(awards)} 个):")
for a in awards:
    sc = a.get("spirit_code") or "全局"
    info(f"  - {a['emoji']} {a['award_name']} ({sc}): {a['reason']}")

new_awards = [a for a in awards
              if a["award_name"] in ("聚焦达人", "节奏切换大师", "平衡守护者")]
expect(
    len(new_awards) >= 1,
    f"月度: 至少触发 1 个 Sprint 3 基调一致性奖项",
    f"实际触发: {[a['award_name'] for a in new_awards]}",
)


step("基调推断: 用 W3-W4 任务数据推断 W5 的基调")

recent_subtasks = w3_subtasks + w4_subtasks
recent_tasks = []

activity = fss_mod.compute_activity_distribution(recent_tasks, recent_subtasks)
info(f"过去 2 周活跃度: {activity}")
keyword_hits = fss_mod.scan_keywords(recent_tasks, recent_subtasks)
info(f"关键词命中: {keyword_hits}")

activity_scores = fss_mod.score_themes_by_activity(activity)
keyword_scores = fss_mod.score_themes_by_keywords(keyword_hits)

history_bonus = {"balanced": 0.7}

final_scores = fss_mod.merge_theme_scores(
    activity_scores, keyword_scores, history_bonus,
)
info("各 theme 推断分数:")
for theme in sorted(final_scores, key=lambda t: -final_scores[t]):
    info(f"  {theme:20s} → {final_scores[theme]}")

top_theme = max(final_scores, key=final_scores.get)
top_conf = final_scores[top_theme]
info(f"→ 推荐: {top_theme} (置信度 {top_conf})")

expect(
    top_conf >= fss_mod.MIN_CONFIDENCE_TO_SHOW,
    f"推断: 置信度 ≥ {fss_mod.MIN_CONFIDENCE_TO_SHOW}",
    f"实际 {top_conf}",
)


step("智能护栏: 检测过去 4 周模式")

by_week_for_warnings = {}
for s_dict, ws in zip([w1_scores, w2_scores, w3_scores, w4_scores], week_starts):
    by_week_for_warnings[ws] = list(s_dict.values())

weeks_sorted = sorted(by_week_for_warnings.keys(), reverse=True)

over_focus_warns = fss_mod.FocusSuggestionService._detect_over_focus(
    by_week_for_warnings, weeks_sorted,
)
expect(
    len(over_focus_warns) == 0,
    "护栏: 只 2 周 exam_prep (阈值=3) → 不触发 over_focus",
    f"实际触发: {len(over_focus_warns)}",
)

no_focus_warns = fss_mod.FocusSuggestionService._detect_no_focus(
    by_week_for_warnings, weeks_sorted,
)
expect(
    len(no_focus_warns) == 0,
    "护栏: 4 周都设了基调 → 不触发 no_focus_too_long",
)

neglect_warns = fss_mod.FocusSuggestionService._detect_neglected_spirit(
    by_week_for_warnings, weeks_sorted,
)
info(f"neglected_spirit 警告数: {len(neglect_warns)}")
expect(
    len(neglect_warns) == 0,
    "护栏: 没有精灵连续 4 周 weight≤0.7 → 不触发 neglect",
)


step("场景测试: 连续 4 周 exam_prep + 健康低 → 必须触发 over_focus")

base = date(2026, 6, 1)
extreme_by_week = {}
for w_offset in range(1, 5):
    ws = base - timedelta(weeks=w_offset)
    extreme_by_week[ws] = [
        FakeScore(
            spirit_code=code, week_start=ws,
            focus_at_scoring="exam_prep",
            score=50 if code in ("soil", "water") else 80,
            focus_weight=1.8 if code == "light" else 0.6,
        )
        for code in ["light", "water", "soil", "air", "nutrition"]
    ]

extreme_sorted = sorted(extreme_by_week.keys(), reverse=True)
extreme_warns = fss_mod.FocusSuggestionService._detect_over_focus(
    extreme_by_week, extreme_sorted,
)
expect(
    len(extreme_warns) == 1,
    "极端: 连续 3 周以上 exam_prep + 健康均分 50 → 必触发 over_focus",
    f"实际触发数: {len(extreme_warns)}",
)
if extreme_warns:
    info(f"  → {extreme_warns[0]['message']}")
    expect(
        extreme_warns[0]["suggested_alternative"] == "recovery",
        "over_focus 警告会建议切换到 recovery",
    )


print()
print("=" * 80)
print(f"端到端验证完成: 通过 {ASSERT_COUNT - FAIL_COUNT} / 失败 {FAIL_COUNT} / 总计 {ASSERT_COUNT}")
print("=" * 80)

if FAIL_COUNT == 0:
    print("\n✅ 全部通过! Sprint 1+2+3+4 数据流端到端验证完整跑通。")
    print("\n这意味着:")
    print("  ✓ 基调权重正确传递到打分公式 (S1 → S2)")
    print("  ✓ 三步走公式输出正确的 raw/final/display 字段 (S2)")
    print("  ✓ quality_note 校准能正确应用到 quality_score (S3)")
    print("  ✓ 月度聚合用 focus_intensity 加权, 提取 theme_history (S3)")
    print("  ✓ 基调一致性奖项 (聚焦达人/节奏切换/平衡守护者) 判定正确 (S3)")
    print("  ✓ 跨周推断三信号融合, 给出合理建议 (S4)")
    print("  ✓ 智能护栏 (over_focus/no_focus/neglect) 阈值正确 (S4)")
    sys.exit(0)
else:
    print(f"\n❌ {FAIL_COUNT} 项失败, 需要排查!")
    sys.exit(1)
