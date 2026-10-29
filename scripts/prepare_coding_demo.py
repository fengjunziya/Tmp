"""Prepare a reproducible coding-repair exercise without overwriting files."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT / '.local' / 'coding_demo')
    parser.add_argument('--acceptance', type=Path, default=ROOT / 'reports' / 'coding_demo_acceptance.json')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    names = ['calculator.py', 'sum_cli.py', 'test_calculator.py']
    if any((workspace / name).exists() for name in names):
        raise ValueError('Exercise files already exist; choose a new --workspace to preserve them')
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / 'calculator.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    (workspace / 'test_calculator.py').write_text(
        'import json, subprocess, sys\nfrom calculator import add\n'
        'assert add(2, 3) == 5, "add(2,3) must equal 5"\nassert add(-2, 2) == 0\n'
        'r = subprocess.run([sys.executable, "sum_cli.py", "--values", "1", "2", "3"], capture_output=True, text=True)\n'
        'assert r.returncode == 0, r.stderr\nassert json.loads(r.stdout) == {"sum": 6}\n'
        'print("AC01_AC02_OK")\n', encoding='utf-8')
    args.acceptance.parent.mkdir(parents=True, exist_ok=True)
    args.acceptance.write_text(json.dumps({'commands': [[sys.executable, 'test_calculator.py']]}, indent=2), encoding='utf-8')
    print(f'Prepared {workspace}; acceptance {args.acceptance.resolve()}')


if __name__ == '__main__':
    main()
