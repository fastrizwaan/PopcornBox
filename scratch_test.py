import sys
sys.path.append("/var/home/rizvan/PopcornBox/src")
import database
addons = database.get_addons()
for addon in addons:
    if not addon.get("enabled", True): continue
    types = addon.get("types", [])
    has_anime = "anime" in types
    if has_anime:
        print(f"Addon {addon.get('name')} supports anime in types")
    for cat in addon.get("catalogs", []):
        if cat.get("type") == "anime":
            print(f"Addon {addon.get('name')} supports anime in catalogs")
