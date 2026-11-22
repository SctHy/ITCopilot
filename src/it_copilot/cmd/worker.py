import asyncio
from pathlib import Path

from dotenv import load_dotenv

from it_copilot.workflow.worker import run_worker


def main():
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
