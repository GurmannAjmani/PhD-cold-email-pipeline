from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLI_OUTPUT = ROOT / "cli_output.txt"
FEEDBACK_JSON = ROOT / "output" / "feedback_store.json"
REGENERATE_SCRIPTS = (
    "find_supervisors.py",
    "enrich_candidates.py",
    "draft_emails.py",
)


def _norm_name(name: str) -> str:
    s = re.sub(
        r"^(?:(?:prof\.?|dr\.?|mr\.?|mrs\.?|ms\.?)\s*)+",
        "",
        name.strip(),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", s).strip().lower()


def _load() -> dict:
    if not FEEDBACK_JSON.is_file():
        return {"global_drafting": [], "professors": {}}
    data = json.loads(FEEDBACK_JSON.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"global_drafting": [], "professors": {}}
    data.setdefault("global_drafting", [])
    data.setdefault("professors", {})
    return data


def _save(store: dict) -> None:
    FEEDBACK_JSON.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK_JSON.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _find_key(store: dict, name: str) -> str | None:
    target = _norm_name(name)
    for key in store.get("professors") or {}:
        if _norm_name(key) == target:
            return key
    return None


def _log_input(answer: str) -> None:
    with open(CLI_OUTPUT, "a", encoding="utf-8") as f:
        f.write(f"> {answer}\n")


def _prompt(label: str) -> str:
    print(label, flush=True)
    try:
        answer = input("> ").strip()
        _log_input(answer)
        return answer
    except (EOFError, KeyboardInterrupt):
        print("\nStopped.", flush=True)
        sys.exit(0)


def _yes_no(label: str) -> bool:
    while True:
        ans = _prompt(label).lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            return False
        print("Please enter yes or no.", flush=True)


def _run_script(script: str) -> None:
    result = subprocess.run([sys.executable, str(ROOT / script)], cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> None:
    print("Collect feedback", flush=True)
    store = _load()

    general = _prompt(
        "General feedback on mail drafting? (Enter to skip)\n"
        "e.g. make emails longer, more formal, mention federated learning more"
    )
    if general:
        store["global_drafting"].append({"text": general, "at": date.today().isoformat()})
        print("  Saved global drafting note.", flush=True)

    while True:
        prof = _prompt("\nProfessor name? (Enter when done)")
        if not prof:
            break
        note = _prompt(f"Feedback for {prof!r}?")
        if not note:
            print("  (skipped — empty feedback)", flush=True)
            continue

        key = _find_key(store, prof) or prof.strip()
        store["professors"].setdefault(key, [])
        store["professors"][key].append({"text": note, "at": date.today().isoformat()})
        print(f"  Saved under {key!r}.", flush=True)

    _save(store)
    print(f"Wrote {FEEDBACK_JSON}", flush=True)

    if _yes_no(
        "Regenerate professor list and mail drafts using this feedback? (yes/no)"
    ):
        for script in REGENERATE_SCRIPTS:
            print(f"\n=== {script} ===\n", flush=True)
            _run_script(script)
        print("Regeneration finished.", flush=True)
    else:
        print("Feedback saved. Run find_supervisors.py → draft_emails.py later to apply.", flush=True)


if __name__ == "__main__":
    main()
