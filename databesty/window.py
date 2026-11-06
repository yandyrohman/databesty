"""Main window: connections/tables sidebar on the left, tabs on the right."""

from gi.repository import Adw, Gio, GLib, Gtk

from . import store
from .conn_dialog import ConnectionDialog
from .console_tab import ConsoleTab
from .drivers import DRIVER_LABELS, make_driver
from .grid import run_async
from .table_tab import TableTab

BADGE = {"postgresql": "PG", "mysql": "MY", "sqlite": "SQ"}


class Session:
    """A live connection."""

    def __init__(self, conn, driver, version):
        self.conn = conn
        self.driver = driver
        self.version = version
        self.schemas = []
        self.schema = ""
        self.tables = []


class ConnectionRow(Gtk.ListBoxRow):
    def __init__(self, window, conn):
        super().__init__()
        self.window = window
        self.conn = conn
        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=4, margin_end=2)
        self.badge = Gtk.Label(css_classes=["db-badge"], valign=Gtk.Align.CENTER)
        box.append(self.badge)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.name_lbl = Gtk.Label(xalign=0, ellipsize=3, css_classes=["heading"])
        self.sub_lbl = Gtk.Label(xalign=0, ellipsize=1, css_classes=["dim-label", "caption"])
        texts.append(self.name_lbl)
        texts.append(self.sub_lbl)
        box.append(texts)
        self.spinner = Adw.Spinner(visible=False)
        box.append(self.spinner)
        self.dot = Gtk.Box(css_classes=["live-dot"], valign=Gtk.Align.CENTER, visible=False, tooltip_text="Terhubung")
        box.append(self.dot)

        menu = Gio.Menu()
        menu.append("Hubungkan", f"win.conn-open::{conn.id}")
        menu.append("Edit…", f"win.conn-edit::{conn.id}")
        menu.append("Duplikat", f"win.conn-duplicate::{conn.id}")
        menu.append("Putuskan", f"win.conn-disconnect::{conn.id}")
        danger = Gio.Menu()
        danger.append("Hapus", f"win.conn-delete::{conn.id}")
        menu.append_section(None, danger)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                              css_classes=["flat", "conn-more"], tooltip_text="Opsi")
        box.append(more)
        self.set_child(box)

        click = Gtk.GestureClick(button=3)
        click.connect("pressed", lambda *_: more.popup())
        self.add_controller(click)
        self.refresh()

    def refresh(self):
        c = self.conn
        self.badge.set_text(BADGE[c.info.driver])
        for col in store.COLORS:
            self.badge.remove_css_class(f"swatch-{col}")
        self.badge.add_css_class(f"swatch-{c.color}")
        self.badge.set_tooltip_text(DRIVER_LABELS[c.info.driver])
        self.name_lbl.set_text(c.name)
        self.sub_lbl.set_text(c.info.summary())
        self.set_tooltip_text(f"{DRIVER_LABELS[c.info.driver]} · {c.info.summary()}")
        self.dot.set_visible(c.id in self.window.sessions)

    def busy(self, on):
        self.spinner.set_visible(on)
        if on:
            self.dot.set_visible(False)
        else:
            self.dot.set_visible(self.conn.id in self.window.sessions)


class TableRow(Gtk.ListBoxRow):
    def __init__(self, table):
        super().__init__()
        self.table = table
        box = Gtk.Box(spacing=10, margin_top=3, margin_bottom=3, margin_start=4)
        icon = "databesty-view-symbolic" if table.kind == "view" else "databesty-table-symbolic"
        img = Gtk.Image(icon_name=icon, css_classes=["dim-label"])
        box.append(img)
        box.append(Gtk.Label(label=table.name, xalign=0, ellipsize=3, hexpand=True))
        if table.kind == "view":
            box.append(Gtk.Label(label="view", css_classes=["dim-label", "caption"], margin_end=6))
        self.set_child(box)
        self.set_tooltip_text(f"{table.schema}.{table.name}" if table.schema else table.name)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Databesty")
        self.set_default_size(1320, 820)
        self.set_size_request(760, 480)
        self.connections = store.load()
        self.sessions = {}
        self.current = None  # Session shown in the tables page
        self.conn_rows = {}

        self._build()
        self._actions()
        self._fill_connections()

    # layout -----------------------------------------------------------------------
    def _build(self):
        self.toasts = Adw.ToastOverlay()
        split = Adw.NavigationSplitView(min_sidebar_width=250, max_sidebar_width=360, sidebar_width_fraction=0.24)
        self.toasts.set_child(split)
        self.set_content(self.toasts)

        # sidebar: navigation between connection list and table list
        self.nav = Adw.NavigationView()
        split.set_sidebar(Adw.NavigationPage(child=self.nav, title="Databesty"))

        # page 1: connections
        h = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Koneksi baru (Ctrl+N)")
        add.set_action_name("win.conn-new")
        h.pack_start(add)
        menu = Gio.Menu()
        menu.append("Pintasan keyboard", "app.shortcuts")
        menu.append("Tentang Databesty", "app.about")
        h.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu utama", primary=True))
        h.set_title_widget(Adw.WindowTitle(title="Databesty"))

        self.conn_list = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.NONE)
        self.conn_list.connect("row-activated", lambda _l, row: self.open_connection(row.conn))
        conn_scroll = Gtk.ScrolledWindow(child=self.conn_list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        label = Gtk.Label(label="KONEKSI", xalign=0, css_classes=["sidebar-section"], margin_start=18, margin_top=6)
        conn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        conn_box.append(label)
        conn_box.append(conn_scroll)

        self.conn_empty = Adw.StatusPage(
            icon_name="databesty-database-symbolic",
            title="Belum ada koneksi",
            description="Tambahkan koneksi PostgreSQL, MySQL atau SQLite.",
            css_classes=["compact"],
        )
        btn = Gtk.Button(label="Tambah koneksi", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
        btn.set_action_name("win.conn-new")
        self.conn_empty.set_child(btn)
        self.conn_stack = Gtk.Stack()
        self.conn_stack.add_named(conn_box, "list")
        self.conn_stack.add_named(self.conn_empty, "empty")
        tv = Adw.ToolbarView(content=self.conn_stack)
        tv.add_top_bar(h)
        self.nav.add(Adw.NavigationPage(child=tv, title="Koneksi", tag="connections"))

        # page 2: tables of one connection
        h2 = Adw.HeaderBar()
        self.tables_title = Adw.WindowTitle()
        h2.set_title_widget(self.tables_title)
        console = Gtk.Button(icon_name="utilities-terminal-symbolic", tooltip_text="SQL console baru (Ctrl+T)")
        console.set_action_name("win.new-console")
        h2.pack_end(console)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Muat ulang daftar tabel")
        refresh.connect("clicked", lambda *_: self.load_tables(self.current))
        h2.pack_end(refresh)

        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        search_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=10, margin_end=10, margin_top=6, margin_bottom=6)
        self.search = Gtk.SearchEntry(placeholder_text="Cari tabel…")
        self.search.connect("search-changed", lambda *_: self.table_list.invalidate_filter())
        self.search.connect("activate", self._search_activate)
        search_box.append(self.search)
        self.schema_dd = Gtk.DropDown(tooltip_text="Schema / database")
        self.schema_dd.connect("notify::selected", self._schema_changed)
        search_box.append(self.schema_dd)
        tbox.append(search_box)

        self.table_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.table_list.set_filter_func(self._filter_table)
        self.table_list.set_header_func(self._table_header)
        self.table_list.connect("row-activated", lambda _l, row: self.open_table(self.current, row.table))
        self.tables_stack = Gtk.Stack()
        self.tables_stack.add_named(Gtk.ScrolledWindow(child=self.table_list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        self.tables_stack.add_named(Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=28, height_request=28), "loading")
        self.tables_empty = Adw.StatusPage(icon_name="databesty-table-symbolic", title="Tidak ada tabel", css_classes=["compact"])
        self.tables_stack.add_named(self.tables_empty, "empty")
        tbox.append(self.tables_stack)

        footer = Gtk.Box(spacing=6, css_classes=["sidebar-footer"])
        self.footer_lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=3, css_classes=["dim-label", "caption"])
        footer.append(self.footer_lbl)
        disc = Gtk.Button(label="Putuskan", css_classes=["flat", "caption"])
        disc.connect("clicked", lambda *_: self.current and self.disconnect(self.current.conn.id))
        footer.append(disc)

        tv2 = Adw.ToolbarView(content=tbox)
        tv2.add_top_bar(h2)
        tv2.add_bottom_bar(footer)
        self.tables_page = Adw.NavigationPage(child=tv2, title="Tabel", tag="tables")
        self.nav.connect("popped", self._nav_popped)

        # content: tabs
        ch = Adw.HeaderBar()
        self.content_title = Adw.WindowTitle(title="Databesty")
        ch.set_title_widget(self.content_title)
        self.tab_view = Adw.TabView()
        self.tab_view.connect("notify::selected-page", self._tab_selected)
        self.tab_view.connect("close-page", self._tab_close)
        self.tab_view.connect("notify::n-pages", lambda *_: self._sync_content())
        tab_bar = Adw.TabBar(view=self.tab_view, autohide=False, expand_tabs=False)

        self.welcome = Adw.StatusPage(
            icon_name="io.github.yandyrohman.Databesty",
            title="Databesty",
            description="Pilih koneksi di sebelah kiri, lalu buka tabel untuk melihat dan mengubah datanya.",
        )
        wbtn = Gtk.Button(label="Koneksi baru", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
        wbtn.set_action_name("win.conn-new")
        self.welcome.set_child(wbtn)

        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        tabs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tabs_box.append(tab_bar)
        tabs_box.append(self.tab_view)
        self.tab_view.set_vexpand(True)
        self.content_stack.add_named(self.welcome, "welcome")
        self.content_stack.add_named(tabs_box, "tabs")
        ctv = Adw.ToolbarView(content=self.content_stack, top_bar_style=Adw.ToolbarStyle.FLAT)
        ctv.add_top_bar(ch)
        split.set_content(Adw.NavigationPage(child=ctv, title="Databesty"))

    def _actions(self):
        def add(name, cb, param=None, accel=None):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
            a.connect("activate", cb)
            self.add_action(a)
            if accel:
                self.get_application().set_accels_for_action(f"win.{name}", accel)
            return a

        add("conn-new", lambda *_: self.edit_connection(None), accel=["<Control>n"])
        add("conn-open", lambda _a, p: self._with_conn(p, self.open_connection), "s")
        add("conn-edit", lambda _a, p: self._with_conn(p, self.edit_connection), "s")
        add("conn-duplicate", lambda _a, p: self._with_conn(p, self.duplicate_connection), "s")
        add("conn-delete", lambda _a, p: self._with_conn(p, self.delete_connection), "s")
        add("conn-disconnect", lambda _a, p: self.disconnect(p.get_string()), "s")
        add("new-console", lambda *_: self.new_console(), accel=["<Control>t"])
        add("close-tab", lambda *_: self._close_current_tab(), accel=["<Control>w"])
        add("reload", lambda *_: self._reload_current(), accel=["F5", "<Control>r"])
        add("find-table", lambda *_: self._focus_search(), accel=["<Control>p"])

    def _with_conn(self, param, fn):
        cid = param.get_string()
        conn = next((c for c in self.connections if c.id == cid), None)
        if conn:
            fn(conn)

    # messages -----------------------------------------------------------------------
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

    def error(self, heading, body):
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    # connections ---------------------------------------------------------------------
    def _fill_connections(self):
        while (row := self.conn_list.get_row_at_index(0)) is not None:
            self.conn_list.remove(row)
        self.conn_rows = {}
        for c in self.connections:
            r = ConnectionRow(self, c)
            self.conn_rows[c.id] = r
            self.conn_list.append(r)
        self.conn_stack.set_visible_child_name("list" if self.connections else "empty")

    def _persist(self):
        try:
            store.save(self.connections)
        except OSError as e:
            self.error("Gagal menyimpan koneksi", str(e))

    def edit_connection(self, conn):
        def saved(new):
            existing = next((i for i, c in enumerate(self.connections) if c.id == new.id), None)
            if existing is None:
                self.connections.append(new)
            else:
                self.connections[existing] = new
                if new.id in self.sessions:
                    # settings changed: reconnect on next open
                    self.disconnect(new.id, quiet=True)
            self._persist()
            self._fill_connections()
            if existing is None:
                self.open_connection(new)

        ConnectionDialog(conn, saved).present(self)

    def duplicate_connection(self, conn):
        import copy

        dup = copy.deepcopy(conn)
        dup.id = store.SavedConnection().id
        dup.name = conn.name + " (salinan)"
        self.connections.insert(self.connections.index(conn) + 1, dup)
        self._persist()
        self._fill_connections()

    def delete_connection(self, conn):
        dlg = Adw.AlertDialog(heading=f"Hapus koneksi “{conn.name}”?", body="Hanya pengaturan koneksi yang dihapus, database-nya tidak tersentuh.")
        dlg.add_response("cancel", "Batal")
        dlg.add_response("delete", "Hapus")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")

        def respond(_d, r):
            if r == "delete":
                self.disconnect(conn.id, quiet=True)
                self.connections = [c for c in self.connections if c.id != conn.id]
                self._persist()
                self._fill_connections()

        dlg.connect("response", respond)
        dlg.present(self)

    def open_connection(self, conn):
        if conn.id in self.sessions:
            self.show_session(self.sessions[conn.id])
            return
        row = self.conn_rows.get(conn.id)
        if row:
            row.busy(True)

        def work():
            d = make_driver(conn.info)
            d.connect()
            return d, d.server_version()

        def done(res):
            driver, version = res
            s = Session(conn, driver, version)
            self.sessions[conn.id] = s
            if row:
                row.busy(False)
            self.show_session(s)

        def fail(e):
            if row:
                row.busy(False)
            self.error(f"Tidak bisa terhubung ke “{conn.name}”", str(e))

        run_async(work, done, fail)

    def disconnect(self, cid, quiet=False):
        s = self.sessions.pop(cid, None)
        if not s:
            return
        for i in reversed(range(self.tab_view.get_n_pages())):
            page = self.tab_view.get_nth_page(i)
            if page.get_child().session is s:
                self.tab_view.close_page(page)
        threading_close(s.driver)
        if self.current is s:
            self.current = None
            self.nav.pop_to_tag("connections")
        row = self.conn_rows.get(cid)
        if row:
            row.refresh()
        if not quiet:
            self.toast(f"Terputus dari {s.conn.name}")

    def show_session(self, s):
        self.current = s
        self.tables_title.set_title(s.conn.name)
        self.tables_title.set_subtitle(s.version if len(s.version) < 40 else s.version[:40] + "…")
        self.footer_lbl.set_text(f"{DRIVER_LABELS[s.conn.info.driver]} · {s.conn.info.summary()}")
        self.search.set_text("")
        if self.nav.get_visible_page() is not self.tables_page:
            self.nav.push(self.tables_page)
        row = self.conn_rows.get(s.conn.id)
        if row:
            row.refresh()
        self.load_tables(s)

    def _nav_popped(self, _nav, page):
        if page is self.tables_page:
            self.current = None

    # tables --------------------------------------------------------------------------
    def load_tables(self, s, keep_schema=True):
        if s is None:
            return
        self.tables_stack.set_visible_child_name("loading")
        drv = s.driver

        def work():
            schemas = drv.schemas()
            schema = s.schema if keep_schema and s.schema in schemas else drv.default_schema(schemas)
            return schemas, schema, drv.tables(schema)

        def done(res):
            s.schemas, s.schema, s.tables = res
            if self.current is s:
                self._show_tables(s)

        def fail(e):
            if self.current is s:
                self.tables_empty.set_title("Gagal memuat tabel")
                self.tables_empty.set_description(GLib.markup_escape_text(str(e)))
                self.tables_stack.set_visible_child_name("empty")

        run_async(work, done, fail)

    def _show_tables(self, s):
        self._schema_lock = True
        self.schema_dd.set_model(Gtk.StringList.new(s.schemas))
        if s.schema in s.schemas:
            self.schema_dd.set_selected(s.schemas.index(s.schema))
        self.schema_dd.set_visible(len(s.schemas) > 1)
        self._schema_lock = False
        while (row := self.table_list.get_row_at_index(0)) is not None:
            self.table_list.remove(row)
        ordered = sorted(s.tables, key=lambda t: (t.kind != "table", t.name.lower()))
        for t in ordered:
            self.table_list.append(TableRow(t))
        if ordered:
            self.tables_stack.set_visible_child_name("list")
        else:
            self.tables_empty.set_title("Tidak ada tabel")
            self.tables_empty.set_description(f"Schema “{s.schema}” masih kosong." if s.schema else None)
            self.tables_stack.set_visible_child_name("empty")

    def _schema_changed(self, dd, _p):
        if getattr(self, "_schema_lock", False) or not self.current:
            return
        item = dd.get_selected_item()
        if item is None:
            return
        self.current.schema = item.get_string()
        self.load_tables(self.current)

    def _filter_table(self, row):
        q = self.search.get_text().strip().lower()
        return not q or q in row.table.name.lower()

    def _table_header(self, row, before):
        if before is None or before.table.kind != row.table.kind:
            n = sum(1 for t in (self.current.tables if self.current else []) if t.kind == row.table.kind)
            lbl = Gtk.Label(label=("TABEL" if row.table.kind == "table" else "VIEW") + f"  {n}",
                            xalign=0, css_classes=["sidebar-section"], margin_start=8, margin_top=6 if before else 0)
            row.set_header(lbl)
        else:
            row.set_header(None)

    def _search_activate(self, *_):
        i = 0
        while (row := self.table_list.get_row_at_index(i)) is not None:
            if self._filter_table(row):
                self.open_table(self.current, row.table)
                return
            i += 1

    def _focus_search(self):
        if self.current:
            self.search.grab_focus()

    def schema_changed(self, s):
        """A console ran DDL/DML; refresh the table list if it is showing."""
        if self.current is s:
            self.load_tables(s)

    # tabs ------------------------------------------------------------------------------
    def _find_tab(self, key):
        for i in range(self.tab_view.get_n_pages()):
            page = self.tab_view.get_nth_page(i)
            if page.get_child().key == key:
                return page
        return None

    def _add_tab(self, widget, icon):
        page = self.tab_view.append(widget)
        page.set_title(widget.title)
        page.set_tooltip(GLib.markup_escape_text(widget.tooltip))
        page.set_icon(Gio.ThemedIcon.new(icon))
        self.tab_view.set_selected_page(page)
        return page

    def open_table(self, s, table):
        if s is None:
            return
        key = ("table", s.conn.id, table.schema, table.name)
        page = self._find_tab(key)
        if page:
            self.tab_view.set_selected_page(page)
            return
        icon = "databesty-view-symbolic" if table.kind == "view" else "databesty-table-symbolic"
        self._add_tab(TableTab(self, s, table), icon)

    def new_console(self, s=None):
        s = s or self.current or self._selected_session()
        if s is None:
            self.toast("Buka koneksi dulu")
            return
        tab = ConsoleTab(self, s)
        self._add_tab(tab, "utilities-terminal-symbolic")
        GLib.idle_add(lambda: (tab.focus(), False)[1])

    def _selected_session(self):
        page = self.tab_view.get_selected_page()
        return page.get_child().session if page else None

    def _tab_selected(self, *_):
        page = self.tab_view.get_selected_page()
        if page is None:
            self.content_title.set_title("Databesty")
            self.content_title.set_subtitle("")
            return
        tab = page.get_child()
        self.content_title.set_title(tab.title)
        self.content_title.set_subtitle(tab.tooltip.split(" · ")[0])

    def _tab_close(self, view, page):
        view.close_page_finish(page, True)
        return True

    def _close_current_tab(self):
        page = self.tab_view.get_selected_page()
        if page:
            self.tab_view.close_page(page)

    def _reload_current(self):
        page = self.tab_view.get_selected_page()
        if page and isinstance(page.get_child(), TableTab):
            page.get_child().reload()

    def _sync_content(self):
        self.content_stack.set_visible_child_name("tabs" if self.tab_view.get_n_pages() else "welcome")
        if not self.tab_view.get_n_pages():
            self._tab_selected()

    def close_all(self):
        for s in list(self.sessions.values()):
            threading_close(s.driver)
        self.sessions.clear()


def threading_close(driver):
    run_async(driver.close, lambda _r: None, lambda _e: None)
