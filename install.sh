#!/usr/bin/env bash
# Databesty installer: installs for the current user into ~/.local
#   ./install.sh            install / update
#   SKIP_DEPS=1 ./install.sh   don't try to install system packages
set -euo pipefail

APP_ID=io.github.yandyrohman.Databesty
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
APP_DIR="$PREFIX/share/databesty"
BIN="$PREFIX/bin/databesty"

check_deps() {
  python3 - <<'PY'
import gi, sys
missing = []
for ns, ver in (("Gtk", "4.0"), ("Adw", "1"), ("GtkSource", "5")):
    try:
        gi.require_version(ns, ver)
        __import__("gi.repository." + ns)
    except Exception:
        missing.append(ns)
for mod in ("psycopg2", "pymysql"):
    try:
        __import__(mod)
    except ImportError:
        missing.append(mod)
if "Adw" not in missing:
    from gi.repository import Adw
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 7):
        missing.append("Adw>=1.7")
print(" ".join(missing))
sys.exit(1 if missing else 0)
PY
}

if [[ "${SKIP_DEPS:-0}" != 1 ]] && ! missing=$(check_deps 2>/dev/null); then
  echo "Dependensi belum lengkap: ${missing:-python3-gi}"
  if command -v apt-get >/dev/null; then
    sudo apt-get install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 python3-psycopg2 python3-pymysql
  elif command -v dnf >/dev/null; then
    sudo dnf install -y python3-gobject gtk4 libadwaita gtksourceview5 python3-psycopg2 python3-pymysql
  elif command -v pacman >/dev/null; then
    sudo pacman -S --needed python-gobject gtk4 libadwaita gtksourceview5 python-psycopg2 python-pymysql
  else
    echo "Install manual: PyGObject, GTK 4, libadwaita >= 1.7, GtkSourceView 5, psycopg2, PyMySQL" >&2
    exit 1
  fi
  check_deps >/dev/null || { echo "Masih ada yang kurang: $(check_deps)" >&2; exit 1; }
fi

echo "Menginstall ke $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR" "$PREFIX/bin" "$PREFIX/share/applications" "$PREFIX/share/icons/hicolor/scalable/apps"
cp -r "$SRC/databesty" "$SRC/data" "$APP_DIR/"
find "$APP_DIR" -name __pycache__ -prune -exec rm -rf {} +

cat > "$BIN" <<LAUNCH
#!/usr/bin/env bash
cd "\$HOME"
exec python3 -c 'import sys; sys.path.insert(0, "$APP_DIR"); from databesty.app import main; sys.exit(main())' "\$@"
LAUNCH
chmod +x "$BIN"

cp "$SRC/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$PREFIX/share/icons/hicolor/scalable/apps/"
sed "s|@EXEC@|$BIN|" "$SRC/data/$APP_ID.desktop.in" > "$PREFIX/share/applications/$APP_ID.desktop"

gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true

echo "Selesai. Buka \"Databesty\" dari menu aplikasi, atau jalankan: $BIN"
