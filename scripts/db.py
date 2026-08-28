"""数据库抽象层：统一 SQLite / MySQL 接口，子类只负责连接和 SQL 执行。

表结构（两种数据库一致，EAV 模式）：
- daily{user_id}: date(PRIMARY KEY), usage(REAL), valley_usage(REAL),
  flat_usage(REAL), peak_usage(REAL), tip_usage(REAL)      -- 每日用电量及分时用电量
- data{user_id}:  name(PRIMARY KEY), value(TEXT)            -- 扩展数据（月度/年度/用户信息等）
"""

import logging
import os

# ── SQLite ──
import sqlite3

# ── MySQL ──（可选，仅 DB_TYPE=mysql 时导入）
try:
    import mysql.connector
    _HAS_MYSQL = True
except ImportError:
    _HAS_MYSQL = False


class DB:
    """数据库基类：定义统一数据操作接口。子类只需实现连接/执行/关闭/建表。"""

    # ── 子类需覆写的方言属性 ──
    db_type: str = "base"

    # ── 子类需实现的方法 ──

    def _connect(self):
        raise NotImplementedError

    def _execute(self, sql: str):
        """执行一条写 SQL 并 commit。"""
        raise NotImplementedError

    def _close(self):
        raise NotImplementedError

    def _create_tables(self, user_id: str) -> bool:
        raise NotImplementedError

    def sum_daily_tou_usage(self, month_prefix: str) -> dict:
        """汇总指定月份的每日分时用电量。"""
        raise NotImplementedError

    def upsert_monthly_tou_usage(self, month_prefix: str, tou_data: dict, user_name: str = ""):
        """将每日分时汇总回写到 month_YYYY-MM，保留总电量和总电费。"""
        raise NotImplementedError

    # ── 统一接口 ──

    def connect_user_db(self, user_id: str) -> bool:
        try:
            self._connect()
            self.table_name = f"daily{user_id}"
            self.table_expand_name = f"data{user_id}"
            return self._create_tables(user_id)
        except Exception as e:
            logging.error(f"[{self.db_type}] 连接/建表失败: {e}")
            return False

    def close_connect(self):
        try:
            self._close()
        except Exception:
            pass

    # ── 数据写入方法（子类共用，无需覆写） ──

    def upsert_user(self, user_id: str, username: str, user_name: str):
        self._execute(
            f"INSERT OR REPLACE INTO {self.table_expand_name} VALUES"
            f"('user_info', '{user_id}|{username}|{user_name}')")

    def insert_balance_log(self, data: dict):
        self._execute(
            f"INSERT OR REPLACE INTO {self.table_expand_name} VALUES"
            f"('balance_{data.get('date', 'latest')}', "
            f"'{data.get('balance', 0)}|{data.get('user_name', '')}|"
            f"{data.get('as_of', '')}|{data.get('amount_due', '')}')")

    def insert_daily_data(self, data: dict):
        self._execute(
            f"INSERT OR REPLACE INTO {self.table_name} "
            f"(date, usage, valley_usage, flat_usage, peak_usage, tip_usage) VALUES "
            f"('{data['date']}', {self._number(data.get('total_usage', data.get('usage', 0)))}, "
            f"{self._number(data.get('valley_usage', 0))}, "
            f"{self._number(data.get('flat_usage', 0))}, "
            f"{self._number(data.get('peak_usage', 0))}, "
            f"{self._number(data.get('tip_usage', 0))})")

    @staticmethod
    def _number(value) -> str:
        try:
            number = float(value)
            return f"{number:.2f}" if number == number else "0"
        except (TypeError, ValueError):
            return "0"

    @staticmethod
    def _decimal_text(value, default="0.00") -> str:
        """将写入 EAV 文本值的浮点数规范为两位小数。"""
        try:
            number = float(value)
            return f"{number:.2f}" if number == number else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def _optional_decimal_text(cls, value, default="") -> str:
        if value is None or str(value).strip() == "":
            return default
        return cls._decimal_text(value, default=default or "0.00")

    def insert_monthly_data(self, data: dict):
        month_key = data.get('month') or data.get('date', '')
        self._execute(
            f"INSERT OR REPLACE INTO {self.table_expand_name} VALUES"
            f"('month_{month_key}', "
            f"'{self._decimal_text(data.get('total_usage', 0))}|"
            f"{self._decimal_text(data.get('total_charge', 0))}|"
            f"{self._optional_decimal_text(data.get('valley_usage'))}|"
            f"{self._optional_decimal_text(data.get('flat_usage'))}|"
            f"{self._optional_decimal_text(data.get('peak_usage'))}|"
            f"{self._optional_decimal_text(data.get('tip_usage'))}|"
            f"{data.get('user_name', '')}')")

    def insert_yearly_data(self, data: dict):
        self._execute(
            f"INSERT OR REPLACE INTO {self.table_expand_name} VALUES"
            f"('year_{data.get('year', '')}', "
            f"'{data.get('total_usage', 0)}|{data.get('total_charge', 0)}|"
            f"{data.get('user_name', '')}')")

    def insert_data(self, data: dict):
        """原始每日数据写入（兼容旧调用）"""
        self.insert_daily_data({
            "date": data["date"],
            "total_usage": data.get("usage", 0),
            "valley_usage": data.get("valley_usage", 0),
            "flat_usage": data.get("flat_usage", 0),
            "peak_usage": data.get("peak_usage", 0),
            "tip_usage": data.get("tip_usage", 0),
        })

    def insert_expand_data(self, data: dict):
        """原始扩展数据写入（兼容旧调用）"""
        self._execute(
            f"INSERT OR REPLACE INTO {self.table_expand_name} VALUES"
            f"('{data['name']}', '{data['value']}')")

    def cleanup_old_data(self):
        try:
            days = int(os.getenv("DATA_RETENTION_DAYS", 365))
            self._execute(
                f"DELETE FROM {self.table_name} "
                f"WHERE date < date('now', '-{days} days')")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
# SQLite 实现
# ═══════════════════════════════════════════════════════════

class SqliteDB(DB):
    db_type = "sqlite"

    def _connect(self):
        db_name = os.getenv("DB_NAME", "homeassistant.db")
        if "PYTHON_IN_DOCKER" in os.environ:
            db_name = "/data/" + db_name
        self._conn = sqlite3.connect(db_name)
        logging.info(f"[sqlite] 已连接 {db_name}")

    def _execute(self, sql: str):
        self._conn.execute(sql)
        self._conn.commit()

    def _close(self):
        if getattr(self, "_conn", None):
            self._conn.close()
            self._conn = None
            logging.info("[sqlite] 已关闭")

    def _create_tables(self, user_id: str) -> bool:
        self._conn.execute(f"""CREATE TABLE IF NOT EXISTS {self.table_name} (
            date DATE PRIMARY KEY NOT NULL,
            usage REAL NOT NULL,
            valley_usage REAL NOT NULL DEFAULT 0,
            flat_usage REAL NOT NULL DEFAULT 0,
            peak_usage REAL NOT NULL DEFAULT 0,
            tip_usage REAL NOT NULL DEFAULT 0)""")
        columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({self.table_name})")}
        for column in ("valley_usage", "flat_usage", "peak_usage", "tip_usage"):
            if column not in columns:
                self._conn.execute(
                    f"ALTER TABLE {self.table_name} ADD COLUMN {column} REAL NOT NULL DEFAULT 0"
                )
        logging.info(f"[sqlite] 表 {self.table_name} OK")
        self._conn.execute(f"""CREATE TABLE IF NOT EXISTS {self.table_expand_name} (
            name TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL)""")
        self._conn.commit()
        logging.info(f"[sqlite] 表 {self.table_expand_name} OK")
        return True

    def sum_daily_tou_usage(self, month_prefix: str) -> dict:
        row = self._conn.execute(
            f"""SELECT
                COALESCE(SUM(usage), 0),
                COALESCE(SUM(valley_usage), 0),
                COALESCE(SUM(flat_usage), 0),
                COALESCE(SUM(peak_usage), 0),
                COALESCE(SUM(tip_usage), 0)
            FROM {self.table_name}
            WHERE date LIKE ?""",
            (f"{month_prefix}%",),
        ).fetchone()
        return {
            "total_usage": float(row[0] or 0),
            "valley_usage": float(row[1] or 0),
            "flat_usage": float(row[2] or 0),
            "peak_usage": float(row[3] or 0),
            "tip_usage": float(row[4] or 0),
        }

    def upsert_monthly_tou_usage(self, month_prefix: str, tou_data: dict, user_name: str = ""):
        key = f"month_{month_prefix}"
        row = self._conn.execute(
            f"SELECT value FROM {self.table_expand_name} WHERE name = ?", (key,)
        ).fetchone()
        parts = (row[0] or "").split("|") if row else [str(float(tou_data.get("total_usage", 0) or 0)), "0"]
        parts += [""] * (7 - len(parts))
        parts[0] = self._decimal_text(parts[0])
        parts[1] = self._decimal_text(parts[1])
        for index, field in enumerate(("valley_usage", "flat_usage", "peak_usage", "tip_usage"), start=2):
            parts[index] = self._decimal_text(tou_data.get(field, 0) or 0)
        if user_name:
            parts[6] = user_name
        self._conn.execute(
            f"INSERT OR REPLACE INTO {self.table_expand_name} (name, value) VALUES (?, ?)",
            (key, "|".join(parts[:7])),
        )
        self._conn.commit()


# ═══════════════════════════════════════════════════════════
# MySQL 实现
# ═══════════════════════════════════════════════════════════

class MysqlDB(DB):
    db_type = "mysql"

    def _connect(self):
        if not _HAS_MYSQL:
            raise RuntimeError("mysql-connector-python 未安装")
        self._conn = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST"),
            user=os.getenv("MYSQL_USER"),
            password=os.getenv("MYSQL_PASSWORD"),
            database=os.getenv("MYSQL_DATABASE"),
            port=int(os.getenv("MYSQL_PORT", 3306)),
        )
        if self._conn.is_connected():
            logging.info(f"[mysql] 已连接 {os.getenv('MYSQL_DATABASE')}")
        else:
            raise ConnectionError("MySQL 连接失败")

    def _execute(self, sql: str):
        # REPLACE INTO 语法替换
        sql = sql.replace("INSERT OR REPLACE INTO", "REPLACE INTO")
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            self._conn.commit()
        finally:
            cursor.close()

    def _close(self):
        if getattr(self, "_conn", None) and self._conn.is_connected():
            self._conn.close()
            self._conn = None
            logging.info("[mysql] 已关闭")

    def _create_tables(self, user_id: str) -> bool:
        self._execute(f"""CREATE TABLE IF NOT EXISTS `{self.table_name}` (
            `date` DATE PRIMARY KEY NOT NULL,
            `usage` REAL NOT NULL,
            `valley_usage` REAL NOT NULL DEFAULT 0,
            `flat_usage` REAL NOT NULL DEFAULT 0,
            `peak_usage` REAL NOT NULL DEFAULT 0,
            `tip_usage` REAL NOT NULL DEFAULT 0)""")
        cursor = self._conn.cursor()
        try:
            cursor.execute(f"SHOW COLUMNS FROM `{self.table_name}`")
            columns = {row[0] for row in cursor.fetchall()}
        finally:
            cursor.close()
        for column in ("valley_usage", "flat_usage", "peak_usage", "tip_usage"):
            if column not in columns:
                self._execute(
                    f"ALTER TABLE `{self.table_name}` ADD COLUMN `{column}` REAL NOT NULL DEFAULT 0"
                )
        logging.info(f"[mysql] 表 {self.table_name} OK")
        self._execute(f"""CREATE TABLE IF NOT EXISTS `{self.table_expand_name}` (
            `name` varchar(100) PRIMARY KEY NOT NULL,
            `value` TEXT NOT NULL)""")
        logging.info(f"[mysql] 表 {self.table_expand_name} OK")
        return True

    def sum_daily_tou_usage(self, month_prefix: str) -> dict:
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                f"""SELECT
                    COALESCE(SUM(`usage`), 0),
                    COALESCE(SUM(`valley_usage`), 0),
                    COALESCE(SUM(`flat_usage`), 0),
                    COALESCE(SUM(`peak_usage`), 0),
                    COALESCE(SUM(`tip_usage`), 0)
                FROM `{self.table_name}`
                WHERE `date` LIKE %s""",
                (f"{month_prefix}%",),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        return {
            "total_usage": float(row[0] or 0),
            "valley_usage": float(row[1] or 0),
            "flat_usage": float(row[2] or 0),
            "peak_usage": float(row[3] or 0),
            "tip_usage": float(row[4] or 0),
        }

    def upsert_monthly_tou_usage(self, month_prefix: str, tou_data: dict, user_name: str = ""):
        key = f"month_{month_prefix}"
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                f"SELECT `value` FROM `{self.table_expand_name}` WHERE `name` = %s",
                (key,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        parts = (row[0] or "").split("|") if row else [str(float(tou_data.get("total_usage", 0) or 0)), "0"]
        parts += [""] * (7 - len(parts))
        parts[0] = self._decimal_text(parts[0])
        parts[1] = self._decimal_text(parts[1])
        for index, field in enumerate(("valley_usage", "flat_usage", "peak_usage", "tip_usage"), start=2):
            parts[index] = self._decimal_text(tou_data.get(field, 0) or 0)
        if user_name:
            parts[6] = user_name
        self._execute(
            f"INSERT OR REPLACE INTO `{self.table_expand_name}` (`name`, `value`) VALUES "
            f"('{key}', '{'|'.join(parts[:7])}')"
        )


# ═══════════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════════

def create_db(db_type: str) -> DB:
    """根据配置创建数据库实例。"""
    t = db_type.lower()
    if t == "mysql":
        return MysqlDB()
    return SqliteDB()
