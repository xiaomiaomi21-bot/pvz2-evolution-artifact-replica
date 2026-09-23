#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

MASK32 = 0xFFFFFFFF
MT_N, MT_M, MT_A = 624, 397, 0x9908B0DF
DEFAULT_SEED = 5489

# 植物名兼容别名：关卡/运行时数据有时使用缩写，属性对象使用完整名称。
# 内部比较统一使用去掉下划线的小写形式。
PLANT_NAME_ALIASES = {
    "dmdragonfruit": "darkmatterdragonfruit",
}


# 全局黑名单：所有关卡和世界均不允许作为神器候选输出的固定植物。
GLOBAL_BLACKLIST = {
            "coffeebean", "pumpkin",
            "powervine", "peavine",
            "aquavine", "bashopult",
            "bitpeashooter", "frog",
            "gloomvine", "intensivecarrot",
            "poisonvine", "pyrevine",
            "shinevine", "cobcannon",
            "carrotmissile", "dragonbabybruit",
            "hollybarrierleaf", "smallcherry",
            "smallexplodeonut", "flattenedshroom",
        }

#读取文件：返回项目根目录。
def project_root() -> Path:
    return Path(__file__).resolve().parent


#读取文件：返回规则文件默认路径。
def default_paths(root: Path | None = None) -> dict[str, Path]:
    rule_dir = (root or project_root()) / "规则"
    return {"rule_dir": rule_dir, "properties": rule_dir / "PROPERTYSHEETS.JSON", "artifact": rule_dir / "ARTIFACT.JSON", "levelmodules": rule_dir / "LEVELMODULES.JSON", "leveleditor": rule_dir / "LEVELEDITOR.JSON", "levels": rule_dir / "LEVELS", "board": rule_dir / "BOARD.json", "state": rule_dir / "artifact_sequence.json", "output": rule_dir / "artifact_result.json"}


#读取文件：读取 UTF-8 JSON 文件。
def read_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"JSON 文件不存在：{path}")
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


#读取文件：推断关卡文件对应的世界。
def infer_world(level_file: Path) -> str:
    name = level_file.stem.lower()
    if "beach" in name or "pirate" in name:
        return "beach"
    if "roof" in name:
        return "roof"
    if "future" in name:
        return "future"
    if "dark" in name or "night" in name:
        return "dark"
    return "egypt"


#连续运行：读取已经保存的神器点击序列。
def load_sequence(state_file: Path) -> list[str]:
    if not state_file.exists():
        return []
    data = read_json(state_file)
    sequence = data.get("sequence", []) if isinstance(data, Mapping) else []
    return [str(item) for item in sequence if str(item) in "134"]


#连续运行：保存神器点击序列。
def save_sequence(state_file: Path, sequence: list[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"sequence": sequence}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


#连续运行：手动清空序列文件。
def clear_sequence(state_file: Path) -> None:
    if state_file.exists():
        state_file.unlink()


#读取文件：取得默认摆放植物文件，不存在时创建空模板。
def ensure_placement_file(board_file: Path) -> Path:
    if not board_file.exists():
        board_file.parent.mkdir(parents=True, exist_ok=True)
        board_file.write_text(json.dumps({"world": "modern", "columns": 5, "rows": 3, "airflow_center": {"col": 3, "row": 1}, "plants": [], "cells": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return board_file


#数据模型：植物静态属性。
@dataclass(frozen=True)
class Plant:
    name: str
    cost: int
    consumable: bool = False
    grid: str = "ground"
    waves: bool = True
    sky: bool = False
    blacklist: frozenset[str] = frozenset()
    valid_stages: frozenset[str] = frozenset()
    mausoleum_cost: int | None = None
    is_pult: bool = False


#数据模型：棋盘植物实例。
@dataclass
class Instance:
    name: str
    col: int
    row: int
    cost: int | None = None
    upgradeable: bool = True

    #输出文件：序列化植物实例。
    def json(self) -> dict[str, Any]:
        result = {"type": self.name, "col": self.col, "row": self.row}
        if self.cost is not None:
            result["effective_cost"] = self.cost
        if not self.upgradeable:
            result["upgradeable"] = False
        return result


#数据模型：棋盘格子。
@dataclass
class Cell:
    terrain: str = "ground"
    blocked: bool = False
    special: bool = False
    occupied: int = 0


#数据模型：棋盘。
@dataclass
class Board:
    world: str
    columns: int = 9
    rows: int = 5
    plants: list[Instance] = field(default_factory=list)
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)
    airflow_after: bool = False
    airflow_center: tuple[int, int] | None = None

    #棋盘读取：读取一个格子。
    def cell(self, col: int, row: int) -> Cell:
        return self.cells.get((col, row), Cell())

    #棋盘读取：判断坐标是否越界。
    def in_bounds(self, col: int, row: int) -> bool:
        return 0 <= col < self.columns and 0 <= row < self.rows

    #棋盘读取：取得默认中心。
    def center(self) -> tuple[int, int]:
        return self.airflow_center or (self.columns // 2, self.rows // 2)

    #棋盘读取：取得所有阻挡格。
    def blocked_positions(self) -> set[tuple[int, int]]:
        return {pos for pos, cell in self.cells.items() if cell.blocked}


#数据模型：神器点击进度。
@dataclass
class Progress:
    primed_stage1: bool = False
    primed_stage3: bool = False
    primed_stage4: bool = False
    clicks: int = 0


#随机数：实现神器洗牌 RNG。
class ShuffleRng:
    #随机数：初始化 MT19937 状态。
    def __init__(self, seed: int = DEFAULT_SEED) -> None:
        self.state = [0] * MT_N
        self.state[0] = seed & MASK32
        for index in range(1, MT_N):
            self.state[index] = (1812433253 * (self.state[index - 1] ^ (self.state[index - 1] >> 30)) + index) & MASK32
        self.index = 0
        self.draws = 0

    #随机数：生成一个 32 位随机数。
    def next_u32(self) -> int:
        current, following = self.index, (self.index + 1) % MT_N
        mix = (self.state[current] & 0x80000000) | (self.state[following] & 0x7FFFFFFF)
        self.state[current] = (self.state[(current + MT_M) % MT_N] ^ (mix >> 1) ^ (MT_A if mix & 1 else 0)) & MASK32
        value = self.state[current]
        value ^= value >> 11
        value ^= (value << 7) & 0x9D2C5680
        value ^= (value << 15) & 0xEFC60000
        value ^= value >> 18
        self.index = following
        self.draws += 1
        return value & MASK32

    #随机数：生成闭区间随机整数。
    def uniform(self, upper_inclusive: int) -> int:
        if upper_inclusive < 0:
            raise ValueError("随机上界不能小于 0")
        span = upper_inclusive + 1
        if span == 1:
            return 0
        width = (span - 1).bit_length()
        while True:
            value = self.next_u32() & ((1 << width) - 1)
            if value < span:
                return value

    #随机数：洗牌并记录首个下标。
    def shuffle_trace(self, values: list[str]) -> tuple[list[str], int | None]:
        result, first_index = list(values), None
        for first in range(len(result) - 1):
            forced = getattr(self, "forced_first_index", None) if first == 0 else None
            if first == 0 and forced is not None:
                self.forced_first_index = None
                # 首随机索引与神器/关卡菜单一致，使用 0-based 候选下标。
                offset = max(0, min(int(forced), len(result) - 1))
            else:
                offset = self.uniform(len(result) - first - 1)
            if first == 0:
                first_index = offset
            if offset:
                other = first + offset
                result[first], result[other] = result[other], result[first]
        return result, first_index


#随机数：实现被动生成的坐标 RNG。
class CoordinateRng(ShuffleRng):
    #随机数：初始化坐标 RNG。
    def __init__(self, seed: int = DEFAULT_SEED) -> None:
        super().__init__(seed)
        self.index = MT_N

    #随机数：扭转状态数组。
    def _twist(self) -> None:
        for index in range(MT_N):
            mix = (self.state[index] & 0x80000000) | (self.state[(index + 1) % MT_N] & 0x7FFFFFFF)
            self.state[index] = (self.state[(index + MT_M) % MT_N] ^ (mix >> 1) ^ (MT_A if mix & 1 else 0)) & MASK32
        self.index = 0

    #随机数：生成坐标随机数。
    def next_u32(self) -> int:
        if self.index >= MT_N:
            self._twist()
        value = self.state[self.index]
        self.index += 1
        value ^= value >> 11
        value ^= (value << 7) & 0x9D2C5680
        value ^= (value << 15) & 0xEFC60000
        value ^= value >> 18
        self.draws += 1
        return value & 0x7FFFFFFF

    #随机数：生成坐标范围内的整数。
    def range(self, minimum: int, maximum: int) -> int:
        return minimum + self.next_u32() % (maximum - minimum + 1)


#配置读取：取得 JSON 对象数据。
def _obj_data(item: Any) -> Mapping[str, Any] | None:
    return item.get("objdata") if isinstance(item, Mapping) and isinstance(item.get("objdata"), Mapping) else None


#配置读取：生成属性对象名称变体。
def name_variants(value: str) -> set[str]:
    variants = {"".join(char.lower() for char in value if char.isalnum())}
    changed = True
    while changed:
        changed = False
        for current in tuple(variants):
            for suffix in ("default", "props", "propertysheet", "properties", "plant"):
                if current.endswith(suffix) and len(current) > len(suffix):
                    short = current[:-len(suffix)]
                    if short not in variants:
                        variants.add(short); changed = True
            if current.startswith("plant") and len(current) > 5:
                short = current[5:]
                if short not in variants:
                    variants.add(short); changed = True
    return variants


#读取植物：将外部植物名转换为属性表使用的规范键。
def canonical_plant_key(value: str) -> str:
    key = "".join(char.lower() for char in str(value) if char.isalnum())
    return PLANT_NAME_ALIASES.get(key, key)


#读取植物：读取 PlantTypeOrder 和植物属性。
def load_plants(path: Path) -> tuple[list[Plant], set[str]]:
    document = read_json(path); objects = document.get("objects", []) if isinstance(document, Mapping) else []
    order: list[str] = []; index: dict[str, Plant] = {}
    for item in objects:
        data = _obj_data(item)
        if not data:
            continue
        if not order and isinstance(data.get("PlantTypeOrder"), list):
            order = [str(value) for value in data["PlantTypeOrder"] if isinstance(value, str)]
        aliases = list(item.get("aliases", [])) if isinstance(item, Mapping) and isinstance(item.get("aliases"), list) else []
        if isinstance(item.get("objclass"), str):
            aliases.append(item["objclass"])
        if not any(key in data for key in ("Cost", "IsConsumable", "PlantGridType")):
            continue
        try:
            cost = int(data.get("Cost", 0) or 0)
        except (TypeError, ValueError):
            continue
        blacklist = data.get("BlackListStages", []); valid = data.get("ValidStages", [])
        plant = Plant(aliases[0].lower() if aliases else "", cost, bool(data.get("IsConsumable", False)), str(data.get("PlantGridType", "ground") or "ground").lower(), bool(data.get("CanLiveOnWaves", False)), bool(data.get("CanLiveInSky", False)), frozenset(str(x).lower() for x in blacklist if isinstance(x, str)), frozenset(str(x).lower() for x in valid if isinstance(x, str)) if isinstance(valid, list) else frozenset(), int(data["MausoleumCost"]) if data.get("MausoleumCost") is not None else None, bool(data.get("IsPultPlant", False)))
        for alias in aliases:
            for key in name_variants(alias):
                index.setdefault(key, plant)
    if not order:
        raise ValueError("属性表缺少 GamePropertySheet.PlantTypeOrder")
    result, unresolved = [], set()
    for name in order:
        key = canonical_plant_key(name); source = index.get(key)
        if source is None:
            unresolved.add(name); continue
        canonical_name = "darkmatter_dragonfruit" if key == "darkmatterdragonfruit" else name
        result.append(Plant(canonical_name, source.cost, source.consumable, source.grid, source.waves, source.sky, source.blacklist, source.valid_stages, source.mausoleum_cost, source.is_pult))
    return result, unresolved


#读取植物：加载目录并补齐运行时缺失植物。
def load_plant_catalog(properties_file: Path) -> tuple[list[Plant], set[str]]:
    plants, unresolved = load_plants(properties_file); document = read_json(properties_file); order: list[str] = []
    for item in document.get("objects", []) if isinstance(document, Mapping) else []:
        data = _obj_data(item)
        if data and isinstance(data.get("PlantTypeOrder"), list): order = [str(x) for x in data["PlantTypeOrder"]]; break
    rank = {canonical_plant_key(name): index for index, name in enumerate(order)}; known = {canonical_plant_key(plant.name) for plant in plants}
    if "dazeychain" not in known: plants.append(Plant("dazeychain", 125)); unresolved.discard("dazeychain")
    if "mapleblade" not in known: plants.append(Plant("mapleblade", 400)); unresolved.discard("mapleblade")
    plants.sort(key=lambda plant: (rank.get(canonical_plant_key(plant.name), 10**9), plant.name.lower()))
    return plants, unresolved


#读取黑名单：读取神器输出黑名单。
def load_blacklist(path: Path) -> set[str]:
    document = read_json(path)
    for item in document.get("objects", []) if isinstance(document, Mapping) else []:
        data = _obj_data(item)
        if data and data.get("TypeName") == "artifact_evolution": return {str(x).lower() for x in data.get("plantBlackList", []) if isinstance(x, str)}
    return set()


#读取黑名单：读取莲叶承载黑名单。
def load_lilypad_blocklist(properties_file: Path) -> set[str]:
    document = read_json(properties_file)
    for item in document.get("objects", []) if isinstance(document, Mapping) else []:
        data = _obj_data(item); value = data.get("PlantsWhichCannotBePlantedOnLilypads") if data else None
        if isinstance(value, Mapping) and isinstance(value.get("List"), list): return {str(x).lower() for x in value["List"] if isinstance(x, str)}
    return set()


#读取配置：读取关卡和 SeedBank 配置。
def load_level_rules(path: Path, levelmodules_path: Path | None = None) -> dict[str, Any]:
    document = read_json(path); result: dict[str, Any] = {"level_file": str(path), "modules": [], "stage_module": "", "stage_prefix": None, "seedbank_blacklist": [], "seedbank_whitelist": [], "preset_plants": [], "is_artifact_disabled": False, "disable_peavine": False, "conveyor_plant_list": [], "frozen_plant_list": [], "flowerpot_start_column": None, "flowerpot_end_column": None}
    for item in document.get("objects", []) if isinstance(document, Mapping) else []:
        data = _obj_data(item)
        if not data: continue
        kind = item.get("objclass")
        if kind == "LevelDefinition": result.update(modules=list(data.get("Modules", []) or []), stage_module=str(data.get("StageModule", "") or ""), is_artifact_disabled=bool(data.get("IsArtifactDisabled", False)), disable_peavine=bool(data.get("DisablePeavine", False)))
        elif kind == "SeedBankProperties": result.update(seedbank_whitelist=list(data.get("PlantWhiteList", []) or []), seedbank_blacklist=list(data.get("PlantBlackList", []) or []), preset_plants=list(data.get("PresetPlantList", []) or []))
        elif kind == "ConveyorSeedBankProperties": result["conveyor_plant_list"] = [str(x.get("PlantType")) for x in data.get("InitialPlantList", []) if isinstance(x, Mapping) and x.get("PlantType")]
        elif kind == "InitialPlantProperties": result["frozen_plant_list"] = [str(x.get("TypeName")) for x in data.get("InitialPlantPlacements", []) if isinstance(x, Mapping) and x.get("TypeName")]
        elif kind == "RoofProperties":
            try: result["flowerpot_start_column"], result["flowerpot_end_column"] = int(data.get("FlowerPotStartColumn")), int(data.get("FlowerPotEndColumn"))
            except (TypeError, ValueError): pass
    modules = " ".join(map(str, result["modules"])); result["mode"] = "conveyor" if "Conveyor" in modules else "last_stand_event_item" if "LastStand" in modules and "EventItemPlacement" in modules else "last_stand" if "LastStand" in modules else "frozen_plant_placement" if "FrozenPlantPlacement" in modules else "standard"
    if levelmodules_path and levelmodules_path.exists(): result["stage_prefix"] = resolve_stage_prefix(str(result["stage_module"]), levelmodules_path)
    return result


#读取配置：提取 StageModule 别名。
def extract_stage_module_alias(value: str) -> str:
    match = re.search(r"RTID\(([^@)]+)@LevelModules\)", value or ""); return match.group(1) if match else str(value or "")


#读取配置：解析 StagePrefix。
def resolve_stage_prefix(stage_module: str, levelmodules_file: Path) -> str | None:
    alias, document = extract_stage_module_alias(stage_module), read_json(levelmodules_file)
    for item in document.get("objects", []) if isinstance(document, Mapping) else []:
        if isinstance(item, Mapping) and alias in (item.get("aliases", []) or []):
            data = _obj_data(item)
            if data and isinstance(data.get("StagePrefix"), str): return data["StagePrefix"]
    return None


#读取配置：读取 LevelDefinition 字段。
def read_level_definition(level_file: Path) -> dict[str, Any]:
    rules = load_level_rules(level_file); return {"level_file": str(level_file), "modules": rules["modules"], "stage_module": rules["stage_module"], "is_artifact_disabled": rules["is_artifact_disabled"], "disable_peavine": rules["disable_peavine"]}


#读取配置：读取 SeedBank 字段。
def read_seedbank(level_file: Path) -> dict[str, Any]:
    rules = load_level_rules(level_file); return {"plant_whitelist": rules["seedbank_whitelist"], "plant_blacklist": rules["seedbank_blacklist"], "preset_plants": rules["preset_plants"]}


#读取配置：读取编辑器黑名单。
def read_level_editor_config(path: Path | None) -> dict[str, list[str]]:
    if path is None or not path.exists(): return {}
    document = read_json(path); result = {}; keys = ("SunProductPlantBanList", "NormalGridPlantBlackList", "SingleHandedPlantBlackList", "NormalSeedPlantBlackList", "AshPlantBanList")
    for item in document.get("objects", []) if isinstance(document, Mapping) else []:
        data = _obj_data(item)
        if data and item.get("objclass") == "LevelEditorConfig":
            for key in keys: result[key] = [str(x).lower() for x in data.get(key, []) if isinstance(x, str)]
            break
    return result


#读取配置：按模式选择编辑器黑名单。
def editor_exclusions_for_mode(mode: str, config: Mapping[str, list[str]] | None) -> tuple[set[str], list[str]]:
    tables = ["SunProductPlantBanList"] if str(mode).lower() in {"last_stand", "last_stand_event_item"} else []
    return {x for table in tables for x in (config or {}).get(table, [])}, tables


#读取配置：合并所有关卡配置。
def build_level_rules(level_file: Path, levelmodules_file: Path, leveleditor_file: Path | None = None) -> dict[str, Any]:
    result = load_level_rules(level_file, levelmodules_file); result["level_definition"] = read_level_definition(level_file); result["seedbank"] = read_seedbank(level_file); result["leveleditor"] = read_level_editor_config(leveleditor_file); result["editor_exclusions"], result["editor_tables"] = editor_exclusions_for_mode(result.get("mode", "standard"), result["leveleditor"]); return result


#读取配置：拒绝神器被禁用的关卡。
def require_artifact_enabled(level_rules: Mapping[str, Any]) -> None:
    if level_rules.get("is_artifact_disabled"): raise ValueError("当前关卡 IsArtifactDisabled=true，禁止生成神器复刻结果")


#读取配置：根据植物属性建立世界规则。
def load_native_rules(plants: list[Plant]) -> list[dict[str, Any]]:
    rules = []
    for plant in plants:
        when, reasons = {}, []
        if plant.valid_stages: when["valid_stages"] = tuple(sorted(plant.valid_stages)); reasons.append("ValidStages")
        if plant.grid == "water": when.update(plant_grid_type="water", board_terrain_not="water"); reasons.append("PlantGridType=water")
        if when: rules.append({"type_name": plant.name, "chinese_name": plant.name, "reason": " + ".join(reasons), "when": when, "source": "PlantProperties"})
    return rules


#读取配置：生成关卡最终规则。
def static_native_rules(level_rules: Mapping[str, Any], native_rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result = {"stage_module": level_rules.get("stage_module"), "stage_prefix": level_rules.get("stage_prefix"), "modules": list(level_rules.get("modules", [])), "seedbank_blacklist": list(level_rules.get("seedbank_blacklist", [])), "editor_exclude": {str(x).lower() for x in level_rules.get("editor_exclusions", set())}, "exclude": {str(x).lower() for x in level_rules.get("seedbank_blacklist", [])}, "native_placement_rules": list(native_rules or []), "mode": level_rules.get("mode", "standard")}
    return result


#读取棋盘：从 JSON 创建棋盘。
def profile_board(document: Mapping[str, Any], world: str, airflow: bool) -> Board:
    raw = document.get("board", document); raw = raw if isinstance(raw, Mapping) else {}; board = Board(str(raw.get("world", world)).lower(), int(raw.get("columns", 9)), int(raw.get("rows", 5)), airflow_after=airflow or bool(raw.get("airflow_after", False)))
    center = raw.get("airflow_center")
    if isinstance(center, Mapping): board.airflow_center = (int(center.get("col", 0)), int(center.get("row", 0)))
    for item in raw.get("plants", []) if isinstance(raw.get("plants"), list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("type", item.get("TypeName")), str): board.plants.append(Instance(item.get("type", item.get("TypeName")), int(item.get("col", 0)), int(item.get("row", 0)), item.get("effective_cost"), bool(item.get("upgradeable", True))))
    for item in raw.get("cells", []) if isinstance(raw.get("cells"), list) else []:
        if isinstance(item, Mapping):
            pos = (int(item.get("col", item.get("x", 0))), int(item.get("row", item.get("y", 0)))); tags = item.get("tags", []); board.cells[pos] = Cell(str(item.get("terrain", "ground")).lower(), bool(item.get("blocked", False)), bool(item.get("special", False) or "special" in tags), int(item.get("native_occupied_count", 0) or 0))
    return board


#读取棋盘：读取外部棋盘文件或创建空棋盘。
def load_board(board_file: Path | None, world: str, airflow_after: bool) -> Board:
    return profile_board(read_json(board_file) if board_file else {}, world, airflow_after)


#读取棋盘：应用海滩默认布局。
def apply_default_beach_template(board: Board, level_rules: Mapping[str, Any]) -> None:
    if str(level_rules.get("stage_prefix") or "").lower() != "beach": return
    board.columns, board.rows = 9, 5
    for col in range(9):
        for row in range(5): board.cells.setdefault((col, row), Cell()).terrain = "ground" if col < 3 else "water"


#读取棋盘：应用屋顶默认布局。
def apply_default_roof_template(board: Board, level_rules: Mapping[str, Any]) -> None:
    if str(level_rules.get("stage_prefix") or "").lower() not in {"roof", "roof_night", "snow_roof"}: return
    board.columns, board.rows = 9, 5; start, end = level_rules.get("flowerpot_start_column"), level_rules.get("flowerpot_end_column")
    for col in range(9):
        for row in range(5):
            cell = board.cells.setdefault((col, row), Cell()); cell.terrain = "roof"; cell.special = isinstance(start, int) and isinstance(end, int) and start <= col <= end


#候选池：执行阳光成本过滤。
def filter_by_sun_cost(plants: list[Plant], source_cost: int | None, active: bool) -> tuple[list[Plant], dict[str, list[str]]]:
    result, rejected = [], {}
    for plant in plants:
        allowed = plant.cost > source_cost if active and source_cost is not None else plant.cost <= 100
        if allowed: result.append(plant)
        else: rejected.setdefault(plant.name, []).append("cost_not_strictly_higher" if source_cost is not None else "cost_exceeds_passive_limit")
    return result, rejected


#候选池：计算单个坐标的候选植物。
def build_candidate_pool(session: "ArtifactSession", source_cost: int | None, col: int, row: int, active: bool) -> dict[str, Any]:
    eligible, rejected = session.candidates(source_cost, col, row, active); return {"source_cost": source_cost, "position": {"col": col, "row": row}, "eligible": eligible, "rejected": rejected, "eligible_count": len(eligible)}


def build_index_candidate_pool(session: "ArtifactSession", source_cost: int, col: int, row: int) -> dict[str, Any]:
    """构造独立植物索引菜单使用的主动候选池。

    该辅助函数只做规则过滤，不调用洗牌 RNG；返回的列表位置可安全地作为
    菜单索引，避免索引菜单和神器菜单共享随机流或首索引。
    """
    eligible, rejected = session.candidates(int(source_cost), int(col), int(row), True)
    return {"source_cost": int(source_cost), "position": {"col": int(col), "row": int(row)}, "eligible": eligible, "rejected": rejected, "eligible_count": len(eligible)}


#神器会话：保存棋盘、候选池、RNG 和进度。
class ArtifactSession:
    #神器会话：初始化会话。
    def __init__(self, board: Board, plants: list[Plant], blacklist: set[str], seed: int, level: int, level_rules: Mapping[str, Any] | None = None, native_rules: list[dict[str, Any]] | None = None, world_template: str | None = None, allow_lilypad: bool = False, apply_active_world_rules: bool = False, lilypad_blocklist: set[str] | None = None) -> None:
        self.board, self.plants = board, {p.name: p for p in plants}; self.order = [p.name for p in plants]; self.blacklist = {x.lower() for x in blacklist}; self.global_blacklist = set(GLOBAL_BLACKLIST); self.level_rules = dict(level_rules or {}); self.rules = static_native_rules(self.level_rules, native_rules); self.stage_world = str(world_template or self.rules.get("stage_prefix") or board.world).lower(); self.world_template = world_template; self.allow_lilypad = allow_lilypad; self.apply_active_world_rules = apply_active_world_rules; self.lilypad_blocklist = {x.lower() for x in (lilypad_blocklist or set())}; self.rng, self.coord_rng = ShuffleRng(seed), CoordinateRng(seed); self.level = level; self.progress: dict[str, Progress] = {}
      
        self.last_active_positions: set[tuple[int, int]] = set()

    #神器会话：取得点击进度。
    def _progress(self, artifact_id: str) -> Progress: return self.progress.setdefault(artifact_id, Progress())

    #候选池：应用所有名称、黑名单、世界和棋盘过滤。
    def candidates(self, source_cost: int | None, col: int, row: int, active: bool) -> tuple[list[str], dict[str, list[str]]]:
        cell, eligible, rejected = self.board.cell(col, row), [], {}
        for name in self.order:
            plant, lower, reasons = self.plants[name], name.lower(), []
            if lower.startswith("parallel_"): reasons.append("target_parallel_variant")
            if lower in self.global_blacklist: reasons.append("native_fixed_name_filter")
            if source_cost is not None and plant.cost <= source_cost: reasons.append("cost_not_strictly_higher")
            if source_cost is None and plant.cost > 100: reasons.append("cost_exceeds_passive_limit")
            if plant.consumable: reasons.append("target_is_consumable")
            if lower in self.blacklist: reasons.append("artifact_output_blacklist")
            if not self.world_template and lower in self.rules.get("exclude", set()): reasons.append("level_seedbank_blacklist")
            if lower == "lilypad" and not self.world_template and cell.terrain != "water": reasons.append("lilypad_requires_water")
            if lower == "lilypad" and cell.special: reasons.append("lilypad_already_present")
            if cell.special and lower in self.lilypad_blocklist: reasons.append("lilypad_cannot_support_plant")
            use_terrain = not active or self.world_template or self.apply_active_world_rules
            if use_terrain:
                if self.stage_world in plant.blacklist: reasons.append("world_blacklist")
                if plant.valid_stages and self.stage_world not in plant.valid_stages and not (self.allow_lilypad and lower == "lilypad"): reasons.append("valid_stages_world_mismatch")
                if cell.blocked: reasons.append("cell_blocked")
                if cell.terrain == "water" and plant.grid != "water" and not plant.waves and not cell.special: reasons.append("plant_cannot_live_on_water")
                if cell.terrain == "sky" and not plant.sky: reasons.append("plant_cannot_live_in_sky")
                if cell.occupied: reasons.append("cell_occupied")
            if reasons: rejected[name] = reasons
            else: eligible.append(name)
        return eligible, rejected

    #神器一阶：处理指定布局中的植物。
    def active(self, center: tuple[int, int], artifact_id: str, layout: str = "3x3", mutate_sources: bool = True) -> dict[str, Any]:
        col, row = center; positions = {(col, row)} if layout == "single_source" else {(col - 1, row + dy) for dy in (-1, 0, 1)} if layout == "left_column_1x3" else {(col + dx, row + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        indices = [i for i, plant in enumerate(self.board.plants) if (plant.col, plant.row) in positions]; indices.sort(key=lambda i: (-self.board.plants[i].col, -self.board.plants[i].row)); records = []
        self.last_active_positions = {(self.board.plants[i].col, self.board.plants[i].row) for i in indices}
        for scan_order, index in enumerate(indices):
            source = self.board.plants[index]; cost = source.cost if source.cost is not None else self.plants[source.name].cost; eligible, rejected = self.candidates(cost, source.col, source.row, True); shuffled, first_index = self.rng.shuffle_trace(eligible); target = shuffled[0] if shuffled else None
            records.append({"scan_order": scan_order, "source": source.json(), "position": {"col": source.col, "row": source.row}, "source_effective_cost": cost, "eligible_candidates": eligible, "rejected_candidates": rejected, "shuffled_candidates": shuffled, "first_random_index": first_index, "rng_draws_after": self.rng.draws, "target": target})
            if target and mutate_sources: self.board.plants[index] = Instance(target, source.col, source.row, None, source.upgradeable)
        return {"type": "active", "artifact_id": artifact_id, "center": {"col": col, "row": row}, "transformed": records}

    #神器被动：随机坐标生成低成本植物。
    def passive(self, ticks: int, artifact_id: str) -> dict[str, Any]:
        generated = []
        for tick in range(1, ticks + 1):
            result: dict[str, Any] = {"tick": tick, "target": None}
            for _ in range(200):
                col, row = self.coord_rng.range(0, 8), self.coord_rng.range(0, 4)
                if not self.board.in_bounds(col, row): continue
                eligible, rejected = self.candidates(None, col, row, False)
                if not eligible: continue
                shuffled, first_index = self.rng.shuffle_trace(eligible); target = shuffled[0]; self.board.plants.append(Instance(target, col, row)); result.update(position={"col": col, "row": row}, eligible_candidates=eligible, rejected_candidates=rejected, shuffled_candidates=shuffled, first_random_index=first_index, rng_draws_after=self.rng.draws, target=target); break
            else: result["skipped_reason"] = "no_placeable_candidate_within_200_attempts"
            generated.append(result)
        return {"type": "passive", "artifact_id": artifact_id, "ticks": ticks, "interval_seconds": max(1, 60 - self.level), "generated": generated}

    #神器四阶：填充 3x3 空位。
    def stage4(self, center: tuple[int, int], artifact_id: str, include_center: bool = False) -> dict[str, Any]:
        generated, blocked = [], self.board.blocked_positions()
        active_sources = set(self.last_active_positions)
        for col in range(center[0] - 1, center[0] + 2):
            for row in range(center[1] - 1, center[1] + 2):
                if not self.board.in_bounds(col, row): continue
                result: dict[str, Any] = {"position": {"col": col, "row": row}, "target": None}
                if (col, row) in active_sources:
                    result["skipped_reason"] = "active_source_replaced"; generated.append(result); continue
                if ((col, row) == center and not include_center) or (col, row) in blocked: result["skipped_reason"] = "source_or_world_obstacle"; generated.append(result); continue
                eligible, rejected = self.candidates(None, col, row, False); shuffled, first_index = self.rng.shuffle_trace(eligible); target = shuffled[0] if shuffled else None; result.update(eligible_candidates=eligible, rejected_candidates=rejected, shuffled_candidates=shuffled, first_random_index=first_index, rng_draws_after=self.rng.draws, target=target); generated.append(result)
                if target: self.board.plants.append(Instance(target, col, row))
        return {"type": "stage4", "artifact_id": artifact_id, "center": {"col": center[0], "row": center[1]}, "generated": generated}

    #神器点击：执行 1/3/4 阶点击状态机。
    def click(self, stage: int, center: tuple[int, int], artifact_id: str) -> dict[str, Any]:
        if stage not in (1, 3, 4): raise ValueError("stage 必须为 1、3 或 4")
        state = self._progress(artifact_id); state.clicks += 1; phases = []
        if not state.primed_stage1: phases.append(self.active(center, artifact_id, "3x3", False)); state.primed_stage1 = True
        elif stage == 1: phases.append(self.active(center, artifact_id, "3x3", False))
        elif stage == 3: phases.append(self.active(center, artifact_id, "left_column_1x3", False)); state.primed_stage3 = True
        else: phases.extend((self.active(center, artifact_id, "left_column_1x3", False), self.stage4(center, artifact_id, True))); state.primed_stage4 = True
        return {"type": "artifact_click", "artifact_id": artifact_id, "stage": stage, "click_index": state.clicks, "primed_stage1": state.primed_stage1, "primed_stage3": state.primed_stage3, "primed_stage4": state.primed_stage4, "phases": phases}


#神器会话：创建会话对象。
def create_session(board: Board, plants: list[Plant], blacklist: set[str], seed: int, level: int, level_rules: Mapping[str, Any], native_rules: list[dict[str, Any]], world_template: str | None = None, allow_lilypad: bool = False, apply_active_world_rules: bool = False, lilypad_blocklist: set[str] | None = None) -> ArtifactSession:
    return ArtifactSession(board, plants, blacklist, seed, level, level_rules, native_rules, world_template, allow_lilypad, apply_active_world_rules, lilypad_blocklist)


#神器配置：定义 1/3/4 阶界面规则。
ARTIFACT_INTERFACE_RULES = {1: {"level": 1, "layout": "3x3", "display_cells": ((0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1), (0, 2), (1, 2), (2, 2)), "processing": "active_3x3"}, 3: {"level": 20, "layout": "left_column_1x3", "display_cells": ((0, 0), (0, 1), (0, 2)), "processing": "active_1x3"}, 4: {"level": 30, "layout": "left_column_1x3", "display_cells": ((0, 0), (0, 1), (0, 2)), "processing": "active_1x3_then_stage4_fill_3x3"}}
ARTIFACT_TIER_LEVELS = {tier: data["level"] for tier, data in ARTIFACT_INTERFACE_RULES.items()}


#神器配置：解析阶数和点击序列。
def resolve_artifact_click_rule(tier: int, stage1_clicked: bool = False) -> tuple[int, str]:
    if tier not in ARTIFACT_TIER_LEVELS: raise ValueError("artifact-tier 只能是 1、3 或 4")
    # 高阶神器界面是独立入口：3/4 阶直接处理最左侧一竖，
    # 不再隐式补一次 1 阶 3x3；连续点击由调用方显式传入序列。
    return ARTIFACT_TIER_LEVELS[tier], "1" if tier == 1 else str(tier)


#神器配置：返回可输出的界面规则。
def artifact_interface_rule(tier: int, stage1_clicked: bool = False) -> dict[str, Any]:
    level, sequence = resolve_artifact_click_rule(tier, stage1_clicked); result = dict(ARTIFACT_INTERFACE_RULES[tier]); result.update(tier=tier, level=level, stage1_clicked_before=stage1_clicked, click_sequence=sequence, display_cells=[list(x) for x in result["display_cells"]]); result["candidate_count"] = len(result["display_cells"]); return result


#读取阳光：创建神器界面向日葵。
def ensure_interface_sunflowers(board: Board, tier: int, sunflower_sun: int, stage1_clicked: bool = False) -> None:
    if sunflower_sun < 0: raise ValueError("sunflower-sun 不能小于 0")
    if board.plants:
        for plant in board.plants:
            if plant.name.lower() == "sunflower" and plant.cost is None: plant.cost = sunflower_sun
        return
    center = board.center(); positions = ((center[0] + dx, center[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)) if tier == 1 or (tier in (3, 4) and not stage1_clicked) else ((center[0] - 1, center[1] + dy) for dy in (-1, 0, 1)); board.plants.extend(Instance("sunflower", col, row, sunflower_sun) for col, row in positions)


#读取阳光：创建主线向日葵来源。
def ensure_level_source(board: Board, sunflower_sun: int, source_position: tuple[int, int] | None = None) -> None:
    if sunflower_sun < 0: raise ValueError("level-sunflower-sun 不能小于 0")
    if board.plants:
        for plant in board.plants:
            if plant.name.lower() == "sunflower" and plant.cost is None: plant.cost = sunflower_sun
        return
    col, row = source_position or board.center(); board.plants.append(Instance("sunflower", col, row, sunflower_sun))


#通用工具：标准化世界名称。
def world_id(value: str) -> str: return str(value).strip().lower()


#通用工具：递归转换 JSON 不支持的集合。
def serializable(value: Any) -> Any:
    if isinstance(value, set): return sorted(value)
    if isinstance(value, tuple): return [serializable(x) for x in value]
    if isinstance(value, Mapping): return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, list): return [serializable(x) for x in value]
    return value


#神器生成：生成点击序列。
def generate_click_sequence(session: ArtifactSession, sequence: str, center: tuple[int, int], artifact_id: str) -> list[dict[str, Any]]:
    if not sequence or any(x not in "134" for x in sequence): raise ValueError("点击序列只能包含 1、3、4")
    return [session.click(int(x), center, artifact_id) for x in sequence]


#神器一阶：生成主动处理结果。
def generate_active(session: ArtifactSession, center: tuple[int, int], artifact_id: str, layout: str = "3x3") -> dict[str, Any]:
    return session.active(center, artifact_id, layout)


#神器被动：生成被动处理结果。
def generate_passive(session: ArtifactSession, ticks: int, artifact_id: str) -> dict[str, Any]:
    return session.passive(ticks, artifact_id)


#神器四阶：生成四阶填充结果。
def generate_stage4(session: ArtifactSession, center: tuple[int, int], artifact_id: str, include_center: bool = False) -> dict[str, Any]:
    return session.stage4(center, artifact_id, include_center)


#主线生成：生成主线关卡的 1 阶或 4 阶结果。
def generate_level_reproduction(level_file: Path | None, tier: int, board_file: Path | None = None, levelmodules_file: Path | None = None, world: str = "egypt", artifact_id: str = "artifact_evolution", sunflower_sun: int = 20, center_col: int | None = None, center_row: int | None = None, source_col: int | None = None, source_row: int | None = None, seed: int = DEFAULT_SEED, first_index_override: int | None = None) -> dict[str, Any]:
    if tier not in (1, 4): raise ValueError("level-tier 只能是 1 或 4")
    paths = default_paths(); level_file = level_file or paths["levels"] / ("BEACH1.JSON" if world_id(world) == "beach" else "EGYPT1.JSON"); modules = levelmodules_file or paths["levelmodules"]; plants, unresolved = load_plant_catalog(paths["properties"]); blacklist = load_blacklist(paths["artifact"]); native = load_native_rules(plants); rules = build_level_rules(level_file, modules, paths["leveleditor"]); require_artifact_enabled(rules); board = load_board(board_file, world, False)
    if board_file is None: board.world = world_id(str(rules.get("stage_prefix") or world)); board.columns, board.rows, board.airflow_center = 9, 5, (1, 2); apply_default_beach_template(board, rules); apply_default_roof_template(board, rules)
    source = (source_col, source_row) if source_col is not None and source_row is not None else None; center = (center_col, center_row) if center_col is not None and center_row is not None else (source or board.center()); had_board_plants = bool(board.plants)
    session = create_session(board, plants, blacklist, seed, tier, rules, native, None, False, True, load_lilypad_blocklist(paths["properties"]))
    if first_index_override is not None: session.rng.forced_first_index = max(0, int(first_index_override))
    # 普通埃及关卡的空地 4 阶没有 active 来源，直接对中心 3×3 做 stage4 填充。
    # 这样候选成本基准为 None（日志中的 <=100 阳光池），不会凭空消耗一次 active RNG。
    if tier == 4 and not had_board_plants:
        phases = [session.stage4(center, artifact_id, True)]
    else:
        layout = "3x3" if had_board_plants else "single_source"; phases = [session.active(center, artifact_id, layout)]
        if tier == 4: phases.append(session.stage4(center, artifact_id, True))
    stage4 = next((x for x in phases if x["type"] == "stage4"), None)
    active_count = len(phases[0].get("transformed", []))
    return {"schema": "artifact_evolution_level_reproduction_v1", "rule_kind": "level", "level_file": str(level_file), "world": board.world, "tier": tier, "level": tier, "sunflower_sun": sunflower_sun, "source_position": {"col": center[0], "row": center[1]}, "center": {"col": center[0], "row": center[1]}, "active_source_count": active_count, "stage4_grid_count": len(stage4["generated"]) if stage4 else 0, "stage4_selected_count": sum(1 for x in stage4["generated"] if x.get("target")) if stage4 else 0, "level_rules": serializable(static_native_rules(rules, native)), "unresolved_property_plants": sorted(unresolved), "events": [{"type": "level_click", "tier": tier, "phases": phases}], "board": {"columns": board.columns, "rows": board.rows, "world": board.world, "plants": [x.json() for x in board.plants], "rng_draws": session.rng.draws, "coordinate_rng_draws": session.coord_rng.draws}}


#神器生成：生成神器界面结果。
def generate_reproduction(level_file: Path | None, board_file: Path | None = None, levelmodules_file: Path | None = None, world: str = "egypt", tier: int = 1, stage1_clicked: bool = False, artifact_id: str = "artifact_evolution", sunflower_sun: int = 50, center_col: int | None = None, center_row: int | None = None, sequence_override: str | None = None) -> dict[str, Any]:
    paths = default_paths(); level_file = level_file or paths["levels"] / ("BEACH1.JSON" if world_id(world) == "beach" else "EGYPT1.JSON"); modules = levelmodules_file or paths["levelmodules"]; plants, unresolved = load_plant_catalog(paths["properties"]); blacklist = load_blacklist(paths["artifact"]); native = load_native_rules(plants); rules = build_level_rules(level_file, modules, paths["leveleditor"]); require_artifact_enabled(rules); board = load_board(board_file, world, False)
    if board_file is None: board.columns, board.rows, board.airflow_center = 5, 3, (3, 1)
    sequence = sequence_override or artifact_interface_rule(tier, stage1_clicked)["click_sequence"]
    effective_tier = 4 if "4" in sequence else 3 if "3" in sequence else 1
    stage1_clicked = bool(sequence and sequence[0] != "1")
    center = (center_col, center_row) if center_col is not None and center_row is not None else board.center()
    # 外部传入中心时同步棋盘中心，确保自动放置的界面向日葵与点击处理使用同一位置。
    board.airflow_center = center
    ensure_interface_sunflowers(board, effective_tier, sunflower_sun, stage1_clicked); interface = artifact_interface_rule(effective_tier, stage1_clicked); level = 30 if "4" in sequence else 20 if "3" in sequence else 1; session = create_session(board, plants, blacklist, DEFAULT_SEED, level, rules, native, "modern", True, False, load_lilypad_blocklist(paths["properties"]));
    if stage1_clicked and tier in (3, 4): session._progress(artifact_id).primed_stage1 = True
    events = generate_click_sequence(session, sequence, center, artifact_id)
    return {"schema": "artifact_evolution_reproduction_v2", "level_file": str(level_file), "world": board.world, "tier": effective_tier, "level": level, "click_sequence": sequence, "stage1_clicked_before": stage1_clicked, "sunflower_sun": sunflower_sun, "interface_rule": interface, "seed": DEFAULT_SEED, "center": {"col": center[0], "row": center[1]}, "level_rules": serializable(static_native_rules(rules, native)), "unresolved_property_plants": sorted(unresolved), "events": events, "board": {"columns": board.columns, "rows": board.rows, "world": board.world, "plants": [x.json() for x in board.plants], "rng_draws": session.rng.draws, "coordinate_rng_draws": session.coord_rng.draws}}


#命令行：构造参数解析器。
def build_parser() -> argparse.ArgumentParser:
    paths = default_paths()
    parser = argparse.ArgumentParser(description="进化神器静态复刻")
    parser.add_argument("--mode", choices=("interface", "level"), default="interface", help="interface=神器页面；level=世界关卡")
    parser.add_argument("--level-file", type=Path, help="世界关卡文件；省略时使用内置模板")
    parser.add_argument("--board", type=Path, default=paths["board"], help="摆放植物和有效阳光 JSON")
    parser.add_argument("--sunflower-sun", type=int, default=50, help="摆放文件没有向日葵阳光时使用")
    parser.add_argument("--artifact-tier", type=int, choices=(1, 3, 4), default=1, help="神器阶数，默认 1 阶")
    parser.add_argument("--output", type=Path, default=paths["output"], help="结果 JSON，默认规则/artifact_result.json")
    parser.add_argument("--clear", action="store_true", help="只清空已保存的神器点击序列")
    return parser


#命令行：执行生成并输出结果。
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        paths = default_paths()
        if args.clear:
            clear_sequence(paths["state"])
            if args.output.exists():
                args.output.unlink()
            print("已清空神器点击序列")
            return 0
        board_file = ensure_placement_file(args.board)
        if args.mode == "level":
            if args.artifact_tier not in (1, 4): raise ValueError("世界关卡只支持神器 1 阶或 4 阶")
            level_file = args.level_file or paths["levels"] / "BEACH1.JSON"
            result = generate_level_reproduction(level_file, args.artifact_tier, board_file, None, infer_world(level_file), "artifact_evolution", args.sunflower_sun)
        else:
            level_file = args.level_file or paths["levels"] / "EGYPT1.JSON"
            saved = load_sequence(paths["state"])
            new_clicks = resolve_artifact_click_rule(args.artifact_tier, bool(saved))[1]
            sequence = saved + list(new_clicks)
            result = generate_reproduction(level_file, board_file, None, infer_world(level_file), args.artifact_tier, False, "artifact_evolution", args.sunflower_sun, sequence_override="".join(sequence))
            save_sequence(paths["state"], sequence)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"已输出：{args.output}")
        return 0
    except Exception as error:
        print(f"复刻失败：{error}", file=sys.stderr); return 1


#程序入口：启动命令行。
if __name__ == "__main__": raise SystemExit(main())
