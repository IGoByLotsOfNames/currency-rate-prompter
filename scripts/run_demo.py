from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from currency_prompter.cli import main

raise SystemExit(main(["demo", *sys.argv[1:]]))
