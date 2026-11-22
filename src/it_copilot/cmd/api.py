import os
import time
from pathlib import Path

import uvicorn
from it_copilot.infra.env import env_float
from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    from it_copilot.api.app import create_app

    app = create_app()
    server = DrainingServer(uvicorn.Config(app, log_config=None, host="0.0.0.0", port=int(os.getenv("IT_COPILOT_PORT", "8001")),
        timeout_graceful_shutdown=env_float("IT_COPILOT_SHUTDOWN_GRACE_S", 15)))
    server.run()


class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig, frame):
        state = self.config.app.state.container
        if not state.draining:
            state.draining = True
            state.shutdown_deadline = time.monotonic() + env_float("IT_COPILOT_SHUTDOWN_GRACE_S", 15)
        super().handle_exit(sig, frame)


if __name__ == "__main__":
    main()
