"""Stage 4: serve.

Loads a manifest and exposes each verified action as an MCP tool. No model is
in this path -- the server fills a template, sends the request, runs the
extractor and returns JSON. Claude ran at compile time or not at all.
"""

from __future__ import annotations

import inspect
import json
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .compile import load_manifest
from .manifest import Action, Manifest, PropertySchema
from .runtime.http import invoke_action

CONFIRM_NOTE = (
    "This action cannot be undone. Call it again with confirm=true to execute it."
)


def servable(manifest: Manifest) -> list[Action]:
    """No ``passed``, no tool.

    An action that failed its compile-time check is absent from the tool list
    entirely, rather than shipped broken for an agent to discover at runtime.
    """
    return [a for a in manifest.actions if a.check is not None and a.check.passed]


def _annotation_for(prop: PropertySchema) -> Any:
    """Maps one JSON Schema property onto a Python annotation.

    MCPServer derives a tool's schema from its handler signature, so the
    manifest's declared types, enums and constraints are expressed as
    annotations and handed back to MCP as schema.
    """
    if prop.enum:
        return Literal[tuple(prop.enum)]  # type: ignore[misc]

    constraints: dict[str, Any] = {}
    if prop.description:
        constraints["description"] = prop.description
    if prop.pattern:
        constraints["pattern"] = prop.pattern
    if prop.maxLength:
        constraints["max_length"] = prop.maxLength
    if prop.minimum is not None:
        constraints["ge"] = prop.minimum
    if prop.maximum is not None:
        constraints["le"] = prop.maximum

    base: Any = {
        "string": str,
        "number": float,
        "integer": int,
        "boolean": bool,
    }[prop.type]

    return Annotated[base, Field(**constraints)] if constraints else base


def _make_handler(action: Action):
    """Builds a callable whose signature mirrors the action's input schema."""

    async def handler(**kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None}
        confirmed = bool(args.pop("confirm", False))

        if action.tier == "irreversible" and not confirmed:
            return json.dumps({
                "status": "confirmation_required",
                "action": action.name,
                "message": CONFIRM_NOTE,
                "arguments": args,
            }, indent=2)

        result = await invoke_action(action, args)
        return json.dumps({
            "action": action.name,
            "tier": action.tier,
            "status": result.status,
            "items_found": result.items_found,
            "records": result.records,
            "source": result.url,
        }, indent=2, ensure_ascii=False)

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    for name, prop in action.input.properties.items():
        annotation = _annotation_for(prop)
        required = name in action.input.required
        annotations[name] = annotation
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=inspect.Parameter.empty if required else (prop.default or None),
            )
        )

    if action.tier == "irreversible":
        # The gate belongs in the schema an agent reads, not only in the
        # error it gets back after trying.
        confirm_annotation = Annotated[
            bool, Field(description="Must be true to execute this irreversible action.")
        ]
        annotations["confirm"] = confirm_annotation
        params.append(
            inspect.Parameter(
                "confirm", inspect.Parameter.KEYWORD_ONLY,
                annotation=confirm_annotation, default=False,
            )
        )

    # Required params must precede optional ones in a signature.
    params.sort(key=lambda p: p.default is not inspect.Parameter.empty)

    handler.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    handler.__annotations__ = annotations
    handler.__name__ = action.name
    handler.__doc__ = action.description
    return handler


def build_server(host: str) -> MCPServer:
    manifest = load_manifest(host)
    server = MCPServer(name=f"shim-{host}", version="0.1.0")

    for action in servable(manifest):
        description = action.description
        if action.tier == "irreversible":
            description = f"{description} {CONFIRM_NOTE}"

        server.add_tool(
            _make_handler(action),
            name=action.name,
            description=description,
            annotations=ToolAnnotations(
                readOnlyHint=action.tier == "read",
                destructiveHint=action.tier == "irreversible",
                idempotentHint=action.tier == "read",
            ),
        )

    # Procedures are MCP prompts: instructions an agent follows with the tools.
    for index, proc in enumerate(manifest.procedures):
        lines = [f"Goal: {proc.intent}", ""]
        for i, step in enumerate(proc.steps, 1):
            if step.action:
                lines.append(f"{i}. Call the tool `{step.action}` with {json.dumps(step.with_)}.")
            elif step.url:
                lines.append(f"{i}. Read {step.url}.")
            else:
                lines.append(f"{i}. {step.note}")
        text = "\n".join(lines)

        # Zero-argument closure: from_function turns any parameter into a
        # prompt argument, so the text must be captured, not passed.
        def _make(captured: str):
            def howto() -> str:
                return captured
            return howto

        _prompt = _make(text)
        try:
            from mcp.server.mcpserver.prompts.base import Prompt
            server.add_prompt(Prompt.from_function(
                _prompt, name=f"howto_{index + 1}", description=proc.intent))
        except Exception as exc:  # prompts are a bonus; never block the tools
            import sys
            print(f"prompt registration failed: {exc}", file=sys.stderr)

    return server


def run_stdio(host: str) -> None:
    import anyio
    anyio.run(build_server(host).run_stdio_async)
