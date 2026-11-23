# Contributing to Ashie

Thanks for helping! Ashie is a hobby-scale, local-first assistant, so small focused pull requests are easiest to review.

## Ground rules
- **Nothing personal in the repo.** No names, keys, phone numbers or voice profiles. Personal data lives in `~/Ashie` (see `.gitignore`).
- **Local-first.** Prefer offline solutions; anything that calls the internet must degrade gracefully.
- **Windows is the target.** Guard Windows-only imports (`winsound`, `pywin32`, `pycaw`) with `try/except`.
- **Dashboard stays dependency-free** (standard library + one HTML file, no CDN, no build step).

## Dev setup
```bash
git clone https://github.com/bluetreesandredskies/ashie-windows-ai.git && cd ashie-windows-ai
powershell -ExecutionPolicy Bypass -File install.ps1
.venv\Scripts\activate
pip install pytest
python dashboard.py --demo        # work on the UI with sample data
python -m pytest tests            # run the tests
```

## Adding a skill
1. Write `skill_yourthing(...)` in `ashie.py` (use `speak()` for replies, `store.log()` for the activity feed).
2. Register it in the `SKILLS` dict and describe it, with one example, in `ROUTER_SYS`.
3. Optionally add an instant, no-AI phrase in `rules()`.
4. Add a line to the README feature table.

## Pull requests
Describe what changed and how you tested it (a screenshot for UI changes). Keep commits small.
