"""Assemble a self-contained OrcaSlicer 2.5.0+ plugin folder in dist/."""

from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SRC = REPO_ROOT / "orca_plugin"
DIST_DIR = REPO_ROOT / "dist" / "flashforge_adv4_bridge"


def build_plugin(output_dir: Path = DIST_DIR) -> Path:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    (output_dir / "templates").mkdir(parents=True, exist_ok=True)
    (output_dir / "lib").mkdir(parents=True, exist_ok=True)

    files_to_copy = [
        (PLUGIN_SRC / "plugin.py", output_dir / "plugin.py"),
        (PLUGIN_SRC / "orca-plugin.json", output_dir / "orca-plugin.json"),
        (PLUGIN_SRC / "server.py", output_dir / "lib" / "server.py"),
        (REPO_ROOT / "bridge_controller.py", output_dir / "lib" / "bridge_controller.py"),
        (REPO_ROOT / "bridge_services.py", output_dir / "lib" / "bridge_services.py"),
        (REPO_ROOT / "flashforge_client.py", output_dir / "lib" / "flashforge_client.py"),
        (REPO_ROOT / "templates" / "index.html", output_dir / "templates" / "index.html"),
        (REPO_ROOT / "config-example.json", output_dir / "config-example.json"),
    ]

    local_cfg = REPO_ROOT / "config.json"
    if local_cfg.exists():
        files_to_copy.append((local_cfg, output_dir / "config.json"))

    for src, dst in files_to_copy:
        shutil.copy2(src, dst)

    return output_dir


if __name__ == "__main__":
    out = build_plugin()
    print(f"Built OrcaSlicer plugin at: {out}")
    print(
        "Copy this folder to Windows:\n"
        "  %APPDATA%\\OrcaSlicer\\orca_plugins\\flashforge_adv4_bridge\\"
    )
