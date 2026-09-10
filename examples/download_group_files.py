"""Mirror the newest files of every group into ./downloads/.

Shows how the file module is meant to be used from a script — walk the group
tree, pick what changed recently, download it with the original folder layout.
"""

from __future__ import annotations

import os

from iserv_scraper import IServClient


def main(days: int = 14, target_dir: str = "downloads") -> None:
    with IServClient.from_env() as iserv:
        for group in iserv.groups():
            print(f"\n== {group.name} ==")
            for entry in iserv.files.recent(days=days, path=group.files_path or "", max_depth=4):
                destination = os.path.join(target_dir, entry.path)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                iserv.files.download_to(entry.path, destination)
                print(f"  {entry.name} -> {destination}")


if __name__ == "__main__":
    main()
