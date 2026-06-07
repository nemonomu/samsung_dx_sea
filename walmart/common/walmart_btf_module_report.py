from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def extract_name(item: dict[str, Any]) -> str:
    for key in ("name", "productName", "title"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    product = item.get("product")
    if isinstance(product, dict):
        return extract_name(product)
    return ""


def extract_items(configs: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("products", "items"):
        value = configs.get(key)
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, dict)]
    p13n = configs.get("p13nDataV1")
    if isinstance(p13n, dict):
        for key in ("products", "items"):
            value = p13n.get(key)
            if isinstance(value, list) and value:
                return [item for item in value if isinstance(item, dict)]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Report ItemCarousel modules from Walmart ItemByIdBtf response.")
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    data = json.loads(args.json.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for obj in walk(data):
        if obj.get("type") != "ItemCarousel":
            continue
        configs = obj.get("configs") if isinstance(obj.get("configs"), dict) else {}
        items = extract_items(configs)
        names = [name for item in items if (name := extract_name(item))]
        rows.append(
            {
                "moduleId": obj.get("moduleId") or obj.get("module_id") or "",
                "name": obj.get("name") or "",
                "title": configs.get("title") or "",
                "zone": (obj.get("matchedTrigger") or {}).get("zone") if isinstance(obj.get("matchedTrigger"), dict) else "",
                "item_count": len(items),
                "names_joined": " ||| ".join(names),
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as fh:
        fields = ["moduleId", "name", "title", "zone", "item_count", "names_joined"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"rows": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
