"""Generate a plugin config file from its _conf_schema.json defaults.

Usage:  python gen_plugin_config.py <schema.json> <out.json> [overrides]

`overrides` is either a path to a JSON file or an inline JSON object. Prefer
the file: Windows PowerShell 5.1 strips the double quotes out of inline JSON
when handing the argument to a native program, turning {"admin_qq":"123"}
into {admin_qq:123} and breaking json.loads.

The overrides are merged on top of the schema defaults, so this script never
has to know about individual plugin options -- when a plugin grows a new
setting its default is picked up automatically.

Output is ASCII only on purpose: Python writes redirected stdout in the system
locale encoding (cp936 on a Chinese Windows), while the calling PowerShell
script prints its own Chinese messages. Keeping this side ASCII avoids a whole
class of mojibake bugs.
"""

import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: gen_plugin_config.py <schema> <out> [overrides-file|json]")
        return 2

    schema_path, out_path = sys.argv[1], sys.argv[2]
    overrides_raw = sys.argv[3] if len(sys.argv) > 3 else ""

    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)

    cfg = {}
    for key, spec in schema.items():
        if isinstance(spec, dict) and "default" in spec:
            cfg[key] = spec["default"]

    if overrides_raw.strip():
        if os.path.isfile(overrides_raw):
            with open(overrides_raw, encoding="utf-8-sig") as f:
                text = f.read()
        else:
            text = overrides_raw
        if text.strip():
            cfg.update(json.loads(text))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print("wrote %d keys -> %s" % (len(cfg), out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
