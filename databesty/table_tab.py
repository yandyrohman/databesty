"""A tab showing one table: data grid with paging, filter, CRUD, and structure."""

from gi.repository import Adw, GLib, Gtk

from .grid import ResultGrid, RowItem, run_async
from .row_dialog import RowDialog

PAGE_SIZES = [50, 100, 200, 500, 1000]


class TableTab(Gtk.Box):
    def __init__(self, window, session, table):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.session = session
        self.table = table
        self.key = ("table", session.conn.id, table.schema, table.name)
        self.columns = []
        self.locator = None
        self.offset = 0
        self.limit = 100
        self.total = None
        self.order_by = None
        self.desc = False
        self.where = ""
        self.loading = False
        self.read_only = table.kind == "view"

        # toolbar ------------------------------------------------------------
        bar = Gtk.Box(spacing=6, css_classes=["toolbar", "table-toolbar"])

        self.mode = Adw.ViewStack()
        switcher = Adw.InlineViewSwitcher(stack=self.mode, css_classes=["round"])
        bar.append(switcher)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4, margin_end=4))

        self.refresh_btn = self._tool("view-refresh-symbolic", "Muat ulang (F5)", self.reload)
        self.add_btn = self._tool("list-add-symbolic", "Tambah baris", self.add_row)
        self.edit_btn = self._tool("document-edit-symbolic", "Edit baris terpilih", self.edit_selected)
        self.del_btn = self._tool("user-trash-symbolic", "Hapus baris terpilih (Delete)", self.delete_selected)
        for b in (self.refresh_btn, self.add_btn, self.edit_btn, self.del_btn):
            bar.append(b)
        if self.read_only:
            for b in (self.add_btn, self.edit_btn, self.del_btn):
                b.set_sensitive(False)

        self.filter = Gtk.Entry(
            placeholder_text="WHERE …   contoh: city = 'Jakarta' AND id > 10",
            hexpand=True,
            primary_icon_name="databesty-filter-symbolic",
            secondary_icon_tooltip_text="Hapus filter",
        )
        self.filter.add_css_class("monospace")
        self.filter.connect("activate", self._apply_filter)
        self.filter.connect("icon-press", self._filter_icon)
        self.filter.connect("changed", self._filter_changed)
        bar.append(self.filter)

        self.prev_btn = self._tool("go-previous-symbolic", "Halaman sebelumnya", self.prev_page)
        self.next_btn = self._tool("go-next-symbolic", "Halaman berikutnya", self.next_page)
        self.range_lbl = Gtk.Label(css_classes=["dim-label", "numeric", "caption"], width_chars=16)
        self.size_dd = Gtk.DropDown.new_from_strings([f"{n} / hal" for n in PAGE_SIZES])
        self.size_dd.set_selected(PAGE_SIZES.index(self.limit))
        self.size_dd.set_tooltip_text("Baris per halaman")
        self.size_dd.connect("notify::selected", self._size_changed)
        bar.append(self.prev_btn)
        bar.append(self.range_lbl)
        bar.append(self.next_btn)
        bar.append(self.size_dd)
        self.append(bar)

        # data page ------------------------------------------------------------
        self.grid = ResultGrid(sortable=True)
        self.grid.on_sort = self._sorted
        self.grid.on_activate = lambda item: self.edit_row(item)
        self.grid.on_delete_key = self.delete_selected
        self.grid.selection.connect("selection-changed", lambda *_: self._sync_buttons())

        self.data_stack = Gtk.Stack()
        self.data_stack.add_named(self.grid, "grid")
        self.error_page = Adw.StatusPage(icon_name="dialog-warning-symbolic", title="Query gagal")
        self.error_page.add_css_class("compact")
        self.data_stack.add_named(self.error_page, "error")
        spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        self.data_stack.add_named(spinner, "loading")
        self.data_stack.set_visible_child_name("loading")
        self.mode.add_titled_with_icon(self.data_stack, "data", "Data", "view-grid-symbolic")

        # structure page ---------------------------------------------------------
        self.struct_grid = ResultGrid()
        self.mode.add_titled_with_icon(self.struct_grid, "structure", "Struktur", "view-list-symbolic")
        self.mode.connect("notify::visible-child-name", lambda *_: self._sync_buttons())

        self.append(self.mode)
        self._sync_buttons()
        self.load_columns()

    # helpers ----------------------------------------------------------------
    @property
    def title(self):
        return self.table.name

    @property
    def tooltip(self):
        where = f"{self.table.schema}." if self.table.schema else ""
        return f"{self.session.conn.name} · {where}{self.table.name}"

    def _tool(self, icon, tip, cb):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"])
        b.connect("clicked", lambda *_: cb())
        return b

    def _sync_buttons(self):
        on_data = self.mode.get_visible_child_name() == "data"
        n = len(self.grid.selected_items()) if on_data else 0
        writable = on_data and not self.read_only and bool(self.columns)
        self.add_btn.set_sensitive(writable)
        self.edit_btn.set_sensitive(writable and n == 1)
        self.del_btn.set_sensitive(writable and n >= 1)
        for w in (self.filter, self.prev_btn, self.next_btn, self.size_dd, self.range_lbl):
            w.set_visible(on_data)
        if on_data:
            self.prev_btn.set_sensitive(self.offset > 0 and not self.loading)
            more = self.total is None or self.offset + self.limit < self.total
            self.next_btn.set_sensitive(more and not self.loading and self.grid.store.get_n_items() >= self.limit)

    def toast(self, msg):
        self.window.toast(msg)

    # loading ----------------------------------------------------------------
    def load_columns(self):
        drv = self.session.driver

        def done(cols):
            self.columns = cols
            self.struct_grid.set_columns(["#", "kolom", "tipe", "null", "default", "key"])
            self.struct_grid.set_rows(
                [
                    (None, [i + 1, c.name, c.type, "YES" if c.nullable else "NO", c.default,
                            ("PK" if c.primary_key else "") + (" auto" if c.auto else "")])
                    for i, c in enumerate(cols)
                ]
            )
            self.grid.set_columns([c.name for c in cols])
            self.reload()

        run_async(lambda: drv.columns(self.table), done, self._show_error)

    def reload(self, recount=True):
        if not self.columns or self.loading:
            if not self.columns:
                self.load_columns()
            return
        self.loading = True
        self.refresh_btn.set_sensitive(False)
        if self.grid.store.get_n_items() == 0:
            self.data_stack.set_visible_child_name("loading")
        drv = self.session.driver
        table, cols = self.table, self.columns
        limit, offset, where, order, desc = self.limit, self.offset, self.where, self.order_by, self.desc

        def work():
            rows, locator = drv.select_page(table, cols, limit, offset, where, order, desc)
            total = drv.count(table, where) if recount else self.total
            return rows, locator, total

        def done(res):
            rows, self.locator, self.total = res
            self.loading = False
            self.refresh_btn.set_sensitive(True)
            self.grid.set_rows(rows)
            self.data_stack.set_visible_child_name("grid")
            self._update_range(len(rows))
            self._sync_buttons()

        def fail(e):
            self.loading = False
            self.refresh_btn.set_sensitive(True)
            self._show_error(e)
            self._sync_buttons()

        run_async(work, done, fail)

    def _update_range(self, n):
        if n == 0:
            text = "0 baris" if not self.total else f"– dari {self.total:,}"
        else:
            text = f"{self.offset + 1:,}–{self.offset + n:,}"
            if self.total is not None:
                text += f" dari {self.total:,}"
        self.range_lbl.set_text(text)

    def _show_error(self, e):
        self.error_page.set_description(GLib.markup_escape_text(str(e)))
        self.data_stack.set_visible_child_name("error")

    # paging / sort / filter ---------------------------------------------------
    def prev_page(self):
        self.offset = max(0, self.offset - self.limit)
        self.reload(recount=False)

    def next_page(self):
        self.offset += self.limit
        self.reload(recount=False)

    def _size_changed(self, dd, _p):
        self.limit = PAGE_SIZES[dd.get_selected()]
        self.offset = 0
        self.reload(recount=False)

    def _sorted(self, column, desc):
        self.order_by, self.desc = column, desc
        self.offset = 0
        self.reload(recount=False)

    def _apply_filter(self, *_):
        self.where = self.filter.get_text().strip()
        self.offset = 0
        self.reload()

    def _filter_changed(self, entry):
        entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "edit-clear-symbolic" if entry.get_text() else None
        )

    def _filter_icon(self, entry, pos):
        if pos == Gtk.EntryIconPosition.SECONDARY:
            entry.set_text("")
            if self.where:
                self._apply_filter()

    # CRUD ---------------------------------------------------------------------
    def add_row(self):
        if self.read_only or not self.columns:
            return
        drv = self.session.driver

        def save(values, done):
            def ok(_n):
                done(None)
                self.toast("Baris ditambahkan")
                self.reload()

            run_async(lambda: drv.insert(self.table, values), ok, done)

        RowDialog(self.table, self.columns, None, save).present(self.window)

    def edit_selected(self):
        items = self.grid.selected_items()
        if len(items) == 1:
            self.edit_row(items[0])

    def edit_row(self, item: RowItem):
        if not self.columns:
            return
        drv = self.session.driver
        cols, locator = self.columns, self.locator

        def save(changes, done):
            def ok(n):
                done(None)
                if n == 0:
                    self.toast("Tidak ada baris yang berubah (mungkin sudah diubah orang lain)")
                else:
                    self.toast("Perubahan disimpan")
                self.reload(recount=False)

            run_async(
                lambda: drv.update(self.table, cols, locator, item.values, item.hidden, changes), ok, done
            )

        RowDialog(self.table, cols, item.values, save, read_only=self.read_only).present(self.window)

    def delete_selected(self):
        if self.read_only:
            return
        items = self.grid.selected_items()
        if not items:
            return
        n = len(items)
        dlg = Adw.AlertDialog(
            heading=f"Hapus {n} baris?",
            body=f"Baris akan dihapus permanen dari tabel {self.table.name}.",
        )
        dlg.add_response("cancel", "Batal")
        dlg.add_response("delete", "Hapus")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        drv = self.session.driver
        cols, locator = self.columns, self.locator
        rows = [(it.hidden, it.values) for it in items]

        def respond(_d, response):
            if response != "delete":
                return

            def ok(deleted):
                self.toast(f"{deleted} baris dihapus")
                if self.offset and self.offset >= (self.total or 0) - deleted:
                    self.offset = max(0, self.offset - self.limit)
                self.reload()

            run_async(lambda: drv.delete_rows(self.table, cols, locator, rows), ok,
                      lambda e: self.window.error("Gagal menghapus", str(e)))

        dlg.connect("response", respond)
        dlg.present(self.window)

    def copy(self):
        n = self.grid.copy_selection()
        if n:
            self.toast(f"{n} baris disalin")

