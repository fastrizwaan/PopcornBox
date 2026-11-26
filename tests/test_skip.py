import urllib.request, json
try:
    d1 = json.loads(urllib.request.urlopen('https://v3-cinemeta.strem.io/catalog/movie/top/skip=50.json', timeout=5).read()).get('metas', [])
    print("skip=50:", len(d1))
except Exception as e:
    print("skip=50 error:", e)
try:
    d2 = json.loads(urllib.request.urlopen('https://v3-cinemeta.strem.io/catalog/movie/top/skip=100.json', timeout=5).read()).get('metas', [])
    print("skip=100:", len(d2))
except Exception as e:
    print("skip=100 error:", e)
