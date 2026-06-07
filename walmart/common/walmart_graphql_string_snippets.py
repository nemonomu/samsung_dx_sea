from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--needle", default="CartShopSimilar")
    parser.add_argument("--radius", type=int, default=700)
    args = parser.parse_args()

    needle_cmp = args.needle.lower()
    files = sorted(args.root.rglob("*.js"))
    print(f"root={args.root} exists={args.root.exists()} js_files={len(files)} needle={args.needle}")
    total_hits = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        text_cmp = text.lower()
        pos = 0
        hit = 0
        while True:
            idx = text_cmp.find(needle_cmp, pos)
            if idx < 0:
                break
            hit += 1
            start = max(0, idx - args.radius)
            end = min(len(text), idx + len(args.needle) + args.radius)
            snippet = " ".join(text[start:end].split())
            print(f"\n--- {path.name} hit={hit} offset={idx} ---")
            print(snippet)
            total_hits += 1
            pos = idx + len(args.needle)
    print(f"total_hits={total_hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
