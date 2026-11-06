"""New / edit connection dialog with two tabs: connection string and fields."""

import copy
import os

from gi.repository import Adw, Gio, GLib, Gtk

from .drivers import DEFAULT_PORTS, DRIVER_LABELS, DRIVERS, ConnInfo, ParseError, build_url, make_driver, parse_url
from .grid import run_async
from .store import COLORS, SavedConnection


class ConnectionDialog(Adw.Dialog):
    def __init__(self, conn: SavedConnection | None, on_save):
        super().__init__()
        self.on_save = on_save
        self.conn = copy.deepcopy(conn) if conn else SavedConnection()
        self.is_new = conn is None
        self.set_title("Koneksi baru" if self.is_new else "Edit koneksi")
        self.set_content_width(560)
        self.set_content_height(700)
        self._syncing = False

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Batal")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save_btn = Gtk.Button(label="Simpan", css_classes=["suggested-action"])
        self.save_btn.connect("clicked", self._save)
        header.pack_end(self.save_btn)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        # general ---------------------------------------------------------------
        general = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title="Nama koneksi")
        self.name_row.set_text(self.conn.name)
        general.add(self.name_row)

        color_row = Adw.ActionRow(title="Warna")
        colors = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        first = None
        self.color_buttons = {}
        for c in COLORS:
            b = Gtk.ToggleButton(css_classes=["color-swatch", f"swatch-{c}"], tooltip_text=c)
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", self._color_toggled, c)
            self.color_buttons[c] = b
            colors.append(b)
        self.color_buttons.get(self.conn.color, first).set_active(True)
        color_row.add_suffix(colors)
        general.add(color_row)
        page.append(general)

        # tabs ------------------------------------------------------------------
        self.stack = Adw.ViewStack(vhomogeneous=False)
        switcher = Adw.InlineViewSwitcher(stack=self.stack, halign=Gtk.Align.CENTER, css_classes=["round"])
        page.append(switcher)

        # tab 1: url
        url_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        url_group = Adw.PreferencesGroup(
            description="Tempel connection string, misalnya dari file .env. Baris DATABASE_URL=… juga bisa."
        )
        self.url_row = Adw.EntryRow(title="Connection string")
        self.url_row.add_css_class("monospace")
        self.url_row.connect("changed", self._url_changed)
        url_group.add(self.url_row)
        url_box.append(url_group)
        self.detect_lbl = Gtk.Label(xalign=0, wrap=True, css_classes=["caption"], margin_start=6)
        url_box.append(self.detect_lbl)
        examples = Gtk.Label(
            xalign=0,
            wrap=True,
            selectable=True,
            css_classes=["dim-label", "caption", "monospace"],
            margin_start=6,
            label="postgres://user:pass@localhost:5432/mydb?sslmode=disable\n"
            "mysql://root:secret@127.0.0.1:3306/shop\n"
            "sqlite:////home/me/data/app.db",
        )
        url_box.append(examples)
        self.stack.add_titled_with_icon(url_box, "url", "Connection string", "insert-link-symbolic")

        # tab 2: fields
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        type_group = Adw.PreferencesGroup()
        self.driver_row = Adw.ComboRow(title="Jenis database", model=Gtk.StringList.new([DRIVER_LABELS[d] for d in DRIVERS]))
        self.driver_row.connect("notify::selected", self._driver_changed)
        type_group.add(self.driver_row)
        fields.append(type_group)

        self.server_group = Adw.PreferencesGroup()
        self.host_row = Adw.EntryRow(title="Host")
        self.port_row = Adw.EntryRow(title="Port", input_purpose=Gtk.InputPurpose.DIGITS)
        self.user_row = Adw.EntryRow(title="User")
        self.pass_row = Adw.PasswordEntryRow(title="Password")
        self.db_row = Adw.EntryRow(title="Database")
        for r in (self.host_row, self.port_row, self.user_row, self.pass_row, self.db_row):
            self.server_group.add(r)
            r.connect("changed", self._fields_changed)
        fields.append(self.server_group)

        self.file_group = Adw.PreferencesGroup()
        self.path_row = Adw.EntryRow(title="File database")
        self.path_row.connect("changed", self._fields_changed)
        browse = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Pilih file…", css_classes=["flat"])
        browse.connect("clicked", self._browse)
        self.path_row.add_suffix(browse)
        self.file_group.add(self.path_row)
        self.create_row = Adw.SwitchRow(title="Buat file baru jika belum ada")
        self.file_group.add(self.create_row)
        fields.append(self.file_group)
        self.stack.add_titled_with_icon(fields, "fields", "Detail koneksi", "view-list-symbolic")
        page.append(self.stack)

        # test ------------------------------------------------------------------
        test_box = Gtk.Box(spacing=12)
        self.test_btn = Gtk.Button(label="Tes koneksi", css_classes=["pill"])
        self.test_btn.connect("clicked", self._test)
        test_box.append(self.test_btn)
        self.test_spinner = Adw.Spinner(visible=False)
        test_box.append(self.test_spinner)
        self.test_lbl = Gtk.Label(xalign=0, wrap=True, hexpand=True, css_classes=["caption"], selectable=True)
        test_box.append(self.test_lbl)
        page.append(test_box)

        clamp = Adw.Clamp(maximum_size=600, child=page, margin_top=12, margin_bottom=24, margin_start=12, margin_end=12)
        scroller = Gtk.ScrolledWindow(child=clamp, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        tv = Adw.ToolbarView(content=scroller)
        tv.add_top_bar(header)
        self.set_child(tv)

        # initial values ---------------------------------------------------------
        self._load_fields(self.conn.info)
        if not self.is_new:
            self.url_row.set_text(build_url(self.conn.info))
        self.stack.set_visible_child_name(self.conn.mode if self.conn.mode in ("url", "fields") else "url")
        self.stack.connect("notify::visible-child-name", self._tab_switched)
        self._url_changed(self.url_row)
        self.set_focus(self.url_row if self.stack.get_visible_child_name() == "url" else self.host_row)

    # field <-> info ---------------------------------------------------------------
    def _load_fields(self, info: ConnInfo):
        self._syncing = True
        self.driver_row.set_selected(DRIVERS.index(info.driver))
        self.host_row.set_text(info.host or "")
        self.port_row.set_text(str(info.port) if info.port else "")
        self.user_row.set_text(info.user or "")
        self.pass_row.set_text(info.password or "")
        self.db_row.set_text(info.database or "")
        self.path_row.set_text(info.path or "")
        self.create_row.set_active(info.params.get("create") == "1")
        self._params = {k: v for k, v in info.params.items() if k != "create"}
        self._syncing = False
        self._driver_changed()

    def _fields_info(self) -> ConnInfo:
        driver = DRIVERS[self.driver_row.get_selected()]
        port = self.port_row.get_text().strip()
        params = dict(self._params)
        if driver == "sqlite" and self.create_row.get_active():
            params["create"] = "1"
        return ConnInfo(
            driver=driver,
            host=self.host_row.get_text().strip() if driver != "sqlite" else "",
            port=int(port) if port.isdigit() else None,
            user=self.user_row.get_text().strip(),
            password=self.pass_row.get_text(),
            database=self.db_row.get_text().strip(),
            path=os.path.expanduser(self.path_row.get_text().strip()) if driver == "sqlite" else "",
            params=params if driver != "sqlite" or self.create_row.get_active() else {},
        )

    def current_info(self) -> ConnInfo:
        if self.stack.get_visible_child_name() == "url":
            return parse_url(self.url_row.get_text())
        return self._fields_info()

    # signals ------------------------------------------------------------------------
    def _color_toggled(self, btn, color):
        if btn.get_active():
            self.conn.color = color

    def _driver_changed(self, *_):
        driver = DRIVERS[self.driver_row.get_selected()]
        self.server_group.set_visible(driver != "sqlite")
        self.file_group.set_visible(driver == "sqlite")
        self.port_row.set_title(f"Port (default {DEFAULT_PORTS[driver]})" if driver in DEFAULT_PORTS else "Port")
        if not self._syncing and driver != "sqlite" and not self.host_row.get_text():
            self.host_row.set_text("localhost")

    def _fields_changed(self, *_):
        self.test_lbl.set_text("")

    def _url_changed(self, row):
        self.test_lbl.set_text("")
        text = row.get_text().strip()
        self.detect_lbl.remove_css_class("error")
        self.detect_lbl.remove_css_class("success")
        if not text:
            self.detect_lbl.set_text("")
            return
        try:
            info = parse_url(text)
        except ParseError as e:
            self.detect_lbl.set_text(str(e))
            self.detect_lbl.add_css_class("error")
            return
        who = f"{info.user}@" if info.user else ""
        self.detect_lbl.set_text(f"✓ {DRIVER_LABELS[info.driver]} · {who}{info.summary()}")
        self.detect_lbl.add_css_class("success")

    def _tab_switched(self, *_):
        # carry what the user typed over to the other tab
        if self.stack.get_visible_child_name() == "fields":
            text = self.url_row.get_text().strip()
            if text:
                try:
                    self._load_fields(parse_url(text))
                except ParseError:
                    pass
        else:
            info = self._fields_info()
            if info.driver == "sqlite" and not info.path:
                return
            self.url_row.set_text(build_url(info))

    def _browse(self, *_):
        dlg = Gtk.FileDialog(title="Pilih file SQLite")
        f = Gtk.FileFilter(name="Database SQLite")
        for pat in ("*.db", "*.sqlite", "*.sqlite3", "*.db3"):
            f.add_pattern(pat)
        anyf = Gtk.FileFilter(name="Semua file")
        anyf.add_pattern("*")
        store = Gio.ListStore(item_type=Gtk.FileFilter)
        store.append(f)
        store.append(anyf)
        dlg.set_filters(store)

        def done(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            if file and file.get_path():
                self.path_row.set_text(file.get_path())

        dlg.open(self.get_root(), None, done)

    def _test(self, *_):
        try:
            info = self.current_info()
        except ParseError as e:
            self._test_result(False, str(e))
            return
        self.test_btn.set_sensitive(False)
        self.test_spinner.set_visible(True)
        self.test_lbl.set_text("")

        def work():
            d = make_driver(info)
            d.connect()
            try:
                return d.server_version()
            finally:
                d.close()

        def done(version):
            self._test_result(True, f"Berhasil terhubung · {version}")

        def fail(e):
            self._test_result(False, str(e))

        run_async(work, done, fail)

    def _test_result(self, ok, text):
        self.test_btn.set_sensitive(True)
        self.test_spinner.set_visible(False)
        self.test_lbl.set_text(("✓ " if ok else "✗ ") + text)
        self.test_lbl.remove_css_class("error" if ok else "success")
        self.test_lbl.add_css_class("success" if ok else "error")

    def _save(self, *_):
        try:
            info = self.current_info()
        except ParseError as e:
            self._test_result(False, str(e))
            return
        if info.driver == "sqlite" and not info.path:
            self._test_result(False, "Pilih file database SQLite dulu")
            return
        self.conn.info = info
        self.conn.mode = self.stack.get_visible_child_name()
        name = self.name_row.get_text().strip()
        if not name:
            name = os.path.basename(info.path) if info.driver == "sqlite" else (info.database or info.host or "database")
        self.conn.name = name
        self.on_save(self.conn)
        self.close()
