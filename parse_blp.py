import re

with open('/var/home/rizvan/PopcornBox/src/window.blp', 'r') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if line.strip().startswith('child: ') and not line.strip().endswith('{'):
        if not line.strip().endswith(';'):
            print(f"Line {i+1}: Missing semicolon on child property? {line.strip()}")
