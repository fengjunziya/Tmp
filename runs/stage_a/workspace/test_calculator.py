import json, subprocess, sys
from calculator import add
assert add(2, 3) == 5, "add(2,3) must equal 5"
assert add(-2, 2) == 0
result = subprocess.run([sys.executable,"sum_cli.py","--values","1","2","3"],capture_output=True,text=True)
assert result.returncode == 0, result.stderr
assert json.loads(result.stdout) == {"sum": 6}
print("AC01_AC02_OK")
