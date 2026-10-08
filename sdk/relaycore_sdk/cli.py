from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence, cast

from relaycore_sdk.client import RelayCoreAPIError, RelayCoreClient, RelayCoreConnectionError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relaycore", description="Operate RelayCore workflows from your terminal.")
    parser.add_argument("--url", default=os.environ.get("RELAYCORE_URL"), help="API base URL (or RELAYCORE_URL).")
    parser.add_argument("--workspace", default=os.environ.get("RELAYCORE_WORKSPACE_ID"),
                        help="Workspace ID (or RELAYCORE_WORKSPACE_ID).")
    parser.add_argument("--timeout", type=float, default=10.0, help="Request timeout in seconds.")
    commands = parser.add_subparsers(dest="area", required=True)

    commands.add_parser("workspaces", help="List workspaces available to this token.").add_subparsers(
        dest="workspace_command", required=True).add_parser("list", help="List workspaces.")

    workflow = commands.add_parser("workflow", help="Manage workflow definitions.").add_subparsers(
        dest="workflow_command", required=True)
    workflow.add_parser("list", help="List workflow definitions.")
    show_workflow = workflow.add_parser("show", help="Show a workflow definition.")
    show_workflow.add_argument("workflow_id")
    create_workflow = workflow.add_parser("create", help="Create a workflow from a JSON file.")
    create_workflow.add_argument("file", type=Path)
    create_workflow.add_argument("--idempotency-key")
    run_workflow = workflow.add_parser("run", help="Start the current version of a workflow.")
    run_workflow.add_argument("workflow_id")
    run_workflow.add_argument("--idempotency-key")

    run = commands.add_parser("runs", help="Inspect and cancel workflow runs.").add_subparsers(
        dest="run_command", required=True)
    run.add_parser("list", help="List workflow runs.")
    show_run = run.add_parser("show", help="Show a workflow run.")
    show_run.add_argument("run_id")
    cancel_run = run.add_parser("cancel", help="Cancel a non-terminal workflow run.")
    cancel_run.add_argument("run_id")
    return parser


def _display(value: Any) -> None:
    if is_dataclass(value):
        value = asdict(cast(Any, value))
        if value.get("details"):
            value = value["details"]
    elif isinstance(value, list):
        value = [asdict(cast(Any, item)) if is_dataclass(item) else item for item in value]
    print(json.dumps(value, indent=2, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    token = os.environ.get("RELAYCORE_API_TOKEN", "")
    if not token:
        print("Set RELAYCORE_API_TOKEN to a workspace API token created in the RelayCore dashboard.", file=sys.stderr)
        return 2
    if not args.url or not args.workspace:
        print("Set RELAYCORE_URL and RELAYCORE_WORKSPACE_ID, or pass --url and --workspace.", file=sys.stderr)
        return 2
    try:
        client = RelayCoreClient(args.url, token, args.workspace, timeout=args.timeout)
        result: Any
        if args.area == "workspaces":
            result = client.list_workspaces()
        elif args.area == "workflow":
            if args.workflow_command == "list":
                result = client.list_workflow_definitions()
            elif args.workflow_command == "show":
                result = client.get_workflow_definition(args.workflow_id)
            elif args.workflow_command == "run":
                result = client.start_workflow(args.workflow_id, idempotency_key=args.idempotency_key)
            else:
                payload = json.loads(args.file.read_text(encoding="utf-8"))
                if (not isinstance(payload, dict) or set(payload) - {"title", "steps", "trigger"}
                        or not isinstance(payload.get("title"), str) or not isinstance(payload.get("steps"), list)):
                    print("Workflow JSON must contain only title, steps, and optional trigger.", file=sys.stderr)
                    return 2
                result = client.create_workflow_definition(
                    payload["title"], payload["steps"], trigger=payload.get("trigger"),
                    idempotency_key=args.idempotency_key,
                )
        elif args.run_command == "list":
            result = client.list_runs()
        elif args.run_command == "show":
            result = client.get_run(args.run_id)
        else:
            result = client.cancel_run(args.run_id)
        _display(result)
        return 0
    except (RelayCoreAPIError, RelayCoreConnectionError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
