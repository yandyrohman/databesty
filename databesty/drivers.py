"""Database access for PostgreSQL, MySQL/MariaDB and SQLite.

Every driver exposes the same small surface so the UI never branches on the
engine. All methods block; the UI calls them from worker threads.
"""

import os
import sqlite3
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

DRIVERS = ("postgresql", "mysql", "sqlite")
DRIVER_LABELS = {"postgresql": "PostgreSQL", "mysql": "MySQL / MariaDB", "sqlite": "SQLite"}
DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}

_SCHEME_ALIASES = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "pgsql": "postgresql",
    "psql": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "file": "sqlite",
}


class ParseError(ValueError):
    pass


@dataclass
class ConnInfo:
    driver: str = "postgresql"
    host: str = "localhost"
    port: int | None = None
    user: str = ""
    password: str = ""
    database: str = ""
    path: str = ""  # sqlite only
    params: dict = field(default_factory=dict)

    def effective_port(self):
        return self.port or DEFAULT_PORTS.get(self.driver)

    def summary(self):
        if self.driver == "sqlite":
            return self.path or "(no file)"
        where = f"{self.host or 'localhost'}:{self.effective_port()}"
        return f"{where}/{self.database}" if self.database else where


def parse_url(url: str) -> ConnInfo:
    """Parse the kind of connection string usually found in .env files.

    Accepts postgres://, postgresql://, mysql://, mariadb://, sqlite:///path,
    SQLAlchemy-style schemes (postgresql+psycopg2://), a jdbc: prefix, and a bare
    path to a .db/.sqlite file.
    """
    url = url.strip().strip("'\"")
    if not url:
        raise ParseError("Connection string is empty")
    if "=" in url.split("://", 1)[0]:
        # KEY=value line pasted straight from a .env file
        url = url.split("=", 1)[1].strip().strip("'\"")
    if url.lower().startswith("jdbc:"):
        url = url[5:]

    if "://" not in url:
        if url.endswith((".db", ".sqlite", ".sqlite3", ".db3")) or url.startswith(("/", "~", "./")):
            return ConnInfo(driver="sqlite", host="", path=os.path.expanduser(url))
        raise ParseError("Not a connection string (expected scheme://…)")

    scheme = url.split("://", 1)[0].lower().split("+", 1)[0]
    driver = _SCHEME_ALIASES.get(scheme)
    if not driver:
        raise ParseError(f"Unsupported database type: {scheme}")

    if driver == "sqlite":
        rest = url.split("://", 1)[1]
        rest = rest.split("?", 1)[0]
        # sqlite:///relative.db -> "/relative.db"? SQLAlchemy treats 3 slashes as
        # relative and 4 as absolute; most .env files use 3 slashes + absolute path.
        if rest.startswith("//"):
            rest = rest[1:]
        return ConnInfo(driver="sqlite", host="", path=os.path.expanduser(unquote(rest)))

    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as e:
        raise ParseError(f"Invalid port: {e}") from None
    return ConnInfo(
        driver=driver,
        host=unquote(parts.hostname or "localhost"),
        port=port,
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        database=unquote(parts.path.lstrip("/")),
        params=dict(parse_qsl(parts.query)),
    )


def build_url(info: ConnInfo, with_password=True) -> str:
    if info.driver == "sqlite":
        return f"sqlite:///{info.path.lstrip('/')}" if info.path.startswith("/") else f"sqlite:///{info.path}"
    scheme = "postgresql" if info.driver == "postgresql" else "mysql"
    auth = ""
    if info.user:
        auth = quote(info.user, safe="")
        if info.password and with_password:
            auth += ":" + quote(info.password, safe="")
        auth += "@"
    host = info.host or "localhost"
    port = f":{info.port}" if info.port else ""
    db = "/" + quote(info.database, safe="") if info.database else ""
    query = "?" + urlencode(info.params) if info.params else ""
    return f"{scheme}://{auth}{host}{port}{db}{query}"


# --------------------------------------------------------------------------- results


@dataclass
class Column:
    name: str
    type: str = ""
    nullable: bool = True
    default: str | None = None
    primary_key: bool = False
    auto: bool = False  # serial / auto_increment / identity


@dataclass
class TableRef:
    schema: str
    name: str
    kind: str = "table"  # table | view


@dataclass
class QueryResult:
    columns: list
    rows: list
    rowcount: int = -1
    message: str = ""


class DbError(Exception):
    pass


def _wrap_errors(fn):
    def inner(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except DbError:
            raise
        except Exception as e:  # noqa: BLE001 - every driver has its own hierarchy
            self._after_error()
            msg = str(e).strip() or e.__class__.__name__
            raise DbError(msg) from e

    inner.__name__ = fn.__name__
    return inner


class Driver:
    """Base class. Subclasses fill in the dialect bits."""

    paramstyle = "%s"
    supports_schemas = True

    def __init__(self, info: ConnInfo):
        self.info = info
        self.conn = None
        self.lock = threading.RLock()

    # identifiers --------------------------------------------------------------
    def q(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def qtable(self, t: TableRef) -> str:
        return f"{self.q(t.schema)}.{self.q(t.name)}" if t.schema else self.q(t.name)

    # lifecycle ----------------------------------------------------------------
    def _after_error(self):
        pass

    def close(self):
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self.conn = None

    # generic helpers ----------------------------------------------------------
    def _fetch(self, sql, params=()):
        with self.lock:
            cur = self.conn.cursor()
            try:
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                return cur.fetchall()
            finally:
                cur.close()
                # psycopg2 and PyMySQL run with autocommit off, so a plain SELECT opens
                # a transaction. Left open it sits "idle in transaction" holding read
                # locks, which blocks other clients' ALTER TABLE / migrations forever.
                # Reads change nothing, so ending the transaction here is safe.
                self._end_read()

    def _end_read(self):
        try:
            self.conn.rollback()
        except Exception:  # noqa: BLE001 - a dead connection surfaces on the next call
            pass

    def _write(self, sql, params=()):
        with self.lock:
            cur = self.conn.cursor()
            try:
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                n = cur.rowcount
                self.conn.commit()
                return n
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()

    # row identity: how to address a row for UPDATE/DELETE ----------------------
    def row_locator(self, table, columns):
        """Return (extra_select_sql, key) where key says how rows are addressed.

        key = ("pk", [col names]) | ("hidden", expr) | ("all", [col names])
        """
        pks = [c.name for c in columns if c.primary_key]
        if pks:
            return None, ("pk", pks)
        return None, ("all", [c.name for c in columns])

    def _where_for(self, key, row_values, columns, hidden_value):
        kind, spec = key
        names = [c.name for c in columns]
        clauses, params = [], []
        if kind == "hidden":
            clauses.append(f"{spec} = {self.paramstyle}")
            params.append(hidden_value)
        else:
            for col in spec:
                v = row_values[names.index(col)]
                if v is None:
                    clauses.append(f"{self.q(col)} IS NULL")
                else:
                    clauses.append(f"{self.q(col)} = {self.paramstyle}")
                    params.append(v)
        return " AND ".join(clauses) or "1=1", params

    def _limit_one_suffix(self, key):
        return ""

    # data ---------------------------------------------------------------------
    @_wrap_errors
    def select_page(self, table, columns, limit, offset, where="", order_by=None, desc=False):
        hidden_sql, key = self.row_locator(table, columns)
        cols = ", ".join(self.q(c.name) for c in columns) or "*"
        if hidden_sql:
            cols = f"{hidden_sql}, {cols}"
        sql = f"SELECT {cols} FROM {self.qtable(table)}"
        if where.strip():
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {self.q(order_by)} {'DESC' if desc else 'ASC'}"
        elif key[0] == "pk":
            # stable paging: without ORDER BY, edited rows jump around
            sql += " ORDER BY " + ", ".join(self.q(c) for c in key[1])
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        rows = self._fetch(sql)
        if hidden_sql:
            return [(r[0], list(r[1:])) for r in rows], key
        return [(None, list(r)) for r in rows], key

    @_wrap_errors
    def count(self, table, where=""):
        sql = f"SELECT COUNT(*) FROM {self.qtable(table)}"
        if where.strip():
            sql += f" WHERE {where}"
        return self._fetch(sql)[0][0]

    @_wrap_errors
    def insert(self, table, values: dict):
        if values:
            cols = ", ".join(self.q(k) for k in values)
            ph = ", ".join([self.paramstyle] * len(values))
            sql = f"INSERT INTO {self.qtable(table)} ({cols}) VALUES ({ph})"
        else:
            sql = self._insert_defaults_sql(table)
        return self._write(sql, list(values.values()))

    def _insert_defaults_sql(self, table):
        return f"INSERT INTO {self.qtable(table)} DEFAULT VALUES"

    @_wrap_errors
    def update(self, table, columns, key, row_values, hidden_value, changes: dict):
        if not changes:
            return 0
        sets = ", ".join(f"{self.q(k)} = {self.paramstyle}" for k in changes)
        where, wparams = self._where_for(key, row_values, columns, hidden_value)
        sql = f"UPDATE {self.qtable(table)} SET {sets} WHERE {where}{self._limit_one_suffix(key)}"
        return self._write(sql, list(changes.values()) + wparams)

    @_wrap_errors
    def delete_rows(self, table, columns, key, rows):
        """rows = [(hidden_value, values), ...]; deleted in one transaction."""
        total = 0
        with self.lock:
            cur = self.conn.cursor()
            try:
                for hidden, values in rows:
                    where, params = self._where_for(key, values, columns, hidden)
                    cur.execute(
                        f"DELETE FROM {self.qtable(table)} WHERE {where}{self._limit_one_suffix(key)}",
                        params,
                    )
                    total += max(cur.rowcount, 0)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()
        return total

    @_wrap_errors
    def execute(self, sql: str) -> list:
        """Run user SQL from the console. Returns a list of QueryResult."""
        results = []
        with self.lock:
            cur = self.conn.cursor()
            try:
                cur.execute(sql)
                results.append(self._result_from_cursor(cur))
                while self._next_result(cur):
                    results.append(self._result_from_cursor(cur))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()
        return results

    def _next_result(self, cur):
        return False

    def _result_from_cursor(self, cur):
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(5000)
            more = bool(cur.fetchmany(1))
            msg = f"{len(rows)} rows" + (" (showing first 5000)" if more else "")
            return QueryResult(cols, [list(r) for r in rows], len(rows), msg)
        n = cur.rowcount
        return QueryResult([], [], n, f"OK, {n} rows affected" if n >= 0 else "OK")


# --------------------------------------------------------------------------- PostgreSQL


class PostgresDriver(Driver):
    def connect(self):
        try:
            import psycopg2
        except ImportError:
            raise DbError("psycopg2 is not installed (sudo apt install python3-psycopg2)") from None
        i = self.info
        kw = dict(
            host=i.host or "localhost",
            port=i.effective_port(),
            user=i.user or None,
            password=i.password or None,
            dbname=i.database or "postgres",
            connect_timeout=8,
            application_name="Databesty",
        )
        kw.update(i.params)
        try:
            self.conn = psycopg2.connect(**{k: v for k, v in kw.items() if v is not None})
        except Exception as e:  # noqa: BLE001
            raise DbError(str(e).strip()) from e

    def _after_error(self):
        if self.conn is not None and not self.conn.closed:
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    @_wrap_errors
    def server_version(self):
        return self._fetch("SHOW server_version")[0][0]

    @_wrap_errors
    def schemas(self):
        rows = self._fetch(
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname NOT IN ('pg_catalog','information_schema','pg_toast') "
            "AND nspname NOT LIKE 'pg_temp_%' AND nspname NOT LIKE 'pg_toast_temp_%' "
            "ORDER BY nspname <> 'public', nspname"
        )
        return [r[0] for r in rows]

    def default_schema(self, schemas):
        return "public" if "public" in schemas else (schemas[0] if schemas else "")

    @_wrap_errors
    def tables(self, schema):
        rows = self._fetch(
            "SELECT c.relname, CASE WHEN c.relkind IN ('v','m') THEN 'view' ELSE 'table' END "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','f') "
            "ORDER BY c.relname",
            (schema,),
        )
        return [TableRef(schema, r[0], r[1]) for r in rows]

    @_wrap_errors
    def columns(self, table):
        rows = self._fetch(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid), "
            "COALESCE(a.attnum = ANY(i.indkey), false), a.attidentity <> '' "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary "
            "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum",
            (table.schema, table.name),
        )
        out = []
        for name, typ, nullable, default, pk, identity in rows:
            auto = bool(identity) or (default or "").startswith("nextval(")
            out.append(Column(name, typ, nullable, default, bool(pk), auto))
        return out

    def row_locator(self, table, columns):
        pks = [c.name for c in columns if c.primary_key]
        if pks:
            return None, ("pk", pks)
        if table.kind == "table":
            return "ctid::text", ("hidden", "ctid")
        return None, ("all", [c.name for c in columns])

    def _where_for(self, key, row_values, columns, hidden_value):
        if key[0] == "hidden":
            return "ctid = %s::tid", [hidden_value]
        return super()._where_for(key, row_values, columns, hidden_value)

    def _result_from_cursor(self, cur):
        r = super()._result_from_cursor(cur)
        if not r.columns and cur.statusmessage:
            r.message = cur.statusmessage
        return r


# --------------------------------------------------------------------------- MySQL


class MysqlDriver(Driver):
    def q(self, ident):
        return "`" + ident.replace("`", "``") + "`"

    def connect(self):
        try:
            import pymysql
        except ImportError:
            raise DbError("PyMySQL is not installed (sudo apt install python3-pymysql)") from None
        i = self.info
        kw = dict(
            host=i.host or "localhost",
            port=int(i.effective_port()),
            user=i.user or "root",
            password=i.password or "",
            database=i.database or None,
            connect_timeout=8,
            charset=i.params.get("charset", "utf8mb4"),
            client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS,
            autocommit=False,
        )
        if i.params.get("ssl") in ("true", "1", "required") or i.params.get("ssl-mode", "").upper() == "REQUIRED":
            kw["ssl"] = {"check_hostname": False}
        try:
            self.conn = pymysql.connect(**kw)
        except Exception as e:  # noqa: BLE001
            raise DbError(_mysql_msg(e)) from e

    def _after_error(self):
        if self.conn is not None:
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    def _fetch(self, sql, params=()):
        with self.lock:
            self.conn.ping(reconnect=True)
            return super()._fetch(sql, params or None)

    def _write(self, sql, params=()):
        with self.lock:
            self.conn.ping(reconnect=True)
            return super()._write(sql, params or None)

    @_wrap_errors
    def server_version(self):
        return self._fetch("SELECT VERSION()")[0][0]

    @_wrap_errors
    def schemas(self):
        if self.info.database:
            return [self.info.database]
        rows = self._fetch(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN "
            "('information_schema','mysql','performance_schema','sys') ORDER BY schema_name"
        )
        return [r[0] for r in rows]

    def default_schema(self, schemas):
        return self.info.database or (schemas[0] if schemas else "")

    @_wrap_errors
    def tables(self, schema):
        rows = self._fetch(
            "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = %s "
            "ORDER BY table_name",
            (schema,),
        )
        return [TableRef(schema, r[0], "view" if "VIEW" in r[1] else "table") for r in rows]

    @_wrap_errors
    def columns(self, table):
        rows = self._fetch(
            "SELECT column_name, column_type, is_nullable, column_default, column_key, extra "
            "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (table.schema, table.name),
        )
        return [
            Column(n, t, nl == "YES", d, k == "PRI", "auto_increment" in (x or ""))
            for n, t, nl, d, k, x in rows
        ]

    def _limit_one_suffix(self, key):
        return " LIMIT 1" if key[0] == "all" else ""

    def _insert_defaults_sql(self, table):
        return f"INSERT INTO {self.qtable(table)} () VALUES ()"

    def execute(self, sql):
        with self.lock:
            if self.conn is not None:
                self.conn.ping(reconnect=True)
        return super().execute(sql)

    def _next_result(self, cur):
        return cur.nextset()


def _mysql_msg(e):
    if getattr(e, "args", None) and len(e.args) == 2 and isinstance(e.args[0], int):
        return f"({e.args[0]}) {e.args[1]}"
    return str(e)


# --------------------------------------------------------------------------- SQLite


class SqliteDriver(Driver):
    paramstyle = "?"
    supports_schemas = False

    def connect(self):
        path = os.path.expanduser(self.info.path)
        if not path:
            raise DbError("No database file selected")
        if path != ":memory:" and not os.path.exists(path) and self.info.params.get("create") != "1":
            raise DbError(f"File not found: {path}")
        try:
            self.conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
        except Exception as e:  # noqa: BLE001
            raise DbError(str(e)) from e

    def qtable(self, t):
        return self.q(t.name)

    @_wrap_errors
    def server_version(self):
        return "SQLite " + sqlite3.sqlite_version

    def schemas(self):
        return ["main"]

    def default_schema(self, schemas):
        return "main"

    @_wrap_errors
    def tables(self, schema):
        rows = self._fetch(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [TableRef("", r[0], r[1]) for r in rows]

    @_wrap_errors
    def columns(self, table):
        rows = self._fetch(f"PRAGMA table_info({self.q(table.name)})")
        pk_cols = [r for r in rows if r[5]]
        out = []
        for _cid, name, typ, notnull, default, pk in rows:
            auto = bool(pk) and len(pk_cols) == 1 and typ.upper() == "INTEGER"
            out.append(Column(name, typ, not notnull, default, bool(pk), auto))
        return out

    def row_locator(self, table, columns):
        pks = [c.name for c in columns if c.primary_key]
        if pks:
            return None, ("pk", pks)
        sql = self._fetch("SELECT sql FROM sqlite_master WHERE name = ?", (table.name,))
        ddl = (sql[0][0] or "").upper() if sql else ""
        if table.kind == "table" and "WITHOUT ROWID" not in ddl:
            return "rowid", ("hidden", "rowid")
        return None, ("all", [c.name for c in columns])

    def execute(self, sql):
        # sqlite3's execute() refuses multiple statements; split them ourselves.
        results = []
        with self.lock:
            try:
                cur = self.conn.cursor()
                for stmt in _split_sqlite(sql):
                    cur.execute(stmt)
                    results.append(self._result_from_cursor(cur))
                self.conn.commit()
            except Exception as e:  # noqa: BLE001
                self.conn.rollback()
                raise DbError(str(e)) from e
        return results


def _split_sqlite(sql):
    buf, out = "", []
    for ch in sql:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            if buf.strip(" \t\r\n;"):
                out.append(buf.strip())
            buf = ""
    if buf.strip(" \t\r\n;"):
        out.append(buf.strip())
    return out


def make_driver(info: ConnInfo) -> Driver:
    return {"postgresql": PostgresDriver, "mysql": MysqlDriver, "sqlite": SqliteDriver}[info.driver](info)
