"""
Type Coercion — 修复 LLM 返回的参数类型错误。

V4 核心问题：LLM 经常返回 "42" 而非 42，"true" 而非 true。
本模块读取工具的 JSON Schema，对每个参数尝试类型强制转换。

设计原则：
- 只转换 string → 其他类型（非 string 值直接跳过）
- 转换失败保留原值，不抛异常，不破坏调用
- 支持 union type（如 "type": ["integer", "string"]）

使用方式：
    from tools.coerce import coerce_args
    coerced = coerce_args(tool_schema, raw_args)
"""

import json
import math


def coerce_args(schema: dict, args: dict) -> dict:
    """根据 JSON Schema 对 args 中的字符串值做类型强制转换。

    遍历 schema["parameters"]["properties"]，对每个 key：
    - 如果 args 中的值是 string，但 schema 声明了非 string 类型 → 尝试转换
    - 转换失败 → 保留原值
    """
    properties = schema.get("parameters", {}).get("properties", {})
    if not properties or not args:
        return args

    coerced = dict(args)
    for key, prop_schema in properties.items():
        if key not in coerced:
            continue
        value = coerced[key]
        # 只对 string 值做转换（非 string 说明类型已经正确）
        if not isinstance(value, str):
            continue

        expected_type = prop_schema.get("type")
        if expected_type is None:
            continue

        coerced[key] = _coerce_value(value, expected_type)

    return coerced


def _coerce_value(value: str, expected_type):
    """根据 expected_type 分发到具体转换函数。

    expected_type 可以是：
    - 单个类型字符串："integer", "number", "boolean", "array", "object"
    - 类型列表（union）：["integer", "string"]
    """
    # union type：按顺序尝试，第一个成功的就用
    if isinstance(expected_type, list):
        for t in expected_type:
            if t == "string":
                return value
            result = _coerce_single(value, t)
            if result is not value:
                return result
        return value

    # 单类型
    if expected_type == "string":
        return value
    return _coerce_single(value, expected_type)


def _coerce_single(value: str, type_name: str):
    """尝试将 string 转换为指定类型。失败返回原值。"""
    if type_name == "integer":
        return _coerce_int(value)
    elif type_name == "number":
        return _coerce_number(value)
    elif type_name == "boolean":
        return _coerce_bool(value)
    elif type_name in ("array", "object"):
        return _coerce_json(value)
    elif type_name == "null":
        if value.strip().lower() == "null":
            return None
        return value
    return value


def _coerce_int(value: str):
    """字符串 → int。"42" → 42, "3.0" → 3, "abc" → "abc"（保留原值）。"""
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return value
        if f == int(f):
            return int(f)
        return value
    except (ValueError, OverflowError):
        return value


def _coerce_number(value: str):
    """字符串 → number。"3.14" → 3.14, "42" → 42, "abc" → "abc"。"""
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return value
        # 整数值返回 int（更干净）
        if f == int(f) and "." not in value and "e" not in value.lower():
            return int(f)
        return f
    except (ValueError, OverflowError):
        return value


def _coerce_bool(value: str):
    """字符串 → bool。"true" → True, "false" → False（不区分大小写）。"""
    lower = value.strip().lower()
    if lower == "true":
        return True
    elif lower == "false":
        return False
    return value


def _coerce_json(value: str):
    """字符串 → list/dict。尝试 JSON 解析，失败保留原值。"""
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (list, dict)):
            return parsed
        return value
    except (json.JSONDecodeError, ValueError):
        return value
