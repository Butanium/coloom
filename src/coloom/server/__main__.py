"""Run the coloom server: `uv run python -m coloom.server [--db ...] [--config ...]`."""

import argparse
import logging
from pathlib import Path

import uvicorn

from coloom.config import ColoomConfig, load_config, load_env_file
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
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="dotenv file loaded into the environment at startup (existing env"
        " vars win); api_key_env endpoints resolve from it",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=Path("web/dist"),
        help="built SPA to serve at / (skipped with a warning if missing;"
        " pass an empty value to disable)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4444)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())
    if load_env_file(args.env_file):
        logging.info("loaded env file %s", args.env_file)
    else:
        logging.info("no env file at %s — relying on the inherited environment", args.env_file)
    config_path = args.config or (
        Path("coloom.yaml") if Path("coloom.yaml").exists() else None
    )
    config = load_config(config_path) if config_path else ColoomConfig()
    if not config.endpoints:
        logging.warning("no inference endpoints configured — /gen will return 400")
    store = WeaveStore(args.db)
    static_dir = args.static_dir if str(args.static_dir) else None
    app = create_app(store, config, static_dir=static_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
