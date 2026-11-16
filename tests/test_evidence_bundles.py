"""Historical evidence remains verifiable without allowing unsafe restoration."""
import hashlib
import io
import tarfile

import pytest

from scripts.evidence import extract, verify


def bundle(root, member="evaluation/reports/example.json", *, symlink=False):
    content = b'{"passed": false}\n'
    path = root / "sample.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(member)
        if symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp/outside"
            archive.addfile(info)
        else:
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return {"packages": {"sample": {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}},
            "entries": [{"action": "package", "package": "sample", "member": member,
                         "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}


def test_restore_preserves_failure_evidence_and_duplicate_alias(tmp_path):
    index = bundle(tmp_path)
    entry = index["entries"][0]
    index["entries"].append({**entry, "action": "deduplicate", "path": "evaluation/reports/older/prepared.jsonl"})
    out = extract(tmp_path, index, "sample", tmp_path / "restored")
    assert (out / entry["member"]).read_bytes() == (out / index["entries"][1]["path"]).read_bytes()
    with pytest.raises(ValueError, match="new directory"):
        extract(tmp_path, index, "sample", out)


def test_changed_bundle_is_rejected_before_writing(tmp_path):
    index = bundle(tmp_path)
    with (tmp_path / "sample.tar.gz").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="archive checksum"):
        extract(tmp_path, index, "sample", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("member,symlink", [("evaluation/reports/../../outside", False),
                                           ("evaluation/reports/link", True)])
def test_archive_paths_and_links_cannot_escape(tmp_path, member, symlink):
    index = bundle(tmp_path, member, symlink=symlink)
    with pytest.raises(ValueError, match="Unsafe|regular files"):
        verify(tmp_path, index, "sample")


def test_package_checksum_cannot_hide_wrong_member_content(tmp_path):
    index = bundle(tmp_path)
    index["entries"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="member checksum"):
        verify(tmp_path, index, "sample")
