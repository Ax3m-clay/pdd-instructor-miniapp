#!/usr/bin/env python3
"""Собрать config.json для xray из VPN-подписки.

В конфиг идут все VLESS-серверы подписки (кроме исключённых), xray сам раз в
минуту проверяет их через туннель (observatory) и гонит трафик через самый
быстрый живой (балансировщик leastPing). Упал сервер — переключится сам.

    python3 gen_config.py URL_ПОДПИСКИ > config.json

Список выбранных серверов печатается в stderr, UUID туда не попадает.
"""

import base64
import json
import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request, urlopen

# Выход в России для обхода блокировок бесполезен, а сервер на техработах — мёртвый.
EXCLUDE = re.compile(r"🇷🇺|Москв|Росси|[Тт]ех\.? ?работ")
PROBE_URL = "https://www.gstatic.com/generate_204"


def fetch(url):
    req = Request(url, headers={"User-Agent": "curl/8"})  # некоторые сервисы отдают формат по User-Agent
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8").strip()
    if "://" in raw:
        return raw
    return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")


def parse_vless(link):
    parts = urlsplit(link)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    return {
        "id": unquote(parts.username or ""),
        "host": parts.hostname,
        "port": parts.port or 443,
        "name": unquote(parts.fragment).strip(),
        "q": q,
    }


def outbound(server, tag):
    q = server["q"]
    network = q.get("type", "tcp")
    security = q.get("security", "none")
    stream = {"network": network, "security": security}
    if security == "reality":
        stream["realitySettings"] = {
            "serverName": q.get("sni", ""),
            "fingerprint": q.get("fp", "chrome"),
            "publicKey": q.get("pbk", ""),
            "shortId": q.get("sid", ""),
        }
        if q.get("spx"):
            stream["realitySettings"]["spiderX"] = q["spx"]
    elif security == "tls":
        stream["tlsSettings"] = {"serverName": q.get("sni") or server["host"], "fingerprint": q.get("fp", "chrome")}
        if q.get("alpn"):
            stream["tlsSettings"]["alpn"] = q["alpn"].split(",")
    if network == "ws":
        stream["wsSettings"] = {"path": q.get("path", "/"), "headers": {"Host": q.get("host", server["host"])}}
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": q.get("serviceName", "")}

    user = {"id": server["id"], "encryption": q.get("encryption", "none")}
    if q.get("flow"):
        user["flow"] = q["flow"]
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {"vnext": [{"address": server["host"], "port": server["port"], "users": [user]}]},
        "streamSettings": stream,
    }


def build(links):
    servers = [parse_vless(l) for l in links if l.startswith("vless://")]
    chosen = [s for s in servers if s["id"] and s["host"] and not EXCLUDE.search(s["name"])]
    if not chosen:
        raise SystemExit("в подписке нет подходящих VLESS-серверов — конфиг не трогаю")

    outbounds, used = [], set()
    for s in chosen:
        tag = "proxy-" + s["host"].split(".")[0]
        while tag in used:
            tag += "x"
        used.add(tag)
        outbounds.append(outbound(s, tag))
        print(f"  {tag:<22} {s['host']}:{s['port']}  {s['name']}", file=sys.stderr)
    skipped = [s["name"] for s in servers if s not in chosen]
    if skipped:
        print(f"  пропущены: {', '.join(skipped)}", file=sys.stderr)

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "socks-in", "listen": "0.0.0.0", "port": 10808, "protocol": "socks", "settings": {"udp": True}},
            {"tag": "http-in", "listen": "0.0.0.0", "port": 10809, "protocol": "http"},
        ],
        "outbounds": outbounds,
        "observatory": {
            "subjectSelector": ["proxy-"],
            "probeUrl": PROBE_URL,
            "probeInterval": "1m",
            "enableConcurrency": True,
        },
        "routing": {
            "balancers": [{"tag": "auto", "selector": ["proxy-"], "strategy": {"type": "leastPing"}}],
            "rules": [{"type": "field", "inboundTag": ["socks-in", "http-in"], "balancerTag": "auto"}],
        },
    }


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    links = [l.strip() for l in fetch(sys.argv[1]).splitlines() if l.strip()]
    config = build(links)
    sys.stdout.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
