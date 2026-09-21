"""Point NapCat at AstrBot by adding a reverse-WebSocket client entry.

Usage:  python patch_napcat.py <onebot11_<qq>.json> <port> <token>

NapCat creates onebot11_<QQ号>.json after the first successful QQ login, which
is why this cannot run during install. Re-running is safe: an existing entry
named "astrbot" is updated in place instead of being duplicated.

Only ASCII goes to stdout -- see gen_plugin_config.py for why.
"""

import json
import os
import sys

CLIENT_NAME = "astrbot"


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: patch_napcat.py <onebot11.json> <port> <token>")
        return 2

    path, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]

    conf = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        if text.strip():
            try:
                conf = json.loads(text)
            except json.JSONDecodeError as exc:
                print("existing file is not valid JSON: %s" % exc)
                return 3

    if not isinstance(conf.get("network"), dict):
        conf["network"] = {}
    network = conf["network"]
    if not isinstance(network.get("websocketClients"), list):
        network["websocketClients"] = []
    clients = network["websocketClients"]

    want = {
        "name": CLIENT_NAME,
        "enable": True,
        "url": "ws://127.0.0.1:%d/ws" % port,
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": token,
        "debug": False,
        "heartInterval": 30000,
        "reconnectInterval": 3000,
    }

    replaced = False
    for i, c in enumerate(clients):
        if isinstance(c, dict) and c.get("name") == CLIENT_NAME:
            want.update({k: v for k, v in c.items() if k not in want})
            clients[i] = want
            replaced = True
            break
    if not replaced:
        clients.append(want)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

    print("%s entry -> ws://127.0.0.1:%d/ws" % ("updated" if replaced else "added", port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
