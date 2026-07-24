import sys
from pathlib import Path

# Adjust paths manually since we are outside flatpak sandbox
DB_FILE = Path.home() / ".var/app/io.github.fastrizwaan.PopcornBox/io.github.fastrizwaan.PopcornBox/data/config/data.json"

if not DB_FILE.exists():
    print("File does not exist")
    sys.exit(1)

import json
try:
    with open(DB_FILE, "r") as f:
        data = json.load(f)
    print("Loaded data successfully. Keys:", data.keys())
    print("Addons count:", len(data.get("addons", [])))
except Exception as e:
    print("Error reading json:", e)
