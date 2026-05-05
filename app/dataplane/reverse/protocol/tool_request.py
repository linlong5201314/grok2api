import json
from typing import Any

from app.platform.errors import ValidationError
from .tool_parser import ParsedToolCall

_SUPPORTED_STRICT_SCHEMA_TYPES = {
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
}


def normalize_openai_tools(
    tools: list[dict[str, Any]] | None,
    functions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    normalized: list[dict[str, Any]] = []
    for tool in tools or []:
        if (
            isinstance(tool, dict)
            and tool.get("type") == "function"
            and isinstance(tool.get("function"), dict)
        ):
            normalized.append(tool)
        elif isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name"):
            normalized.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters"),
                    "strict": tool.get("strict"),
                },
            })
    for function in functions or []:
        if isinstance(function, dict) and function.get("name"):
            normalized.append({"type": "function", "function": function})
    return normalized or None


def normalize_openai_tool_choice(tool_choice: Any, function_call: Any) -> Any:
    if tool_choice is not None:
        if isinstance(tool_choice, dict) and tool_choice.get("name") and not tool_choice.get("type"):
            return {"type": "function", "function": {"name": tool_choice["name"]}}
        return tool_choice
    if function_call is None or function_call == "auto":
        return "auto"
    if function_call == "none":
        return "none"
    if isinstance(function_call, dict) and function_call.get("name"):
        return {"type": "function", "function": {"name": function_call["name"]}}
    return "auto"


def tool_choice_allows_calls(tool_choice: Any) -> bool:
    if tool_choice == "none":
        return False
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "none":
        return False
    return True


def allowed_tool_names(tools: list[dict[str, Any]] | None, tool_choice: Any) -> list[str]:
    if not tool_choice_allows_calls(tool_choice):
        return []
    names: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if name:
            names.append(name)
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        forced_name = _forced_tool_choice_name(tool_choice)
        if not forced_name:
            raise ValidationError("tool_choice.function.name is required", param="tool_choice")
        if forced_name not in names:
            raise ValidationError(f"tool_choice references unknown tool {forced_name!r}", param="tool_choice")
        return [forced_name]
    if _tool_choice_requires_tools(tool_choice) and not names:
        raise ValidationError("tool_choice requires at least one valid tool", param="tool_choice")
    return names


def validate_tool_choice_tools(tools: list[dict[str, Any]] | None, tool_choice: Any) -> None:
    if not _tool_choice_requires_tools(tool_choice):
        return
    if not tools:
        raise ValidationError("tool_choice requires tools to be provided", param="tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        if not _forced_tool_choice_name(tool_choice):
            raise ValidationError("tool_choice.function.name is required", param="tool_choice")


def validate_strict_tools(tools: list[dict[str, Any]] | None) -> set[str]:
    strict_names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        if not isinstance(function, dict):
            continue
        if function.get("strict") is not True:
            continue
        name = str(function.get("name") or "").strip()
        if name:
            strict_names.add(name)
        schema = function.get("parameters") or {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        error = _strict_schema_error(schema)
        if error:
            raise ValidationError(
                f"invalid strict tool schema for {name or 'unnamed function'}: {error}",
                param="tools",
            )
    return strict_names


def strict_tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        if not isinstance(function, dict) or function.get("strict") is not True:
            continue
        name = str(function.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def normalize_parsed_tool_calls(
    tool_calls: list[ParsedToolCall],
    tools: list[dict[str, Any]] | None,
) -> list[ParsedToolCall]:
    if not tool_calls:
        return tool_calls
    tools_by_name = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        if isinstance(function, dict) and function.get("name"):
            tools_by_name[function.get("name")] = function
    normalized: list[ParsedToolCall] = []
    for call in tool_calls:
        function = tools_by_name.get(call.name) or {}
        if function.get("strict") is not True:
            normalized.append(call)
            continue
        schema = function.get("parameters") or {}
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        try:
            args = json_loads_object(call.arguments)
        except ValueError:
            args = {}
        args = {key: value for key, value in args.items() if key in properties}
        for key in required:
            args.setdefault(key, None)
        normalized.append(
            ParsedToolCall.make(call.name, args if args is not None else {}, call_id=call.call_id)
        )
    return normalized


def json_loads_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except Exception as exc:
        raise ValueError from exc
    if not isinstance(parsed, dict):
        raise ValueError
    return parsed


def _forced_tool_choice_name(tool_choice: dict[str, Any]) -> str:
    function = tool_choice.get("function") or {}
    if not isinstance(function, dict):
        return ""
    return str(function.get("name") or "").strip()


def _tool_choice_requires_tools(tool_choice: Any) -> bool:
    return tool_choice == "required" or (
        isinstance(tool_choice, dict)
        and tool_choice.get("type") in {"function", "required"}
    )


def _strict_schema_error(schema: Any, path: str = "$") -> str:
    if not isinstance(schema, dict):
        return f"{path} must be a JSON schema object"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        for item_type in schema_type:
            if item_type not in _SUPPORTED_STRICT_SCHEMA_TYPES:
                return f"{path}.type {item_type!r} is not supported in strict mode"
    elif schema_type and schema_type not in _SUPPORTED_STRICT_SCHEMA_TYPES:
        return f"{path}.type {schema_type!r} is not supported in strict mode"
    if "anyOf" in schema:
        any_of = schema.get("anyOf")
        if not isinstance(any_of, list) or not any_of:
            return f"{path}.anyOf must be a non-empty array"
        for index, item in enumerate(any_of):
            error = _strict_schema_error(item, f"{path}.anyOf[{index}]")
            if error:
                return error
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            return f"{path}.properties must be an object"
        required = schema.get("required") or []
        if not isinstance(required, list):
            return f"{path}.required must be an array"
        missing_required = [name for name in properties if name not in required]
        if missing_required:
            return f"{path}.required must include every property in strict mode: {', '.join(missing_required)}"
        unknown_required = [name for name in required if name not in properties]
        if unknown_required:
            return f"{path}.required contains undeclared properties: {', '.join(unknown_required)}"
        if schema.get("additionalProperties") is not False:
            return f"{path}.additionalProperties must be false in strict mode"
        for name, prop_schema in properties.items():
            error = _strict_schema_error(prop_schema, f"{path}.properties.{name}")
            if error:
                return error
    if schema_type == "array" and isinstance(schema.get("items"), dict):
        return _strict_schema_error(schema["items"], f"{path}.items")
    return ""


__all__ = [
    "normalize_openai_tools",
    "normalize_openai_tool_choice",
    "tool_choice_allows_calls",
    "allowed_tool_names",
    "validate_tool_choice_tools",
    "validate_strict_tools",
    "strict_tool_names",
    "normalize_parsed_tool_calls",
]
