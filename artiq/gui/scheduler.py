import time
import asyncio

from gi.repository import Gtk

from artiq.gui.tools import Window, ListSyncer
from artiq.management.sync_struct import Subscriber


class _QueueStoreSyncer(ListSyncer):
    def convert(self, x):
        rid, run_params, timeout = x
        row = [rid, run_params["file"]]
        for e in run_params["unit"], timeout:
            row.append("-" if e is None else str(e))
        return row


class _PeriodicStoreSyncer:
    def __init__(self, periodic_store, init):
        self.periodic_store = periodic_store
        self.periodic_store.clear()
        self.order = []
        for prid, x in sorted(init.items(), key=lambda e: (e[1][0], e[0])):
            self.periodic_store.append(self._convert(prid, x))
            self.order.append((x[0], prid))

    def _convert(self, prid, x):
        next_run, run_params, timeout, period = x
        row = [time.strftime("%m/%d %H:%M:%S", time.localtime(next_run)),
               prid, run_params["file"]]
        for e in run_params["unit"], timeout:
            row.append("-" if e is None else str(e))
        row.append(str(period))
        return row

    def _find_index(self, prid):
        for i, e in enumerate(self.periodic_store):
            if e[1] == prid:
                return i
        raise KeyError

    def __setitem__(self, prid, x):
        try:
            i = self._find_index(prid)
        except KeyError:
            pass
        else:
            del self.periodic_store[i]
            del self.order[i]
        ord_el = (x[0], prid)
        j = len(self.order)
        for i, o in enumerate(self.order):
            if o > ord_el:
                j = i
                break
        self.periodic_store.insert(j, self._convert(prid, x))
        self.order.insert(j, ord_el)

    def __delitem__(self, key):
        i = self._find_index(key)
        del self.periodic_store[i]
        del self.order[i]


class SchedulerWindow(Window):
    def __init__(self):
        Window.__init__(self, title="Scheduler")
        self.set_default_size(720, 570)

        topvbox = Gtk.VBox(spacing=6)
        self.add(topvbox)

        hbox = Gtk.HBox(spacing=6)
        enable = Gtk.Switch(active=True)
        label = Gtk.Label("Run experiments")
        hbox.pack_start(label, False, False, 0)
        hbox.pack_start(enable, False, False, 0)
        topvbox.pack_start(hbox, False, False, 0)

        notebook = Gtk.Notebook()
        topvbox.pack_start(notebook, True, True, 0)

        self.queue_store = Gtk.ListStore(int, str, str, str)
        tree = Gtk.TreeView(self.queue_store)
        for i, title in enumerate(["RID", "File", "Unit", "Timeout"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.add(tree)
        vbox = Gtk.VBox(spacing=6)
        vbox.pack_start(scroll, True, True, 0)
        hbox = Gtk.HBox(spacing=6)
        button = Gtk.Button("Find")
        hbox.pack_start(button, True, True, 0)
        button = Gtk.Button("Move up")
        hbox.pack_start(button, True, True, 0)
        button = Gtk.Button("Move down")
        hbox.pack_start(button, True, True, 0)
        button = Gtk.Button("Remove")
        hbox.pack_start(button, True, True, 0)
        vbox.pack_start(hbox, False, False, 0)
        vbox.set_border_width(6)
        notebook.insert_page(vbox, Gtk.Label("Queue"), -1)

        self.periodic_store = Gtk.ListStore(str, int, str, str, str, str)
        tree = Gtk.TreeView(self.periodic_store)
        for i, title in enumerate(["Next run", "PRID", "File", "Unit",
                                   "Timeout", "Period"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            tree.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.add(tree)
        vbox = Gtk.VBox(spacing=6)
        vbox.pack_start(scroll, True, True, 0)
        hbox = Gtk.HBox(spacing=6)
        button = Gtk.Button("Change period")
        hbox.pack_start(button, True, True, 0)
        button = Gtk.Button("Remove")
        hbox.pack_start(button, True, True, 0)
        vbox.pack_start(hbox, False, False, 0)
        vbox.set_border_width(6)
        notebook.insert_page(vbox, Gtk.Label("Periodic schedule"), -1)

    @asyncio.coroutine
    def sub_connect(self, host, port):
        self.queue_subscriber = Subscriber("queue", self.init_queue_store)
        yield from self.queue_subscriber.connect(host, port)
        try:
            self.periodic_subscriber = Subscriber(
                "periodic", self.init_periodic_store)
            yield from self.periodic_subscriber.connect(host, port)
        except:
            yield from self.queue_subscriber.close()
            raise

    @asyncio.coroutine
    def sub_close(self):
        yield from self.periodic_subscriber.close()
        yield from self.queue_subscriber.close()

    def init_queue_store(self, init):
        return _QueueStoreSyncer(self.queue_store, init)

    def init_periodic_store(self, init):
        return _PeriodicStoreSyncer(self.periodic_store, init)