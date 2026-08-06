"""Demo tools available to the agent.

Each tool is a plain Python function decorated with `@tool` — the docstring
is sent to the LLM as the tool description, so keep it accurate.
Replace or extend these with real integrations as needed.
"""

import ast
import operator
from datetime import datetime, timedelta, timezone

from langchain.tools import tool

# ---------------------------------------------------------------------------
# Safe arithmetic evaluation for the calculator tool
# ---------------------------------------------------------------------------
_ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPERATORS:
        return _ALLOWED_OPERATORS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPERATORS:
        return _ALLOWED_OPERATORS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式: {ast.dump(node)}")


@tool
def get_current_time(utc_offset_hours: float = 8) -> str:
    """查询当前日期和时间。utc_offset_hours 是 UTC 时区偏移（小时），默认东八区（北京时间）。"""
    tz = timezone(timedelta(hours=utc_offset_hours))
    now = datetime.now(tz)
    sign = "+" if utc_offset_hours >= 0 else "-"
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} (UTC{sign}{abs(utc_offset_hours):02.0f})"


@tool
def calculate(expression: str) -> str:
    """计算一个数学表达式，例如 "2 + 3 * 4"、"(10 - 2) / 4"、"2 ** 10"。只支持加减乘除、取余、幂运算。"""
    try:
        result = _safe_eval(ast.parse(expression.strip(), mode="eval"))
    except Exception as exc:  # noqa: BLE001 - report any parse/eval error to the LLM
        return f"表达式无法计算: {exc}"
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return str(result)


@tool
def get_weather(city: str) -> str:
    """查询指定城市的当前天气（演示用模拟数据，非真实天气）。city 为城市名，例如 "北京"、"上海"、"Tokyo"。"""
    mock_weather = {
        "北京": "晴，27°C，西北风 3 级",
        "上海": "多云，31°C，东南风 2 级",
        "深圳": "雷阵雨，33°C，湿度 85%",
        "杭州": "小雨，29°C，东风 2 级",
        "tokyo": "Cloudy, 26°C, light breeze",
        "new york": "Sunny, 24°C, calm",
    }
    key = city.strip().lower()
    for name, weather in mock_weather.items():
        if name in key or key in name:
            return f"{city}：{weather}"
    return f"{city}：晴转多云，25°C，微风（演示数据）"


ALL_TOOLS = [get_current_time, calculate, get_weather]
