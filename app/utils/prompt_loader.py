"""
Prompt 加载器 — 从 .md 文件中按强度档位加载精灵 Prompt

目录结构:
  prompts/
  ├── parser.md
  ├── scheduler.md
  ├── orchestrator.md
  ├── chat_to_task.md
  ├── weekly_analysis.md
  ├── spirit_comment.md
  └── spirits/
      ├── light.md       ← 内含 ## LOW / ## MID / ## HIGH 三节
      ├── water.md
      ├── soil.md
      ├── air.md
      └── nutrition.md

精灵 Prompt .md 文件格式约定:
  ───────────────────
  ## LOW
  这里是低强度的完整 system prompt...

  ## MID
  这里是中强度的完整 system prompt...

  ## HIGH
  这里是高强度的完整 system prompt...
  ───────────────────

强度映射:
  0-33   → LOW
  34-66  → MID
  67-100 → HIGH
"""
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# ===== 路径 =====
_BASE_DIR = Path(__file__).resolve().parent.parent  # app/
PROMPTS_DIR = _BASE_DIR / "ai" / "prompts"
SPIRITS_PROMPTS_DIR = PROMPTS_DIR / "spirits"

# ===== 强度档位 =====
INTENSITY_LEVELS = ("low", "mid", "high")

INTENSITY_THRESHOLDS = [
    (0, 33, "low"),
    (34, 66, "mid"),
    (67, 100, "high"),
]

# 节标题正则（匹配 ## LOW / ## MID / ## HIGH，大小写不敏感）
_SECTION_RE = re.compile(
    r"^##\s*(LOW|MID|HIGH)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def intensity_to_level(intensity: int) -> str:
    """
    将 0-100 的强度数值转换为档位字符串。

    >>> intensity_to_level(20)
    'low'
    >>> intensity_to_level(50)
    'mid'
    >>> intensity_to_level(85)
    'high'
    """
    intensity = max(0, min(100, intensity))
    for lo, hi, level in INTENSITY_THRESHOLDS:
        if lo <= intensity <= hi:
            return level
    return "mid"  # fallback


def _parse_sections(content: str) -> dict[str, str]:
    """
    解析 .md 文件内容，按 ## LOW / ## MID / ## HIGH 拆分为三节。
    返回 {"low": "...", "mid": "...", "high": "..."}。

    如果文件没有分节标记，则整个内容作为所有档位的通用 Prompt。
    """
    matches = list(_SECTION_RE.finditer(content))

    if not matches:
        # 没有分节 → 全文作为通用 Prompt
        stripped = content.strip()
        return {"low": stripped, "mid": stripped, "high": stripped}

    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        level = match.group(1).lower()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections[level] = content[start:end].strip()

    # 补全缺失档位：用最近的档位填充
    for level in INTENSITY_LEVELS:
        if level not in sections:
            # 优先用 mid，其次用任意已有的
            sections[level] = sections.get("mid", next(iter(sections.values()), ""))

    return sections


@lru_cache(maxsize=64)
def _load_file(filepath: str) -> str:
    """读取文件内容（带缓存）"""
    try:
        return Path(filepath).read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("prompt_file_not_found", path=filepath)
        return ""
    except Exception as e:
        logger.error("prompt_file_read_error", path=filepath, error=str(e))
        return ""


def load_spirit_prompt(spirit_code: str, intensity: int = 50) -> str:
    """
    加载精灵的强度分档 Prompt。

    Args:
        spirit_code: 精灵代码 (light/water/soil/air/nutrition)
        intensity:   强度值 0-100

    Returns:
        对应档位的 prompt 文本。如果文件不存在或为空，返回空字符串。
    """
    filepath = SPIRITS_PROMPTS_DIR / f"{spirit_code}.md"
    content = _load_file(str(filepath))
    if not content:
        return ""

    level = intensity_to_level(intensity)
    sections = _parse_sections(content)
    prompt = sections.get(level, "")

    logger.debug(
        "spirit_prompt_loaded",
        spirit=spirit_code,
        intensity=intensity,
        level=level,
        length=len(prompt),
    )
    return prompt


def load_prompt(name: str) -> str:
    """
    加载通用 Prompt（不分强度档位）。

    Args:
        name: Prompt 名称，对应 prompts/ 下的文件名（不含 .md 后缀）
              例如 "parser", "scheduler", "orchestrator"

    Returns:
        Prompt 文本。
    """
    filepath = PROMPTS_DIR / f"{name}.md"
    content = _load_file(str(filepath))
    return content.strip()


def reload_all():
    """
    清空缓存，强制重新加载所有 Prompt 文件。
    可用于开发时热重载。
    """
    _load_file.cache_clear()
    logger.info("prompt_cache_cleared")


def get_available_prompts() -> dict:
    """
    列出所有已存在的 Prompt 文件（调试用）。
    """
    result = {"general": [], "spirits": []}

    if PROMPTS_DIR.exists():
        for f in PROMPTS_DIR.glob("*.md"):
            result["general"].append(f.stem)

    if SPIRITS_PROMPTS_DIR.exists():
        for f in SPIRITS_PROMPTS_DIR.glob("*.md"):
            result["spirits"].append(f.stem)

    return result
