"""Run the coloom server: `uv run python -m coloom.server [--db ...] [--config ...]`."""

import argparse
import logging
from pathlib import Path

import uvicorn

from coloom.config import ColoomConfig, load_config
from coloom.server.app import create_app
from coloom.store import WeaveStore


def main() -> None:
    parser = argparse.ArgumentParser(description="coloom server")
    parser.add_argument("--db", type=Path, default=Path("coloom.sqlite"))
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML endpoints/presets config (default: ./coloom.yaml if present)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4444)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())
    config_path = args.config or (
        Path("coloom.yaml") if Path("coloom.yaml").exists() else None
    )
    config = load_config(config_path) if config_path else ColoomConfig()
    if not config.endpoints:
        logging.warning("no inference endpoints configured — /gen will return 400")
    store = WeaveStore(args.db)
    app = create_app(store, config)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
