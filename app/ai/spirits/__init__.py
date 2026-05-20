"""
精灵 Agent 工厂
"""
from app.ai.spirits.base import BaseSpirit
from app.ai.spirits.light import LightSpirit
from app.ai.spirits.water import WaterSpirit
from app.ai.spirits.soil import SoilSpirit
from app.ai.spirits.air import AirSpirit
from app.ai.spirits.nutrition import NutritionSpirit

# 精灵注册表
SPIRIT_REGISTRY: dict[str, BaseSpirit] = {
    "light": LightSpirit(),
    "water": WaterSpirit(),
    "soil": SoilSpirit(),
    "air": AirSpirit(),
    "nutrition": NutritionSpirit(),
}


def get_spirit(code: str) -> BaseSpirit:
    """获取精灵实例"""
    spirit = SPIRIT_REGISTRY.get(code)
    if not spirit:
        raise ValueError(f"未知精灵代码: {code}，支持: {list(SPIRIT_REGISTRY.keys())}")
    return spirit


VALID_SPIRIT_CODES = set(SPIRIT_REGISTRY.keys())
