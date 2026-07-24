import mpv
try:
    player = mpv.MPV(scripts="/does/not/exist.so")
    print("Success")
except Exception as e:
    print(f"Error: {e}")
