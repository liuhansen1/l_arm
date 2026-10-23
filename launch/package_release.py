"""从已提交的版本标签构建源码发布包、文件清单和 SHA-256 校验文件。"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = {
    "README.md", "RELEASE_NOTES.md", "LICENSE", "requirements-cpu.txt",
    "launch/web.py", "launch/check.py", "backend/simulation.py", "backend/store.py",
    "frontend/index.html", "frontend/app.js", "frontend/frame_stream.js",
    "llm/model.py", "llm/checkpoints/intent_v2.pt",
    "robot/demo.py", "robot/grasp.py", "robot/skills.py",
    "robot/models/franka_panda/panda.urdf", "robot/models/franka_panda/LICENSE.txt",
}


def git(*arguments):
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, check=True)
    return result.stdout


def private_path(name):
    parts = PurePosixPath(name).parts
    excluded = {".git", ".publish", ".aws", ".codex", ".agents", "dist",
                ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    return (name.startswith("backend/data/")
            or any(part in excluded or part.startswith(".venv_")
                   or (part.startswith(".env") and part != ".env.example") for part in parts)
            or ".sqlite3" in PurePosixPath(name).name
            or PurePosixPath(name).name in {"id_rsa", "id_ed25519"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="已存在的版本标签，例如 v1.0.0")
    parser.add_argument("--output", type=Path, help="输出目录，默认 dist/<tag>")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        parser.error("版本标签必须采用 v主版本.次版本.修订号，例如 v1.0.0")
    if git("status", "--porcelain").strip():
        raise RuntimeError("工程有未提交修改，请先提交再构建发布包。")
    commit = git("rev-parse", "--verify", f"refs/tags/{args.tag}^{{commit}}").decode().strip()
    records = []
    for entry in git("ls-tree", "-r", "-z", commit).split(b"\0"):
        if not entry:
            continue
        attributes, raw_name = entry.split(b"\t", 1)
        mode, kind, sha = attributes.decode().split()
        name = raw_name.decode("utf-8")
        if private_path(name):
            raise RuntimeError(f"发布版本包含本地环境或用户数据：{name}")
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise RuntimeError(f"发布包需要普通文件，不支持子模块或符号链接：{name}")
        data = git("cat-file", "blob", sha)
        records.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    missing = REQUIRED_FILES - {record["path"] for record in records}
    if missing:
        raise RuntimeError(f"发布版本缺少运行资源：{', '.join(sorted(missing))}")

    output = (args.output or ROOT / "dist" / args.tag).resolve()
    output.mkdir(parents=True, exist_ok=True)
    prefix = f"l_arm-{args.tag}"
    artifacts = []
    for extension in ("zip", "tar.gz"):
        target = output / f"{prefix}.{extension}"
        git("archive", f"--format={extension}", f"--prefix={prefix}/", f"--output={target}", commit)
        artifacts.append(target)
    manifest = output / f"{prefix}-manifest.json"
    manifest.write_text(json.dumps({"version": args.tag, "commit": commit, "files": records},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    artifacts.append(manifest)
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                                 for path in artifacts), encoding="utf-8")
    print(json.dumps({"version": args.tag, "commit": commit, "files": len(records),
                      "artifacts": [{"path": str(path), "size": path.stat().st_size}
                                    for path in [*artifacts, checksums]]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        print(error.stderr.decode(errors="replace").strip(), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"发布包构建失败：{error}", file=sys.stderr)
        raise SystemExit(1)
