import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    uvicorn.run(
        "it_copilot.api.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.getenv("IT_COPILOT_PORT", "8001")),
    )


if __name__ == "__main__":
    main()
