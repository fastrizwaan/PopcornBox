import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib
sys.path.append("/var/home/rizvan/PopcornBox/src")

def on_activate(app):
    from window import CineWindow
    win = CineWindow(application=app)
    app.add_window(win)
    print(f"Anime supported: {win.anime_supported}")
    print(f"Movies tab button visible: {win.anime_inactive_btn_movies.get_visible()}")
    print(f"Series tab button visible: {win.anime_inactive_btn_series.get_visible()}")
    sys.exit(0)

app = Adw.Application(application_id="io.github.fastrizwaan.PopcornBox.Test")
app.connect("activate", on_activate)
app.run(None)
