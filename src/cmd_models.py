"""CLI for persistent model routing preferences."""

from __future__ import annotations

import argparse
import json
from typing import Any

from model_prefs import (
    FEATURE_LABELS,
    apply_chat_opus_preset,
    doctor_findings,
    model_label,
    reset_feature_route,
    resolve_model_route,
    routes_summary,
    set_feature_route,
    set_profile_route,
)


def cmd_models(args: argparse.Namespace) -> None:
    """Handle the ``models`` subcommand."""
    action = getattr(args, "models_cmd", None)
    if action is None:
        _show_status(json_output=args.json)
        return
    if action == "preset":
        if args.name != "chat-opus":
            raise SystemExit("Unknown preset. Use: chat-opus")
        apply_chat_opus_preset()
        print("Applied preset: chat-opus")
        _show_feature("chat")
        return
    if action == "reset":
        reset_feature_route(args.feature)
        print(f"Reset {args.feature}.")
        _show_feature(args.feature)
        return
    if action == "profile":
        set_profile_route(args.profile, primary=args.primary, fallback=args.fallback)
        print(f"Updated {args.profile} profile.")
        _show_status(json_output=False)
        return
    if action == "set":
        set_feature_route(
            args.feature,
            primary=args.primary,
            fallback=args.fallback,
            reasoning_effort=_parse_reasoning(args.reasoning),
            temperature=_parse_temperature(args.temperature),
        )
        print(f"Updated {args.feature}.")
        _show_feature(args.feature)
        return
    if action == "doctor":
        findings = doctor_findings()
        if not findings:
            print("Model routing looks OK.")
            return
        print("Model routing findings:")
        for finding in findings:
            print(f"- {finding}")
        return
    raise SystemExit(f"Unknown models command: {action}")


def _show_feature(feature: str) -> None:
    route = resolve_model_route(feature)
    print(
        f"{FEATURE_LABELS.get(feature, feature)}: "
        f"{route.primary} -> {route.fallback or 'none'}"
    )


def _show_status(*, json_output: bool) -> None:
    routes = routes_summary()
    if json_output:
        payload = [_route_to_dict(route) for route in routes]
        print(json.dumps(payload, indent=2))
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title="Model Routes", show_lines=False)
    table.add_column("Feature", style="cyan")
    table.add_column("Primary")
    table.add_column("Fallback")
    table.add_column("Params", style="dim")
    table.add_column("Profile", style="dim")
    for route in routes:
        params = []
        if "reasoning_effort" in route.params:
            params.append(f"reasoning={route.reasoning_effort or 'none'}")
        if "temperature" in route.params:
            params.append(
                "temperature=omit"
                if route.temperature is None
                else f"temperature={route.temperature:g}"
            )
        table.add_row(
            FEATURE_LABELS.get(route.feature, route.feature),
            model_label(route.primary),
            model_label(route.fallback) if route.fallback else "none",
            ", ".join(params) or "-",
            route.profile,
        )
    Console().print(table)


def _route_to_dict(route: Any) -> dict[str, Any]:
    return {
        "feature": route.feature,
        "primary": route.primary,
        "fallback": route.fallback,
        "profile": route.profile,
        "params": route.params,
        "source": route.source,
    }


def _parse_reasoning(value: str | None) -> str | None | object:
    if value is None:
        return ...
    if value == "none":
        return None
    return value


def _parse_temperature(value: str | None) -> float | None | object:
    if value is None:
        return ...
    if value == "omit":
        return None
    return float(value)
