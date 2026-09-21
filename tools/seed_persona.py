"""Insert the whale-girl persona into AstrBot's SQLite database and select it.

Usage:  python seed_persona.py <data_v4.db> <persona_name> <persona.md> <cmd_config.json>

AstrBot keeps personas in the `personas` table (see astrbot/core/db/po.py), not
in a JSON file, so the row is written directly with sqlite3. The table layout is
read back with PRAGMA first and any NOT NULL column without a default is filled
in, which keeps this working if the schema gains columns in a later version.

Re-running updates the existing persona instead of creating duplicates.

Only ASCII goes to stdout -- see gen_plugin_config.py for why.
"""

import datetime
import json
import os
import sqlite3
import sys

PERSONA_ID = None  # set from argv


def now_stamp():
    """Format a timestamp the way SQLAlchemy's DateTime column expects."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def main() -> int:
    if len(sys.argv) < 5:
        print("usage: seed_persona.py <db> <name> <persona.md> <cmd_config.json>")
        return 2

    db_path, name, persona_path, conf_path = sys.argv[1:5]

    if not os.path.exists(db_path):
        print("database not found: %s" % db_path)
        return 3
    with open(persona_path, encoding="utf-8") as f:
        prompt = f.read().strip()
    if not prompt:
        print("persona file is empty")
        return 4

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        info = list(cur.execute("PRAGMA table_info(personas)"))
        if not info:
            print("table 'personas' does not exist yet - start AstrBot once first")
            return 5

        columns = {row[1]: {"notnull": row[3], "default": row[4], "pk": row[5]} for row in info}

        row = {
            "persona_id": name,
            "system_prompt": prompt,
            "begin_dialogs": "[]",
            "tools": None,
            "skills": None,
            "folder_id": None,
            "sort_order": 0,
        }

        # Fill anything else the schema insists on.
        for col, meta in columns.items():
            if col in row or meta["pk"]:
                continue
            if meta["notnull"] and meta["default"] is None:
                row[col] = now_stamp() if ("_at" in col or "time" in col) else ""

        unknown = [c for c in row if c not in columns]
        for c in unknown:
            row.pop(c)

        cols = list(row.keys())
        placeholders = ", ".join("?" for _ in cols)
        sql = "INSERT OR REPLACE INTO personas (%s) VALUES (%s)" % (
            ", ".join(cols),
            placeholders,
        )
        cur.execute(sql, [row[c] for c in cols])
        conn.commit()
        print("persona '%s' written (%d chars, %d columns)" % (name, len(prompt), len(cols)))
    finally:
        conn.close()

    # --- make it the default -------------------------------------------------
    if os.path.exists(conf_path):
        with open(conf_path, encoding="utf-8-sig") as f:
            text = f.read()
        conf = json.loads(text) if text.strip() else {}
        runner = conf.get("agent_runner")
        if not isinstance(runner, dict):
            runner = {"runner_type": "local", "config": {}}
        cfg = runner.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
        persona = cfg.get("persona")
        if not isinstance(persona, dict):
            persona = {}
        persona["persona_id"] = name
        cfg["persona"] = persona
        runner["config"] = cfg
        conf["agent_runner"] = runner

        tmp = conf_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=4)
        os.replace(tmp, conf_path)
        print("default persona -> %s" % name)
    else:
        print("warning: %s not found, default persona not set" % conf_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
