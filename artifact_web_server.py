#!/usr/bin/env python3
"""本地 Artifact 复刻 Web 服务。

算法只在 Python 进程内执行；浏览器只接收经过裁剪的结果、植物名称和静态图片。
启动：python artifact_web_server.py --host 0.0.0.0 --port 8787
"""
from __future__ import annotations

import argparse
import importlib
import json
import mimetypes
import re
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import artifact_reproduce_modular as engine

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
RULES = ROOT / "规则"
PACKETS = RULES / "PACKETS"
UPLOADS = ROOT / ".artifact_web_uploads"
UPLOADS.mkdir(exist_ok=True)
MAX_UPLOAD = 8 * 1024 * 1024


def load_strings() -> dict[str, str]:
    result: dict[str, str] = {}
    path = RULES / "LAWNSTRINGS.TXT"
    key = None
    for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            key = line[1:-1].strip().upper()
        elif key and line:
            result.setdefault(key, line)
            key = None
    return result


STRINGS = load_strings()

WORLD_LABELS = {
    "EGYPT": "埃及", "BEACH": "海滩", "DARK": "黑暗", "FUTURE": "未来", "PIRATE": "海盗",
    "ROOF": "屋顶", "WILDWEST": "西部", "JURASSIC": "恐龙", "ICEAGE": "冰河", "MODERN": "摩登",
    "LOSTCITY": "失落", "FARFUTURE": "未来", "FAIRYTALE": "童话", "NEONMIX": "摇滚",
}

NAME_LABELS = {
    "ARENA": "竞技场", "ARTIFACT": "神器演示", "ATLANTIS": "海底", "BIRTHDAY": "生日",
    "CARD_GAME": "卡牌", "COWBOY": "西部", "DINO": "恐龙", "DINOSAUR": "恐龙",
    "EIGHTIES": "复兴", "FESTIVAL": "节日", "EGYPT": "埃及", "BEACH": "海滩",
    "DARK": "黑暗", "FUTURE": "未来", "PIRATE": "海盗", "JURASSIC": "恐龙",
    "ICEAGE": "冰河", "MODERN": "摩登", "LOSTCITY": "失落", "NEONMIX": "摇滚",
    "SUMMER_NIGHT": "夏夜", "SUMMER_DAYLIGHT": "夏日", "SPRING_DAYLIGHT": "春日", "SPRING_NIGHT": "春夜",
    "AUTUMN_LATE": "深秋", "CHILDRENDAY": "儿童节", "TUTORIAL": "教学", "CARD": "卡牌", "KONGFU": "功夫",
    "HEIAN": "平安", "PLANTWARS": "植物大战", "PVP": "对战", "NIGHT": "夜晚", "RIFT": "裂隙",
    "FAIRY_TALE": "童话", "DAVECUP": "戴夫杯", "ARBORDAY": "植树节", "NEEDFORSPEED": "极速",
    "NO42_UNIVERSE": "42号宇宙", "SKYCITY": "天空之城", "HALLOWEEN": "万圣节", "ZCORP": "僵尸公司",
    "RENAI": "复兴", "SNOW": "冰雪", "UNCHARTED": "未探索",
}

def level_label(filename: str) -> str:
    stem = Path(filename).stem.upper()
    world = next((label for key, label in NAME_LABELS.items() if key in stem), "自定义")
    return f"{world}世界 · {filename}"

def level_short_label(filename: str) -> str:
    stem = Path(filename).stem.upper()
    world = next((label for key, label in NAME_LABELS.items() if key in stem), "自定义")
    return f"{world}世界"

def level_mode_label(filename: str, rules: dict[str, Any]) -> str:
    modules = " ".join(map(str, rules.get("modules", []))).upper()
    stem = Path(filename).stem.upper()
    detected = str(rules.get("mode") or "standard").lower()
    if detected == "conveyor" or "CONVEYOR" in modules: return "传送带"
    if detected in {"last_stand", "last_stand_event_item"} or "LASTSTAND" in modules: return "植物防线"
    if detected == "frozen_plant_placement" or "FROZENPLANT" in modules: return "固定种子"
    if "DANGER" in stem: return "挑战"
    return ""

def stage_world_label(stage_prefix: str | None, filename: str = "") -> str:
    value = str(stage_prefix or "").upper()
    for key, label in NAME_LABELS.items():
        if key in value:
            return f"{label}世界"
    # StagePrefix 可能是 festival_level_four、modern 等内部名；
    # 未知值保留一个可读中文名，不使用“自定义关卡”误导用户。
    stem = Path(filename).stem.upper()
    if stem.startswith("PVZ1_"):
        return "经典世界"
    token = re.sub(r"_?(LEVEL|STAGE|WORLD).*$", "", value).strip("_ ")
    if token:
        return f"{token.title()}世界"
    return "未命名世界"

# 游戏字符串中的键有时省略下划线、Plant 前缀或使用不同的内部别名。
STRING_NORMALIZED = {re.sub(r"[^A-Z0-9]", "", key.upper()): value for key, value in STRINGS.items()}


def zh(name: str) -> str:
    raw = str(name or "")
    upper = raw.upper()
    candidates = (upper, "PLANT_" + upper, "PLANTTYPE_" + upper, upper.replace("_", ""))
    for candidate in candidates:
        if candidate in STRINGS:
            return STRINGS[candidate]
        normalized = re.sub(r"[^A-Z0-9]", "", candidate)
        if normalized in STRING_NORMALIZED:
            return STRING_NORMALIZED[normalized]
    return raw or "未命名植物"


def plant_rarity(plant: engine.Plant) -> int:
    # 资源表没有统一的稀有度字段，成本是游戏内最稳定的品质代理。
    if plant.cost <= 50:
        return 0
    if plant.cost <= 100:
        return 1
    if plant.cost <= 175:
        return 2
    if plant.cost <= 250:
        return 3
    return 4


def packet_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_]", "", name).upper()
    candidates = (stem + ".png", stem + "_NEWRARE_1.png", stem + "_AVATAR.png")
    for candidate in candidates:
        if (PACKETS / candidate).exists():
            return candidate
    # 部分老植物没有独立 packet，返回透明占位牌，避免破图图标破坏布局。
    return "EMPTY_PACKET_DYNAMIC.png"


def plant_card(plant: engine.Plant, black: set[str] | None = None, white: set[str] | None = None) -> dict[str, Any]:
    key = plant.name.lower()
    return {
        "id": key, "name": key, "zh": zh(key), "cost": plant.cost,
        "consumable": plant.consumable,
        "rarity": plant_rarity(plant), "image": "/packets/" + packet_name(key),
        "blacklisted": key in (black or set()), "whitelisted": key in (white or set()),
    }


def catalog() -> list[dict[str, Any]]:
    plants, _ = engine.load_plant_catalog(RULES / "PROPERTYSHEETS.JSON")
    black = engine.load_blacklist(RULES / "ARTIFACT.JSON")
    return [plant_card(p, black) for p in plants]


CATALOG = catalog()
CATALOG_BY_NAME = {p["name"]: p for p in CATALOG}


def safe_level_path(value: str | None) -> Path:
    default = RULES / "LEVELS" / "EGYPT1.JSON"
    if not value:
        return default
    if str(value).startswith("upload:"):
        value = str(value).removeprefix("upload:")
        candidate = (UPLOADS / Path(value).name).resolve()
        if UPLOADS.resolve() not in candidate.parents:
            raise ValueError("无效的导入关卡")
        return candidate
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = RULES / "LEVELS" / candidate.name
    candidate = candidate.resolve()
    levels = (RULES / "LEVELS").resolve(); uploads = UPLOADS.resolve()
    if not ((levels in candidate.parents) or (uploads in candidate.parents)) or candidate.suffix.lower() != ".json":
        raise ValueError("关卡必须来自规则/LEVELS目录")
    if not candidate.exists():
        raise ValueError("关卡文件不存在")
    return candidate


def write_board(config: dict[str, Any], mode: str) -> Path:
    if not isinstance(config, dict):
        config = {}
    default_cols, default_rows = (5, 3) if mode == "artifact" else (9, 5)
    data = {
        "world": str(config.get("world", "modern" if mode == "artifact" else "egypt")),
        "columns": int(config.get("columns", default_cols)),
        "rows": int(config.get("rows", default_rows)),
        "airflow_center": config.get("airflow_center", {"col": 3 if mode == "artifact" else 1, "row": 1 if mode == "artifact" else 2}),
        "sunflower_sun": int(config.get("sunflower_sun", 50)),
        "plants": config.get("plants", []), "cells": config.get("cells", []),
    }
    handle = tempfile.NamedTemporaryFile(prefix="artifact-board-", suffix=".json", dir=UPLOADS, delete=False, mode="w", encoding="utf-8")
    json.dump(data, handle, ensure_ascii=False)
    handle.close()
    return Path(handle.name)


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    """删去完整候选排列，只返回前端展示所需的数据。"""
    def compact_record(item: dict[str, Any]) -> dict[str, Any]:
        source = item.get("source") or {}
        return {"position": item.get("position"), "source": {"type": source.get("type"), "zh": zh(str(source.get("type", "")))},
                "source_effective_cost": item.get("source_effective_cost"), "target": item.get("target"),
                "target_zh": zh(str(item.get("target", ""))), "first_random_index": item.get("first_random_index"),
                "stage": (int(item["first_random_index"]) // 25 if isinstance(item.get("first_random_index"), int) else None),
                "eligible_count": len(item.get("eligible_candidates", [])) if isinstance(item.get("eligible_candidates"), list) else item.get("eligible_count")}
    events = []
    for event in result.get("events", []):
        phases = []
        for phase in event.get("phases", []):
            key = "transformed" if phase.get("type") == "active" else "generated"
            phases.append({"type": phase.get("type"), "center": phase.get("center"), "records": [compact_record(x) for x in phase.get(key, [])]})
        events.append({"stage": event.get("stage"), "click_index": event.get("click_index"), "phases": phases})
    debug_phases = []
    for event_index, event in enumerate(result.get("events", []), 1):
        for phase in event.get("phases", []):
            key = "transformed" if phase.get("type") == "active" else "generated"
            raw = phase.get(key, [])
            debug_phases.append({"event": event_index, "stage": event.get("stage"), "type": phase.get("type"), "records": len(raw), "targets": sum(1 for x in raw if x.get("target")), "positions": [x.get("position") for x in raw], "first_indices": [x.get("first_random_index") for x in raw if isinstance(x.get("first_random_index"), int)]})
    debug_input = result.get("_input_board") if isinstance(result.get("_input_board"), dict) else None
    return {"schema": result.get("schema"), "mode": result.get("rule_kind", "artifact"), "world": result.get("world"),
            "tier": result.get("tier", result.get("level")), "click_sequence": result.get("click_sequence"),
            "sunflower_sun": result.get("sunflower_sun"), "center": result.get("center"), "events": events,
            "board": result.get("board"), "level_rules": result.get("level_rules"),
            "generation_debug": {"input_plants": debug_input.get("plants", []) if debug_input else [], "generated_board_plants": result.get("board", {}).get("plants", []) if isinstance(result.get("board"), dict) else [], "rng_draws": result.get("board", {}).get("rng_draws") if isinstance(result.get("board"), dict) else None, "phases": debug_phases},
            "unresolved_property_plants": result.get("unresolved_property_plants", [])}


def config_rules(level_path: Path) -> dict[str, Any]:
    rules = engine.build_level_rules(level_path, RULES / "LEVELMODULES.JSON", RULES / "LEVELEDITOR.JSON")
    prefix = str(rules.get("stage_prefix") or "").upper()
    return {"mode": rules.get("mode"), "mode_label": level_mode_label(level_path.name, rules), "stage_prefix": rules.get("stage_prefix"), "world_zh": stage_world_label(rules.get("stage_prefix"), level_path.name),
            "modules": rules.get("modules", []), "seedbank_blacklist": rules.get("seedbank_blacklist", []),
            "seedbank_whitelist": rules.get("seedbank_whitelist", []), "preset_plants": rules.get("preset_plants", []),
            "editor_exclusions": sorted(rules.get("editor_exclusions", set())),
            "is_artifact_disabled": rules.get("is_artifact_disabled", False)}

def extract_level_board(level_path: Path) -> dict[str, Any]:
    data = engine.read_json(level_path); cells: list[dict[str, Any]] = []; plants: list[dict[str, Any]] = []
    for item in data.get("objects", []) if isinstance(data, dict) else []:
        obj = item.get("objdata") if isinstance(item, dict) else None
        if not isinstance(obj, dict): continue
        kind = str(item.get("objclass", ""))
        if kind == "InitialGridItemProperties":
            for placement in obj.get("InitialGridItemPlacements", []) or []:
                if not isinstance(placement, dict): continue
                try: col, row = int(placement.get("GridX", 0)), int(placement.get("GridY", 0))
                except (TypeError, ValueError): continue
                name = str(placement.get("TypeName", "grid_item")); lower = name.lower()
                cells.append({"col": col, "row": row, "terrain": "water" if "lilypad" in lower else "ground", "blocked": "lilypad" not in lower, "special": True, "grid_item": name})
        if kind == "InitialPlantProperties":
            for placement in obj.get("InitialPlantPlacements", []) or []:
                if not isinstance(placement, dict) or not placement.get("TypeName"): continue
                try: col, row = int(placement.get("GridX", 0)), int(placement.get("GridY", 0))
                except (TypeError, ValueError): continue
                plants.append({"type": str(placement["TypeName"]).lower(), "col": col, "row": row})
    return {"columns": 9, "rows": 5, "cells": cells, "plants": plants}


def generate_artifact_interface(level: Path, sequence: str, sun: int, first_index_override: int | None = None,
                                center_col: int | None = None, center_row: int | None = None) -> dict[str, Any]:
    """按神器固定界面回放共享点击序列。

    一阶每次显示 3×3 九棵向日葵；三、四阶显示中心左侧的一竖三棵。
    每次界面切换不重置 RNG，因此 sequence 中各阶共享同一随机流。
    """
    paths = engine.default_paths()
    plants, unresolved = engine.load_plant_catalog(paths["properties"])
    rules = engine.build_level_rules(level, paths["levelmodules"], paths["leveleditor"])
    engine.require_artifact_enabled(rules)
    blacklist = engine.load_blacklist(paths["artifact"])
    native = engine.load_native_rules(plants)
    # 中心必须来自当前请求；旧实现固定为 (3, 1)，导致前端换中心后仍在旧位置生成。
    center = (int(center_col), int(center_row)) if center_col is not None and center_row is not None else (3, 1)
    board = engine.Board("modern", 5, 3, [], {}, False, center)
    session = engine.create_session(board, plants, blacklist, engine.DEFAULT_SEED, 30, rules, native, "modern", True, False, engine.load_lilypad_blocklist(paths["properties"]))
    if first_index_override is not None:
        session.rng.forced_first_index = max(0, int(first_index_override))
    events: list[dict[str, Any]] = []
    valid = "".join(item for item in sequence if item in "134") or "1"
    for click_index, item in enumerate(valid, 1):
        stage = int(item)
        if stage == 1:
            positions = [(center[0] + dx, center[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        else:
            positions = [(center[0] - 1, center[1] + dy) for dy in (-1, 0, 1)]
            # 三、四阶直接进入固定的高阶界面，不能再触发一阶 3×3 扫描。
            session._progress("artifact_evolution").primed_stage1 = True
        # 直接点击 3/4 阶时，固定界面已经处于对应高阶状态；不能补跑 1 阶。
        progress = session._progress("artifact_evolution")
        progress.primed_stage1 = True
        if stage == 3:
            progress.primed_stage3 = False
        if stage == 4:
            progress.primed_stage4 = False
        session.board.plants = [engine.Instance("sunflower", col, row, sun) for col, row in positions]
        events.append(session.click(stage, center, "artifact_evolution"))
    return {
        "schema": "artifact_evolution_reproduction_v2", "level_file": str(level), "world": "modern",
        "tier": int(valid[-1]), "level": 30 if "4" in valid else 20 if "3" in valid else 1,
        "click_sequence": valid, "sunflower_sun": sun, "seed": engine.DEFAULT_SEED,
        "center": {"col": center[0], "row": center[1]}, "level_rules": engine.serializable(engine.static_native_rules(rules, native)),
        "unresolved_property_plants": sorted(unresolved), "events": events,
        "board": {"columns": board.columns, "rows": board.rows, "world": board.world,
                  "plants": [item.json() for item in board.plants], "rng_draws": session.rng.draws,
                  "coordinate_rng_draws": session.coord_rng.draws},
    }


def generate(payload: dict[str, Any]) -> dict[str, Any]:
    # 开发中的复刻规则可在服务运行后更新；生成前重载以避免网页使用新界面、
    # 后端却仍保留旧 ArtifactSession 逻辑的版本错配。
    global engine
    engine = importlib.reload(engine)
    mode = str(payload.get("mode", "artifact")).lower()
    board_path = write_board(payload.get("board", {}), mode)
    level = safe_level_path(payload.get("level_file"))
    world = str(payload.get("world") or engine.infer_world(level))
    if world in {"—", "世界", "unknown", "None"}:
        world = engine.infer_world(level)
    sun = int(payload.get("sunflower_sun", payload.get("board", {}).get("sunflower_sun", 50)))
    center = payload.get("center") or {}
    kwargs = {"center_col": int(center["col"]), "center_row": int(center["row"])} if "col" in center and "row" in center else {}
    try:
        if mode == "level":
            step = max(0, int(payload.get("level_generation_step", 0)))
            override = payload.get("first_index_override")
            result = engine.generate_level_reproduction(level, int(payload.get("tier", 4)), board_path, None, world, "artifact_evolution", sun, seed=engine.DEFAULT_SEED + step * 9973, first_index_override=int(override) if override is not None else None, **kwargs)
        else:
            sequence = str(payload.get("sequence") or str(payload.get("tier", 1)))
            override = payload.get("first_index_override")
            result = generate_artifact_interface(
                level, sequence, sun, int(override) if override is not None else None,
                kwargs.get("center_col"), kwargs.get("center_row"),
            )
        result["_input_board"] = payload.get("board", {})
        return compact_result(result)
    finally:
        board_path.unlink(missing_ok=True)


def pool(payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode", "level")).lower(); board_path = write_board(payload.get("board", {}), mode)
    level = safe_level_path(payload.get("level_file")); world = str(payload.get("world") or engine.infer_world(level)); sun = int(payload.get("sunflower_sun", 100))
    board = engine.load_board(board_path, world, False); plants, unresolved = engine.load_plant_catalog(RULES / "PROPERTYSHEETS.JSON")
    rules = engine.build_level_rules(level, RULES / "LEVELMODULES.JSON", RULES / "LEVELEDITOR.JSON"); native = engine.load_native_rules(plants)
    if mode == "artifact":
        board.world = "modern"; session = engine.create_session(board, plants, engine.load_blacklist(RULES / "ARTIFACT.JSON"), engine.DEFAULT_SEED, 30, rules, native, "modern", True, False, engine.load_lilypad_blocklist(RULES / "PROPERTYSHEETS.JSON"))
    else:
        board.world = engine.world_id(world); session = engine.create_session(board, plants, engine.load_blacklist(RULES / "ARTIFACT.JSON"), engine.DEFAULT_SEED, 30, rules, native, None, False, True, engine.load_lilypad_blocklist(RULES / "PROPERTYSHEETS.JSON"))
    center = payload.get("center") or {"col": board.center()[0], "row": board.center()[1]}; center_t = (int(center["col"]), int(center["row"]))
    sequence = str(payload.get("sequence", ""));
    if sequence:
        engine.generate_click_sequence(session, sequence, center_t, "artifact_evolution")
    pos = payload.get("position") or {"col": center_t[0], "row": center_t[1]}; col, row = int(pos["col"]), int(pos["row"])
    eligible, rejected = session.candidates(sun, col, row, True); shuffled, first = session.rng.shuffle_trace(eligible)
    by_name = {plant.name: plant for plant in plants}
    return {"position": {"col": col, "row": row}, "sun": sun, "eligible_count": len(eligible), "first_random_index": first, 
            "stage": (first // 25 if isinstance(first, int) else None), "selected": shuffled[0] if shuffled else None,
            "selected_zh": zh(shuffled[0]) if shuffled else None, "plants": [plant_card(by_name[x]) for x in eligible],
            "rejected_plants": [plant_card(by_name[x]) for x in rejected if x in by_name],
            "rejected_count": len(rejected), "unresolved": sorted(unresolved)}


def index_plants(payload: dict[str, Any]) -> dict[str, Any]:
    """返回独立“植物索引”菜单使用的完整主动候选池。

    这里故意不读取或推进任何神器 RNG 状态：索引就是实际
    ``first_random_index`` 使用的 0-based 候选数组位置，因此该菜单可以
    直接拿同一个首索引复刻。
    """
    level = safe_level_path(payload.get("level_file"))
    world = str(payload.get("world") or engine.infer_world(level))
    if world in {"—", "世界", "unknown", "None"}:
        world = engine.infer_world(level)
    sun = max(0, int(payload.get("sunflower_sun", 20)))
    mode = str(payload.get("mode", "level")).lower()
    rules = engine.build_level_rules(level, RULES / "LEVELMODULES.JSON", RULES / "LEVELEDITOR.JSON")
    plants, unresolved = engine.load_plant_catalog(RULES / "PROPERTYSHEETS.JSON")
    native = engine.load_native_rules(plants)
    blacklist = engine.load_blacklist(RULES / "ARTIFACT.JSON")

    board_config = payload.get("board") if isinstance(payload.get("board"), dict) else {}
    # 未提供自定义棋盘时使用关卡中解析出的地形/障碍，保证导入关卡也能影响候选数量。
    if not board_config:
        board_config = extract_level_board(level)
    board_path = write_board(board_config, mode)
    try:
        board = engine.load_board(board_path, engine.world_id(world), False)
    finally:
        board_path.unlink(missing_ok=True)
    board.world = engine.world_id(world)
    center = payload.get("center") or {"col": board.center()[0], "row": board.center()[1]}
    col, row = int(center.get("col", board.center()[0])), int(center.get("row", board.center()[1]))
    session = engine.create_session(
        board, plants, blacklist, engine.DEFAULT_SEED, 1, rules, native,
        None, False, True, engine.load_lilypad_blocklist(RULES / "PROPERTYSHEETS.JSON"),
    )
    pool_data = engine.build_index_candidate_pool(session, sun, col, row)
    eligible, rejected = pool_data["eligible"], pool_data["rejected"]
    by_name = {plant.name: plant for plant in plants}
    entries = []
    # 首次 Fisher-Yates 交换决定 result[0]，强制首索引 i 时最终植物为 eligible[i]。
    for index, name in enumerate(eligible):
        plant = by_name.get(name)
        if plant is not None:
            item = plant_card(plant, blacklist)
            item.update(index=index, sun=sun)
            entries.append(item)
    mode_label = level_mode_label(level.name, rules) or "普通关卡"
    world_label = stage_world_label(rules.get("stage_prefix"), level.name)
    return {
        "level": level.name, "level_file": str(level), "world": world,
        "world_zh": world_label, "mode": rules.get("mode") or mode,
        "mode_label": mode_label, "sun": sun, "position": {"col": col, "row": row},
        "count": len(entries), "plants": entries,
        "skipped": len(eligible) - len(entries), "rejected_count": len(rejected),
        "unresolved": sorted(unresolved),
    }


def stage_sun_plants(payload: dict[str, Any]) -> dict[str, Any]:
    """按 25 阳光为一个阶段，查询同一首索引在不同阳光下的植物。"""
    level = safe_level_path(payload.get("level_file"))
    world = str(payload.get("world") or engine.infer_world(level))
    if world in {"—", "世界", "unknown", "None"}:
        world = engine.infer_world(level)
    source_index = max(0, int(payload.get("source_index", 0)))
    mode = str(payload.get("mode", "level")).lower()
    rules = engine.build_level_rules(level, RULES / "LEVELMODULES.JSON", RULES / "LEVELEDITOR.JSON")
    plants, unresolved = engine.load_plant_catalog(RULES / "PROPERTYSHEETS.JSON")
    native = engine.load_native_rules(plants)
    blacklist = engine.load_blacklist(RULES / "ARTIFACT.JSON")
    board_config = payload.get("board") if isinstance(payload.get("board"), dict) else {}
    if not board_config:
        board_config = extract_level_board(level)
    board_path = write_board(board_config, mode)
    try:
        board = engine.load_board(board_path, engine.world_id(world), False)
    finally:
        board_path.unlink(missing_ok=True)
    board.world = engine.world_id(world)
    center = payload.get("center") or {"col": board.center()[0], "row": board.center()[1]}
    col, row = int(center.get("col", board.center()[0])), int(center.get("row", board.center()[1]))
    by_name = {plant.name: plant for plant in plants}
    stages = []
    for sun in range(0, 501, 25):
        session = engine.create_session(
            board, plants, blacklist, engine.DEFAULT_SEED, 1, rules, native,
            None, False, True, engine.load_lilypad_blocklist(RULES / "PROPERTYSHEETS.JSON"),
        )
        pool_data = engine.build_index_candidate_pool(session, sun, col, row)
        eligible = pool_data["eligible"]
        selected_name = eligible[source_index] if source_index < len(eligible) else None
        selected = None
        if selected_name in by_name:
            selected = plant_card(by_name[selected_name], blacklist)
            selected.update(index=source_index, sun=sun, stage=sun // 25)
        stages.append({"stage": sun // 25, "sun": sun, "candidate_count": len(eligible), "plant": selected})
    available = sum(1 for item in stages if item["plant"] is not None)
    return {
        "level": level.name, "level_file": str(level), "world": world,
        "world_zh": stage_world_label(rules.get("stage_prefix"), level.name),
        "mode": rules.get("mode") or mode, "mode_label": level_mode_label(level.name, rules) or "普通关卡",
        "source_index": source_index, "position": {"col": col, "row": row},
        # 不返回没有对应植物的阶段，前端只渲染实际存在的植物。
        "stages": [item for item in stages if item["plant"] is not None], "available": available, "total": len(stages),
        "unresolved": sorted(unresolved),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ArtifactWeb/1.0"
    def log_message(self, *_: Any) -> None: return
    def send_json(self, value: Any, status: int = 200) -> None:
        try:
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 浏览器刷新/取消请求时会主动关闭连接，避免错误处理再次写入已关闭的 socket。
            return
    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"));
        if length > MAX_UPLOAD: raise ValueError("请求过大")
        raw = self.rfile.read(length); return json.loads(raw.decode("utf-8")) if raw else {}
    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path); path = unquote(parsed.path)
            if path == "/api/catalog": return self.send_json({"plants": CATALOG, "artifact_blacklist": sorted(engine.load_blacklist(RULES / "ARTIFACT.JSON"))})
            if path == "/api/levels":
                levels = []
                for item in sorted((RULES / "LEVELS").glob("*.JSON")):
                    try: rules = engine.load_level_rules(item, RULES / "LEVELMODULES.JSON")
                    except Exception: rules = {"mode": "standard", "modules": []}
                    mode = level_mode_label(item.name, rules)
                    world = stage_world_label(rules.get("stage_prefix"), item.name)
                    display = world if not mode else f"{world} · {mode}"
                    levels.append({"name": item.name, "label": display, "short_label": display, "mode_label": mode, "path": item.name})
                return self.send_json({"levels": levels})
            if path == "/api/rules":
                level = safe_level_path(parse_qs(parsed.query).get("level", [None])[0]); return self.send_json({"level": level.name, "rules": config_rules(level), "artifact_blacklist": sorted(engine.load_blacklist(RULES / "ARTIFACT.JSON"))})
            if path == "/api/level-map":
                level = safe_level_path(parse_qs(parsed.query).get("level", [None])[0]); return self.send_json(extract_level_board(level))
            if path.startswith("/packets/"):
                name = Path(path.removeprefix("/packets/")).name.upper(); file = PACKETS / name
                if file.exists() and file.suffix.lower() == ".png": return self.send_file(file)
            if path == "/" or path == "/index.html": return self.send_file(WEB / "index.html")
            if path.startswith("/static/"):
                file = (WEB / path.removeprefix("/static/")).resolve();
                if WEB.resolve() in file.parents and file.exists(): return self.send_file(file)
            self.send_json({"error": "not found"}, 404)
        except Exception as exc: self.send_json({"error": str(exc)}, 400)
    def send_file(self, file: Path) -> None:
        try:
            raw = file.read_bytes(); self.send_response(200); self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream"); self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "public, max-age=86400"); self.end_headers(); self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path; payload = self.body()
            if path == "/api/generate": return self.send_json(generate(payload))
            if path == "/api/pool": return self.send_json(pool(payload))
            if path == "/api/index-plants": return self.send_json(index_plants(payload))
            if path == "/api/stage-sun-plants": return self.send_json(stage_sun_plants(payload))
            if path == "/api/import-level":
                content = str(payload.get("content", "")); json.loads(content); name = Path(str(payload.get("name", "upload.JSON"))).name
                if len(content.encode()) > MAX_UPLOAD: raise ValueError("关卡文件过大")
                if not name.lower().endswith(".json"): name += ".JSON"
                stored = uuid.uuid4().hex + "-" + name; path = UPLOADS / stored; path.write_text(content, encoding="utf-8"); return self.send_json({"id": "upload:" + stored, "name": name})
            self.send_json({"error": "not found"}, 404)
        except Exception as exc: self.send_json({"error": str(exc)}, 400)


def main() -> int:
    parser = argparse.ArgumentParser(description="Artifact 复刻本地 Web 服务")
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(); server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Artifact Web: http://{args.host}:{args.port}/")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
