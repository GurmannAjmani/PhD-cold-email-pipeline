from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from groq import Groq

ROOT = Path(__file__).resolve().parent
GROQ_MODEL = "llama-3.3-70b-versatile"

DATASET_CSV = ROOT / "Indian_Engineering_Colleges_Dataset.csv"
PROFILE_TXT = ROOT / "samples" / "profile.txt"
RESEARCH_TXT = ROOT / "samples" / "research_interests.txt"
OUTPUT_JSON = ROOT / "output" / "student_output.json"

REACH: dict[str, list[str]] = {
    "T1": ["T1"],
    "T2": ["T2", "T1"],
    "T3": ["T3", "T2"],
    "T4": ["T4", "T3"],
    "unknown": ["T4", "T3"],
}

NAME_COL = "College_Name"
TIER_COL = "tier"
STATE_COL = "State"
RATING_COL = "Rating"
TOP_N = 100
SAMPLE_CAP = 40


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _strip_json_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _normalize_tier(raw: str | float | None) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip().upper()
    if s in {"T1", "T2", "T3", "T4"}:
        return s
    return None


def _groq_json(prompt: str) -> dict:
    load_dotenv(ROOT / ".env")
    key = os.getenv("GROQ_API_KEY")
    if not key:
        _die("Error: set GROQ_API_KEY in .env")

    client = Groq(api_key=key)
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": prompt + "\n\nRespond with a single JSON object only, no markdown.",
                }
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        _die(f"Error: Groq API request failed: {e}")

    text = (resp.choices[0].message.content or "").strip()
    if not text:
        _die("Error: empty response from Groq (check API key, quota, and model name).")

    try:
        return json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as e:
        _die(f"Error: Groq did not return valid JSON: {e}\n---\n{text[:2000]}\n---")


def main() -> None:
    if not DATASET_CSV.is_file():
        _die(f"Error: missing dataset CSV: {DATASET_CSV}")
    if not PROFILE_TXT.is_file():
        _die(f"Error: missing profile: {PROFILE_TXT}")
    if not RESEARCH_TXT.is_file():
        _die(f"Error: missing research interests: {RESEARCH_TXT}")

    df = pd.read_csv(DATASET_CSV)
    for col in (NAME_COL, TIER_COL, RATING_COL):
        if col not in df.columns:
            _die(f"Error: CSV must include column {col!r}.")

    df["_tier_norm"] = df[TIER_COL].map(_normalize_tier)
    if df["_tier_norm"].isna().any():
        _die("Error: dataset has rows with missing or invalid tier (expected T1–T4).")

    df["_rating"] = pd.to_numeric(df[RATING_COL], errors="coerce")
    top = df.dropna(subset=["_rating"]).sort_values("_rating", ascending=False).head(TOP_N)

    college_list: list[dict[str, str | float]] = []
    for _, row in top.iterrows():
        college_list.append(
            {
                "name": str(row[NAME_COL]).strip(),
                "state": str(row[STATE_COL]).strip() if STATE_COL in row.index else "",
                "tier": row["_tier_norm"],
                "rating": float(row["_rating"]),
            }
        )
    valid_names = {c["name"] for c in college_list}
    colleges_json = json.dumps(college_list, ensure_ascii=False, indent=2)

    profile = PROFILE_TXT.read_text(encoding="utf-8")
    research = RESEARCH_TXT.read_text(encoding="utf-8")

    prompt = f"""You structure PhD application inputs. Use only the CV and research text below; do not invent degrees, employers, or papers.

You are also given the top {TOP_N} Indian engineering colleges by rating from our dataset. Pick the student's home college from that list only.

Return a single JSON object with exactly these keys:
- student_background: object with "narrative" (string) and "bullets" (array of strings, facts from the CV text).
- research_interests: object with "paragraph" (string) and "keywords" (array of 5-15 strings).
- department_focus: string or null (engineering discipline if clearly stated, else null).
- lab_focus: string or null (named lab themes if stated, else null).
- home_institution_matched: string or null — MUST be exactly one "name" from the college list below, or null if none fit.
- home_tier: string — one of "T1", "T2", "T3", "T4", or "unknown". The tier of the student's home institution for PhD reach policy. IITs and IISc are T1; use the matched row's tier when matched, else infer from the CV.

Rules:
1. home_institution_matched must copy a list "name" exactly (e.g. CV says IIT Bombay → pick "IIT Bombay" if present).
2. Do not use location/state to choose the college; match on institution identity only.
3. home_tier drives how ambitious the student's target pool should be.

College list (top {TOP_N} by rating):
{colleges_json}

CV text:
---
{profile}
---

Research interests text:
---
{research}
---
"""

    data = _groq_json(prompt)
    required = [
        "student_background",
        "research_interests",
        "department_focus",
        "lab_focus",
        "home_institution_matched",
        "home_tier",
    ]
    for k in required:
        if k not in data:
            _die(f"Error: JSON missing key {k!r}.")

    matched_raw = data.get("home_institution_matched")
    matched_name: str | None = None
    if isinstance(matched_raw, str):
        m = matched_raw.strip()
        if m in valid_names:
            matched_name = m

    raw_tier = str(data.get("home_tier") or "unknown").strip().upper()
    if raw_tier not in {"T1", "T2", "T3", "T4", "UNKNOWN"}:
        _die(f"Error: invalid home_tier {raw_tier!r}; expected T1–T4 or unknown.")
    home_key = "unknown" if raw_tier == "UNKNOWN" else raw_tier

    possible_tier = home_key if home_key != "unknown" else "unknown"
    target_tiers = REACH.get(home_key) or REACH["unknown"]

    allowed = set(target_tiers)
    pool = top[top["_tier_norm"].isin(allowed)].head(SAMPLE_CAP)

    tier_college_sample = []
    for _, row in pool.iterrows():
        tier_college_sample.append(
            {
                "name": str(row[NAME_COL]).strip(),
                "state": str(row[STATE_COL]).strip() if STATE_COL in row.index else "",
                "tier": row["_tier_norm"],
            }
        )

    matched_tier: str | None = None
    if matched_name:
        hit = top[top[NAME_COL].astype(str).str.strip() == matched_name]
        if not hit.empty:
            matched_tier = hit.iloc[0]["_tier_norm"]

    out = {
        "student_background": data["student_background"],
        "research_interests": data["research_interests"],
        "department_focus": data["department_focus"],
        "lab_focus": data["lab_focus"],
        "possible_tier_of_colleges": possible_tier,
        "target_institute_tiers": target_tiers,
        "tier_college_sample": tier_college_sample,
        "meta": {
            "phase": 1,
            "groq_model": GROQ_MODEL,
            "dataset_csv": str(DATASET_CSV.name),
            "home_institution_matched": matched_name,
            "home_tier": home_key,
            "matched_tier_from_csv": matched_tier,
        },
    }

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
