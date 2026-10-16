"""Tests for jarvis.node.sync: manifests, what to copy, and archives that can't escape their folder."""

import io
import os
import tarfile
from pathlib import Path

from jarvis.node import sync


def tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_a_manifest_leaves_out_secrets_git_and_caches_that_cant_move(tmp_path):
    folder = tree(tmp_path / "p", {"app.py": "x", "src/a.js": "y", ".env": "KEY=1", ".env.local": "K=2",
                                   ".env.example": "KEY=", "config/credentials.json": "{}", "secrets/token": "t",
                                   "keys/server.pem": "k", ".git/config": "[remote]", ".venv/bin/python": "py",
                                   "__pycache__/a.pyc": "c", "node_modules/lib/index.js": "m", ".DS_Store": ""})
    found = sync.manifest(folder)
    assert sorted(found) == [".env.example", "app.py", "node_modules/lib/index.js", "src/a.js"]  # a template isn't secret
    assert found["app.py"][0] == 1 and len(found["app.py"][1]) == 64


def test_links_inside_the_folder_are_kept_as_links_and_others_left_out(tmp_path):
    folder = tree(tmp_path / "p", {"node_modules/pkg/cli.js": "x"})
    (folder / "node_modules/.bin").mkdir()
    os.symlink("../pkg/cli.js", folder / "node_modules/.bin/pkg")
    os.symlink("/etc/hosts", folder / "outside")
    os.symlink("../../elsewhere", folder / "node_modules/up")
    os.symlink("node_modules", folder / "mods")
    found = sync.manifest(folder)
    assert found["node_modules/.bin/pkg"] == [sync.LINK, "../pkg/cli.js"] and found["mods"] == [sync.LINK, "node_modules"]
    assert "outside" not in found and "node_modules/up" not in found


def test_hashes_are_reused_until_a_file_changes(tmp_path, monkeypatch):
    folder = tree(tmp_path / "p", {"a.txt": "one", "b.txt": "two"})
    cache = tmp_path / "hashes.json"
    first = sync.manifest(folder, cache)
    read = []
    real = sync._hash
    monkeypatch.setattr(sync, "_hash", lambda path: (read.append(path.name), real(path))[1])
    (folder / "b.txt").write_text("changed")
    os.utime(folder / "b.txt", ns=(1, 1))
    again = sync.manifest(folder, cache)
    assert read == ["b.txt"] and again["a.txt"] == first["a.txt"] and again["b.txt"] != first["b.txt"]


def test_only_what_differs_is_asked_for(tmp_path):
    main = tree(tmp_path / "main", {"same.txt": "s", "changed.txt": "new", "added.txt": "a"})
    copy = tree(tmp_path / "copy", {"same.txt": "s", "changed.txt": "old", "gone.txt": "g"})
    wanted, have = sync.manifest(main), sync.manifest(copy)
    assert sync.differing(wanted, have) == ["added.txt", "changed.txt"]
    assert sorted(set(have) - set(wanted)) == ["gone.txt"]


def test_files_travel_with_their_executable_bit_and_links(tmp_path):
    main = tree(tmp_path / "main", {"run.sh": "#!/bin/sh\n", "src/a.py": "a"})
    os.chmod(main / "run.sh", 0o755)
    os.symlink("src/a.py", main / "alias.py")
    archive = sync.pack(main, ["run.sh", "src/a.py", "alias.py", ".env", "missing.txt"], tmp_path / "x.tar")
    copy = tmp_path / "copy"
    copy.mkdir()
    assert sorted(sync.unpack(archive, copy)) == ["alias.py", "run.sh", "src/a.py"]
    assert os.access(copy / "run.sh", os.X_OK) and os.readlink(copy / "alias.py") == "src/a.py"
    assert sync.manifest(copy) == sync.manifest(main)


def _hostile(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
    with tarfile.open(path, "w") as tar:
        for info, data in members:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if info.isfile() else None)
    return path


def test_an_archive_cant_write_outside_its_folder_or_plant_secrets(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", copy / "escape")

    def member(name: str, kind=tarfile.REGTYPE, link: str = "") -> tuple[tarfile.TarInfo, bytes]:
        info = tarfile.TarInfo(name)
        info.type, info.linkname = kind, link
        return info, b"x"

    archive = _hostile(tmp_path / "bad.tar", [
        member("../evil.txt"), member("/abs.txt"), member("escape/through-link.txt"), member(".env"),
        member("sub/id_ed25519"), member("ok.txt"), member("dev", tarfile.CHRTYPE), member("hard", tarfile.LNKTYPE, "ok.txt"),
        member("link-out", tarfile.SYMTYPE, "../../outside"), member("link-in", tarfile.SYMTYPE, "ok.txt")])
    assert sorted(sync.unpack(archive, copy)) == ["link-in", "ok.txt"]
    assert not list((tmp_path / "outside").iterdir()) and not (tmp_path / "evil.txt").exists()


def test_removing_never_touches_folders_or_anything_outside(tmp_path):
    copy = tree(tmp_path / "copy", {"a/b/old.txt": "x", "keep/dir/file.txt": "k"})
    (tmp_path / "outside.txt").write_text("o")
    assert sync.remove(copy, ["a/b/old.txt", "../outside.txt", "keep/dir"]) == ["a/b/old.txt"]
    assert not (copy / "a").exists() and (copy / "keep/dir/file.txt").exists() and (tmp_path / "outside.txt").exists()
