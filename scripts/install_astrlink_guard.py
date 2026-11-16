"""下载并校验与 Magpie 相同版本的 AstrLink Guard；不发送用户文本。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.local_pii import REVISION, install

if __name__ == "__main__":
    print(f"AstrLink Guard {REVISION}: {install()}")
