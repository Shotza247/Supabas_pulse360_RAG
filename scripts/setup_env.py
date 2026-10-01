"""Create current local settings without printing secrets or removing the original."""

import argparse
import getpass
import shutil
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]


def sync_env(root):
    target, example = root / ".env", root / ".env.example"
    defaults = dotenv_values(example)
    old = dotenv_values(target) if target.exists() else {}
    if target.exists():
        archive = root / ".local" / "archive" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archive.mkdir(parents=True, exist_ok=False)
        shutil.copy2(target, archive / "legacy.env")
    shutil.copyfile(example, target)
    for key, value in old.items():
        if key in defaults and value is not None:
            set_key(target, key, value)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sync", action="store_true", help="Merge current settings; archive old .env"
    )
    parser.add_argument("--token", action="store_true", help="Enter HF token using a hidden prompt")
    args = parser.parse_args()
    target = sync_env(ROOT) if args.sync else ROOT / ".env"
    if args.token:
        if not target.exists():
            parser.error("Run --sync first")
        token = getpass.getpass("Hugging Face token (hidden): ").strip()
        if not token.startswith("hf_") or len(token) < 10:
            parser.error("Expected a Hugging Face token")
        set_key(target, "HF_TOKEN", token)
    values = dotenv_values(target)
    for key in ("HF_TOKEN", "EMBEDDING_URL", "LLM_MODEL"):
        print(f"{key}: {'set' if values.get(key) else 'missing'}")
    print("Settings values and credentials are not printed.")
    print("Restart the API after changing .env; configuration is cached.")


if __name__ == "__main__":
    main()
