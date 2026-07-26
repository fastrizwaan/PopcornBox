import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

@Gtk.Template(string="<interface><template class='TestWin' parent='GtkWindow'><child><GtkScrolledWindow id='sw'/></child></template></interface>")
class TestWin(Gtk.Window):
    __gtype_name__ = 'TestWin'
    sw = Gtk.Template.Child()
    
    def __init__(self):
        super().__init__()
        print("hasattr:", hasattr(self, "sw"))
        print("sw:", self.sw)
        
TestWin()
