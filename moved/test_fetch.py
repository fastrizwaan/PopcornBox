import sys
sys.path.insert(0, '/var/home/rizvan/popcorn-box/src')
from popcorn_box import api, database
# patch cache so it uses existing data
details = api.fetch_movie_details("tt0410975", "series")
if details and details.get("videos"):
    vids = [v for v in details["videos"] if v["season"] == 1]
    print(f"Total videos for season 1: {len(vids)}")
    for v in vids[:10]:
        print(v)
