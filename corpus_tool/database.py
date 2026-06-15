"""数据库管理"""
import sqlite3
import os
from pathlib import Path


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "corpus.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS corpus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_text TEXT NOT NULL,
            desensitized_text TEXT,
            source_file TEXT,
            status TEXT DEFAULT 'imported',
            rule_version INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            is_sampled INTEGER DEFAULT 0,
            sample_batch TEXT,
            final_conclusion TEXT,
            metadata TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS desensitization_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            pattern TEXT NOT NULL,
            replacement TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            created_at TEXT,
            description TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS review_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus_id INTEGER NOT NULL,
            reviewer TEXT NOT NULL,
            conclusion TEXT NOT NULL,
            comment TEXT,
            created_at TEXT,
            rule_version_at_review INTEGER,
            FOREIGN KEY (corpus_id) REFERENCES corpus(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT NOT NULL,
            operator TEXT,
            details TEXT,
            rule_version INTEGER,
            created_at TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conflict_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus_id INTEGER NOT NULL,
            reviewer1 TEXT NOT NULL,
            reviewer2 TEXT NOT NULL,
            conclusion1 TEXT NOT NULL,
            conclusion2 TEXT NOT NULL,
            resolved INTEGER DEFAULT 0,
            created_at TEXT,
            FOREIGN KEY (corpus_id) REFERENCES corpus(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER NOT NULL,
            description TEXT,
            created_at TEXT,
            is_active INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sample_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_name TEXT NOT NULL,
            sample_count INTEGER,
            created_at TEXT,
            rule_version INTEGER
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS export_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_name TEXT NOT NULL DEFAULT 'default',
            config_json TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(config_name)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            rule_version INTEGER DEFAULT 0,
            export_config_name TEXT,
            corpus_count INTEGER DEFAULT 0,
            review_count INTEGER DEFAULT 0,
            conflict_count INTEGER DEFAULT 0,
            created_at TEXT,
            created_by TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS snapshot_data (
            snapshot_id INTEGER PRIMARY KEY,
            corpus_json TEXT,
            review_records_json TEXT,
            conflict_records_json TEXT,
            export_config_json TEXT,
            sample_batches_json TEXT,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )
    ''')

    conn.commit()
    conn.close()


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def ensure_default_rules():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM desensitization_rules")
    count = cursor.fetchone()[0]
    if count == 0:
        default_rules = [
            ("手机号脱敏", "phone", r"1[3-9]\d{9}", "***-****-****", "匹配中国大陆手机号"),
            ("身份证号脱敏", "id_card", r"\d{17}[\dXx]|\d{15}", "**********", "匹配15或18位身份证号"),
            ("固定电话脱敏", "landline", r"\d{3,4}-\d{7,8}", "****-****", "匹配固定电话号码"),
            ("邮箱地址脱敏", "email", r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "***@***.com", "匹配邮箱地址"),
            ("银行卡号脱敏", "bank_card", r"\d{16,19}", "**** **** **** ****", "匹配16-19位银行卡号"),
            ("地址脱敏", "address", r"(北京市|上海市|广州市|深圳市|杭州市|南京市|成都市|武汉市|西安市|重庆市|天津市|苏州市|郑州市|长沙市|沈阳市|青岛市|宁波市|东莞市|无锡市|合肥市|福州市|厦门市|济南市|哈尔滨市|长春市|石家庄市|太原市|南昌市|南宁市|昆明市|贵阳市|兰州市|乌鲁木齐市|呼和浩特市|拉萨市|西宁市|银川市|海口市)([市区][^，。！？；\n]{2,20}|[^，。！？；\n]{2,30})", "**市**区", "匹配常见城市地址片段"),
            ("姓名脱敏", "name", r"(先生|女士|小姐|同志|老师)\s*[:：]\s*[\u4e00-\u9fa5]{2,4}", "***:", "匹配带称谓的姓名"),
        ]
        for name, category, pattern, replacement, desc in default_rules:
            cursor.execute('''
                INSERT INTO desensitization_rules 
                (name, category, pattern, replacement, version, created_at, description)
                VALUES (?, ?, ?, ?, 1, datetime('now'), ?)
            ''', (name, category, pattern, replacement, desc))
        cursor.execute('''
            INSERT INTO rule_versions (version, description, created_at, is_active)
            VALUES (1, '初始默认规则集', datetime('now'), 1)
        ''')
        conn.commit()
    else:
        cursor.execute("SELECT id, pattern FROM desensitization_rules WHERE category = 'address'")
        address_rules = cursor.fetchall()
        fixed_pattern = r"(北京市|上海市|广州市|深圳市|杭州市|南京市|成都市|武汉市|西安市|重庆市|天津市|苏州市|郑州市|长沙市|沈阳市|青岛市|宁波市|东莞市|无锡市|合肥市|福州市|厦门市|济南市|哈尔滨市|长春市|石家庄市|太原市|南昌市|南宁市|昆明市|贵阳市|兰州市|乌鲁木齐市|呼和浩特市|拉萨市|西宁市|银川市|海口市)([市区][^，。！？；\n]{2,20}|[^，。！？；\n]{2,30})"
        need_update = False
        for rule_id, current_pattern in address_rules:
            if '[市区]' in current_pattern and '|[' not in current_pattern:
                need_update = True
                cursor.execute('''
                    UPDATE desensitization_rules 
                    SET pattern = ?
                    WHERE id = ?
                ''', (fixed_pattern, rule_id))
        if need_update:
            conn.commit()
    conn.close()
