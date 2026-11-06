"""Result grid shared by table tabs and the SQL console."""

import datetime
import decimal
import json
import threading

from gi.repository import Gdk, GLib, GObject, Gio, Gtk


def run_async(fn, on_done, on_error=None):
    """Run fn() on a worker thread, then call back on the GTK main loop."""

    def worker():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            if on_error:
                GLib.idle_add(lambda err=e: (on_error(err), False)[1])
            return
        GLib.idle_add(lambda: (on_done(result), False)[1])

    threading.Thread(target=worker, daemon=True).start()


def display(value, limit=300):
    """Short single-line text for a grid cell."""
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        h = b[:24].hex()
        return f"0x{h}{'…' if len(b) > 24 else ''}  ({len(b)} B)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, datetime.datetime):
        s = value.isoformat(" ")
    else:
        s = str(value)
    s = s.replace("\r\n", "↵").replace("\n", "↵").replace("\t", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def to_text(value):
    """Full editable text for a value (inverse of from_text as far as possible)."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, datetime.datetime):
        return value.isoformat(" ")
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    return str(value)


def tsv_cell(value):
    if value is None:
        return ""
    return to_text(value).replace("\t", " ").replace("\n", "\\n")


class RowItem(GObject.Object):
    __gtype_name__ = "DatabestyRow"

    def __init__(self, values, hidden=None):
        super().__init__()
        self.values = values
        self.hidden = hidden


class ResultGrid(Gtk.ScrolledWindow):
    """A Gtk.ColumnView that shows rows of python values.

    sortable=True adds clickable headers; the caller listens to `on_sort`
    and re-queries the database (sorting happens server-side).
    """

    def __init__(self, sortable=False):
        super().__init__(hexpand=True, vexpand=True)
        self.store = Gio.ListStore(item_type=RowItem)
        self.selection = Gtk.MultiSelection(model=self.store)
        self.view = Gtk.ColumnView(
            model=self.selection,
            show_column_separators=True,
            show_row_separators=True,
            reorderable=False,
        )
        self.view.add_css_class("data-grid")
        self.set_child(self.view)
        self.sortable = sortable
        self.on_sort = None
        self.on_activate = None
        self.column_names = []
        self._sort_handler = None
        self.view.connect("activate", self._activated)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.view.add_controller(keys)
        self.on_delete_key = None

    # columns ---------------------------------------------------------------
    def set_columns(self, names, types=None, sort_column=None, sort_desc=False):
        sorter = self.view.get_sorter()
        if self._sort_handler and sorter:
            sorter.disconnect(self._sort_handler)
            self._sort_handler = None
        for col in list(self.view.get_columns()):
            self.view.remove_column(col)
        self.column_names = list(names)
        selected = None
        for idx, name in enumerate(names):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup_cell)
            factory.connect("bind", self._bind_cell, idx)
            col = Gtk.ColumnViewColumn(title=name, factory=factory, resizable=True)
            col.set_fixed_width(self._width_for([len(name)]))
            if self.sortable:
                col.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d: 0, None))
                if name == sort_column:
                    selected = col
            self.view.append_column(col)
        if self.sortable:
            sorter = self.view.get_sorter()
            if selected is not None:
                self.view.sort_by_column(
                    selected, Gtk.SortType.DESCENDING if sort_desc else Gtk.SortType.ASCENDING
                )
            self._sort_handler = sorter.connect("changed", self._sort_changed)
        self._needs_autosize = True

    @staticmethod
    def _width_for(lengths):
        # ~8px per monospace char at 0.9em, plus cell padding; sort arrow room
        longest = max(lengths) if lengths else 4
        return max(70, min(380, 30 + 8 * longest))

    def _autosize(self, rows):
        cols = list(self.view.get_columns())
        sample = rows[:200]
        for idx, col in enumerate(cols):
            lens = [len(self.column_names[idx]) + 2]
            lens += [len(display(r[1][idx], 60)) for r in sample if idx < len(r[1])]
            col.set_fixed_width(self._width_for(lens))

    def _setup_cell(self, _f, item):
        label = Gtk.Label(xalign=0, ellipsize=3, single_line_mode=True, width_chars=4)
        label.add_css_class("cell")
        item.set_child(label)

    def _bind_cell(self, _f, item, idx):
        label = item.get_child()
        row = item.get_item()
        value = row.values[idx] if idx < len(row.values) else None
        label.set_text(display(value))
        if value is None:
            label.add_css_class("null")
        else:
            label.remove_css_class("null")
        if isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool):
            label.set_xalign(1)
        else:
            label.set_xalign(0)

    def _sort_changed(self, sorter, _change):
        if not self.on_sort:
            return
        col = sorter.get_primary_sort_column()
        if col is None:
            self.on_sort(None, False)
            return
        idx = list(self.view.get_columns()).index(col)
        desc = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
        self.on_sort(self.column_names[idx], desc)

    # rows ------------------------------------------------------------------
    def set_rows(self, rows):
        """rows: list of (hidden, values)"""
        if getattr(self, "_needs_autosize", False):
            self._autosize(rows)
            self._needs_autosize = bool(not rows)
        items = [RowItem(values, hidden) for hidden, values in rows]
        self.store.splice(0, self.store.get_n_items(), items)
        self.view.scroll_to(0, None, Gtk.ListScrollFlags.NONE, None) if items else None

    def selected_items(self):
        bits = self.selection.get_selection()
        out = []
        ok, it, pos = Gtk.BitsetIter.init_first(bits)
        while ok:
            out.append(self.store.get_item(pos))
            ok, pos = it.next()
        return out

    def _activated(self, _view, pos):
        if self.on_activate:
            self.on_activate(self.store.get_item(pos))

    def _key_pressed(self, _ctl, keyval, _code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self.copy_selection()
            return True
        if keyval == Gdk.KEY_Delete and self.on_delete_key:
            self.on_delete_key()
            return True
        return False

    def copy_selection(self):
        items = self.selected_items()
        if not items:
            return 0
        lines = ["\t".join(self.column_names)]
        lines += ["\t".join(tsv_cell(v) for v in it.values) for it in items]
        self.get_clipboard().set("\n".join(lines))
        return len(items)
