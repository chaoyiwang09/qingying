"""把 Homebrew 里的 ffmpeg 和它依赖的库收进一个目录，方便打进 Mac 应用。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def linked(path: Path) -> list[str]:
    out = subprocess.check_output(["otool", "-L", str(path)], text=True)
    libs = []
    for line in out.splitlines()[1:]:
        item = line.strip().split(" (", 1)[0]
        if item.startswith(("/opt/homebrew/", "/usr/local/")):
            libs.append(item)
    return libs


def main() -> None:
    prefix = subprocess.check_output(["brew", "--prefix", "ffmpeg"], text=True).strip()
    dest = Path("dist/ffmpeg-bin")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    queue = [Path(prefix) / "bin" / "ffmpeg", Path(prefix) / "bin" / "ffprobe"]
    seen: set[str] = set()
    while queue:
        current = queue.pop()
        if current.name in seen or not current.is_file():
            continue
        seen.add(current.name)
        target = dest / current.name
        shutil.copy2(current, target)
        os.chmod(target, 0o755)
        queue.extend(Path(item) for item in linked(target))
    for path in dest.iterdir():
        if not path.is_file():
            continue
        subprocess.run(["install_name_tool", "-id", f"@loader_path/{path.name}", str(path)], check=False)
        for origin in linked(path):
            name = Path(origin).name
            if (dest / name).is_file():
                subprocess.run(
                    ["install_name_tool", "-change", origin, f"@loader_path/{name}", str(path)],
                    check=False,
                )
    print(f"bundled {len(seen)} files into {dest}")


if __name__ == "__main__":
    main()
