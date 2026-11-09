"""CLI entrypoint for driftwatch server and manual check commands."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
import uvicorn

from driftwatch.app import create_app
from driftwatch.runner import DriftRunner
from driftwatch.store import DriftStore

app = typer.Typer(
    name="driftwatch",
    help="Fact drift detector and debounce alert engine",
    no_args_is_help=True,
)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind host")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Bind port")] = 8000,
    db: Annotated[str, typer.Option(help="Database URL")] = "sqlite:///driftwatch.db",
    poll_interval: Annotated[float, typer.Option(help="Background poller interval in seconds (0 to disable)")] = 30.0,
) -> None:
    """Run driftwatch HTTP API and poller."""
    store = DriftStore(db_url=db)
    runner = DriftRunner(store=store)
    fastapi_app = create_app(store=store, runner=runner, poll_interval=poll_interval)
    uvicorn.run(fastapi_app, host=host, port=port)


@app.command()
def check(
    db: Annotated[str, typer.Option(help="Database URL")] = "sqlite:///driftwatch.db",
    target_id: Annotated[str | None, typer.Option("--target-id", help="Target ID to check")] = None,
    all: Annotated[bool, typer.Option("--all", help="Check all due targets")] = False,
) -> None:
    """Execute on-demand checks."""
    async def _run() -> None:
        store = DriftStore(db_url=db)
        runner = DriftRunner(store=store)
        if target_id:
            result = await runner.check_target(target_id)
            print(f"Target: {result.target_id} | Status: {result.status} | Value: {result.extracted_value} | Alert: {result.alert_type}")
        elif all:
            results = await runner.check_due_targets()
            print(f"Completed {len(results)} due target checks.")
            for r in results:
                print(f"- {r.target_id}: status={r.status}, value={r.extracted_value}, alert={r.alert_type}")
        else:
            print("Specify either --target-id <id> or --all")

    asyncio.run(_run())


@app.command("init-db")
def init_db(
    db: Annotated[str, typer.Option(help="Database URL")] = "sqlite:///driftwatch.db",
) -> None:
    """Initialize database tables."""
    store = DriftStore(db_url=db)
    store.create_tables()
    print(f"Database schema initialized successfully for {db}")


def main(argv: list[str] | None = None) -> None:
    """CLI application entrypoint."""
    if argv is not None:
        app(args=argv, standalone_mode=False)
    else:
        app()


if __name__ == "__main__":
    main()
