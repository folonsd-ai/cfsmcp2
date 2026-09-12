"""Generate cfs-mark.ico for Windows portable (Pillow only)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.portable.tray_icon import draw_mark  # noqa: E402


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    ico_path = out_dir / "cfs-mark.ico"
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    images = [draw_mark(s) for s, _ in sizes]
    images[0].save(ico_path, format="ICO", sizes=sizes)
    print(f"Wrote {ico_path}")


if __name__ == "__main__":
    main()
