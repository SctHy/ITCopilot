"""下载并校验与 Magpie 相同版本的 AstrLink Guard；不发送用户文本。"""

from servicedesk_agent.agent.masking.local_pii import REVISION, install

if __name__ == "__main__":
    print(f"AstrLink Guard {REVISION}: {install()}")
