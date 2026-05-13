from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
MAX_SERP_CALLS = 20
MAX_PAPERS = 5

PHASE3_JSON = ROOT / "output" / "phase3_candidates.json"
OUTPUT_JSON = ROOT / "output" / "phase4_candidates_enriched.json"
SERP_ENDPOINT = "https://serpapi.com/search.json"


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _year(s: str) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", s)
    return int(m.group(0)) if m else None


def _serp(api_key: str, q: str) -> dict[str, Any]:
    url = f"{SERP_ENDPOINT}?{urlencode({'engine': 'google_scholar', 'q': q, 'hl': 'en', 'api_key': api_key})}"
    with urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _papers(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in (data.get("organic_results") or [])[:MAX_PAPERS]:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        pub = r.get("publication_info") if isinstance(r.get("publication_info"), dict) else {}
        summary = (pub.get("summary") or "").strip()
        year = r.get("year")
        if isinstance(year, str) and year.isdigit():
            yi = int(year)
        elif isinstance(year, int):
            yi = year
        else:
            yi = _year(summary) or _year(snippet) or _year(title)
        out.append({
            "title": title or None,
            "link": (r.get("link") or "").strip() or None,
            "year": yi,
            "snippet": snippet[:800] if snippet else None,
        })
    return out


def main() -> None:
    print("Phase 4 started")
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")
    if not api_key:
        _die("Error: set SERPAPI_API_KEY in .env")

    phase3 = json.loads(PHASE3_JSON.read_text(encoding="utf-8"))
    pool = (phase3.get("candidates") or [])[:MAX_SERP_CALLS]

    enriched = []
    for idx, c in enumerate(pool, start=1):
        name = (c.get("name") or "").strip()
        institute = (c.get("institute") or "").strip()
        print(f"  [{idx}/{len(pool)}] {name!r}")
        data = _serp(api_key, f"{name} {institute}")
        papers = _papers(data)
        years = [p["year"] for p in papers if isinstance(p.get("year"), int)]
        enriched.append({
            **c,
            "papers": papers,
            "last_publication_year": max(years) if years else None,
            "email": (c.get("email") or "").strip() or None,
        })
        if idx < len(pool):
            time.sleep(0.35)

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(
        json.dumps({"candidates_enriched": enriched}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_JSON} ({len(enriched)} rows)")


if __name__ == "__main__":
    main()
