import json
import os
from popcorn_box import database

print("Initial:", [d['name'] for d in database.get_downloads()])
h = "12345"
database.add_download(h, "Test Name", "magnet:?xt=urn:btih:12345")
print("After add:", [d['name'] for d in database.get_downloads()])
database.remove_download(h)
print("After delete:", [d['name'] for d in database.get_downloads()])
database.add_download(h, "Test Name", "magnet:?xt=urn:btih:12345")
print("After add again:", [d['name'] for d in database.get_downloads()])
