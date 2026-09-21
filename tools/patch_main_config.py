"""Patch AstrBot's data/cmd_config.json for the 鲸鱼娘聊天AI Windows package.

Usage:  python patch_main_config.py <cmd_config.json> <overrides.json> <token_out>

overrides.json holds {"api_key": "...", "admin_qq": "..."}. It is a file rather
than command-line arguments on purpose: Windows PowerShell 5.1 silently drops
empty-string arguments when calling a native program, so a blank API key would
shift every later argument by one position.

What it sets (everything else in the file is left untouched):

  platform        lists  -> one OneBot v11 (aiocqhttp) entry on 127.0.0.1:6199
  provider_sources       -> a DeepSeek source holding the API key
  provider               -> one chat model entry: deepseek-flash
  provider_settings      -> streaming_response = False
  platform_settings      -> segmented_reply.enable = False
  timezone               -> Asia/Shanghai

Why streaming must be off: the meme plugin rewrites the model's answer to
turn [表情:tag] markers into real stickers. With streaming on, the marker
leaks into the chat before the plugin can catch it.

AstrBot merges a partial cmd_config.json with its built-in defaults on load,
so writing only these keys is safe.

Only ASCII goes to stdout -- see gen_plugin_config.py for why.
"""

import json
import os
import secrets
import sys

QQ_PORT = 6199


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    return json.loads(text) if text.strip() else {}


def save(path, conf):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: patch_main_config.py <cmd_config.json> <overrides.json> [token_out]")
        return 2

    conf_path, overrides_path = sys.argv[1], sys.argv[2]
    token_path = sys.argv[3] if len(sys.argv) > 3 else ""

    api_key, admin_qq = "", ""
    if os.path.isfile(overrides_path):
        with open(overrides_path, encoding="utf-8-sig") as f:
            over = json.load(f)
        api_key = over.get("api_key") or ""
        admin_qq = over.get("admin_qq") or ""
    conf = load(conf_path)
    changed = []

    # --- OneBot v11 platform -------------------------------------------------
    platforms = conf.get("platform")
    if not isinstance(platforms, list):
        platforms = []
    entry = None
    for item in platforms:
        if isinstance(item, dict) and item.get("type") == "aiocqhttp":
            entry = item
            break
    if entry is None:
        entry = {
            "id": "qq",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": QQ_PORT,
            "ws_reverse_token": "",
        }
        platforms.append(entry)
        changed.append("added OneBot v11 platform")
    else:
        changed.append("OneBot v11 platform already present")

    # A token keeps anything else on the machine from driving the bot.
    if not entry.get("ws_reverse_token"):
        entry["ws_reverse_token"] = secrets.token_hex(16)
        changed.append("generated OneBot token")
    entry["enable"] = True
    conf["platform"] = platforms

    # --- DeepSeek provider ---------------------------------------------------
    sources = conf.get("provider_sources")
    if not isinstance(sources, list):
        sources = []
    source = None
    for item in sources:
        if isinstance(item, dict) and item.get("id") == "deepseek":
            source = item
            break
    if source is None:
        source = {
            "id": "deepseek",
            "provider": "deepseek",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": [],
            "api_base": "https://api.deepseek.com/v1",
            "timeout": 120,
            "proxy": "",
            "custom_headers": {},
        }
        sources.append(source)
        changed.append("added DeepSeek source")
    if api_key:
        source["key"] = [api_key]
        changed.append("set DeepSeek API key")
    conf["provider_sources"] = sources

    models = conf.get("provider")
    if not isinstance(models, list):
        models = []
    have_model = any(
        isinstance(m, dict) and m.get("id") == "deepseek-flash" for m in models
    )
    if not have_model:
        models.append(
            {
                "id": "deepseek-flash",
                "provider_source_id": "deepseek",
                "model": "deepseek-flash",
                "enable": True,
            }
        )
        changed.append("added model deepseek-flash")
    conf["provider"] = models

    # --- required plugin settings -------------------------------------------
    ps = conf.get("provider_settings")
    if not isinstance(ps, dict):
        ps = {}
    if ps.get("streaming_response") is not False:
        ps["streaming_response"] = False
        changed.append("streaming_response -> False (required by the meme plugin)")
    conf["provider_settings"] = ps

    pls = conf.get("platform_settings")
    if not isinstance(pls, dict):
        pls = {}
    seg = pls.get("segmented_reply")
    if isinstance(seg, dict) and seg.get("enable"):
        seg["enable"] = False
        changed.append("segmented_reply -> False (keeps sticker markers off screen)")
    pls.setdefault("ignore_at_all", False)
    conf["platform_settings"] = pls

    if conf.get("timezone") != "Asia/Shanghai":
        conf["timezone"] = "Asia/Shanghai"
        changed.append("timezone -> Asia/Shanghai")

    save(conf_path, conf)

    # Token needs to be readable by the NapCat patcher.
    token = entry["ws_reverse_token"]
    if token_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(token_path)), exist_ok=True)
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(token)
        except OSError as exc:
            print("warning: could not write token file: %s" % exc)

    print("port  : %d" % QQ_PORT)
    print("token : %s" % token)
    for c in changed:
        print("  - %s" % c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
