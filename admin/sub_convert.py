#!/usr/bin/env python3
"""把订阅内容转成 sing-box 配置片段
支持：
  1) 已经是 sing-box JSON（含 outbounds）→ 原样输出
  2) Clash / Mihomo 订阅（YAML，含 proxies: 列表）→ 转成 sing-box 节点
  3) base64 编码的分享链接列表（v2ray 订阅）
  4) 明文分享链接列表（一行一个）
用法：sub-convert.py <输入文件> <输出文件>
"""
import base64
import json
import re
import sys
import urllib.parse


def b64d(s):
    s = "".join(s.split()).replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def qs(q):
    return {k: v[-1] for k, v in urllib.parse.parse_qs(q, keep_blank_values=True).items()}


def tls_of(q, sni_default, default_on=False):
    sec = (q.get("security") or "").lower()
    t = {}
    if sec in ("tls", "reality", "xtls") or (default_on and sec != "none"):
        t["enabled"] = True
        sni = q.get("sni") or q.get("host") or sni_default
        if sni:
            t["server_name"] = sni
        if q.get("alpn"):
            t["alpn"] = [a for a in q["alpn"].split(",") if a]
        if q.get("fp"):
            t["utls"] = {"enabled": True, "fingerprint": q["fp"]}
        if q.get("allowInsecure") in ("1", "true") or q.get("insecure") in ("1", "true"):
            t["insecure"] = True
        if sec == "reality":
            r = {"enabled": True}
            if q.get("pbk"):
                r["public_key"] = q["pbk"]
            if q.get("sid"):
                r["short_id"] = q["sid"]
            t["reality"] = r
    return t


def transport_of(q):
    ty = (q.get("type") or q.get("net") or "tcp").lower()
    host = q.get("host")
    path = q.get("path") or "/"
    hdr = (q.get("headerType") or "").lower()
    if ty == "ws":
        tr = {"type": "ws", "path": path}
        if host:
            tr["headers"] = {"Host": host}
        return tr
    if ty == "grpc":
        return {"type": "grpc", "service_name": q.get("serviceName") or ""}
    if ty == "http" or (ty == "tcp" and hdr == "http"):
        tr = {"type": "http", "path": path}
        if host:
            tr["host"] = [h for h in host.split(",") if h]
        return tr
    return None


def tag_of(frag, scheme, host):
    return urllib.parse.unquote(frag) if frag else "%s-%s" % (scheme.upper(), host)


def conv_vless(u):
    q = qs(u.query)
    host = u.hostname or ""
    o = {"type": "vless", "tag": tag_of(u.fragment, "vless", host), "server": host,
         "server_port": u.port or 443, "uuid": urllib.parse.unquote(u.username or "")}
    if q.get("flow"):
        o["flow"] = q["flow"]
    t = tls_of(q, host)
    if t:
        o["tls"] = t
    tr = transport_of(q)
    if tr:
        o["transport"] = tr
    return o


def conv_trojan(u):
    q = qs(u.query)
    host = u.hostname or ""
    o = {"type": "trojan", "tag": tag_of(u.fragment, "trojan", host), "server": host,
         "server_port": u.port or 443, "password": urllib.parse.unquote(u.username or "")}
    t = tls_of(q, host, default_on=True)
    t.setdefault("enabled", True)
    o["tls"] = t
    tr = transport_of(q)
    if tr:
        o["transport"] = tr
    return o


def conv_hy2(u):
    q = qs(u.query)
    host = u.hostname or ""
    o = {"type": "hysteria2", "tag": tag_of(u.fragment, "hysteria2", host), "server": host,
         "server_port": u.port or 443, "password": urllib.parse.unquote(u.username or "")}
    t = {"enabled": True}
    if q.get("sni"):
        t["server_name"] = q["sni"]
    if q.get("insecure") in ("1", "true"):
        t["insecure"] = True
    o["tls"] = t
    return o


def conv_tuic(u):
    q = qs(u.query)
    host = u.hostname or ""
    o = {"type": "tuic", "tag": tag_of(u.fragment, "tuic", host), "server": host,
         "server_port": u.port or 443, "uuid": urllib.parse.unquote(u.username or ""),
         "password": urllib.parse.unquote(u.password or ""),
         "tls": {"enabled": True, "alpn": ["h3"]}}
    if q.get("sni"):
        o["tls"]["server_name"] = q["sni"]
    return o


def conv_ss(u):
    tag = tag_of(u.fragment, "ss", u.hostname or "")
    nl = u.netloc
    if "@" in nl:
        ui, hp = nl.rsplit("@", 1)
        try:
            ui = b64d(ui).decode()
        except Exception:
            ui = urllib.parse.unquote(ui)
        m, _, p = ui.partition(":")
    else:
        dec = b64d(nl).decode()
        cred, _, hp = dec.rpartition("@")
        m, _, p = cred.partition(":")
    if hp.startswith("["):
        h, _, pt = hp[1:].partition("]")
        port = int(pt.lstrip(":") or 443)
    else:
        h, _, pt = hp.rpartition(":")
        port = int(pt or 443) if h else 443
    return {"type": "shadowsocks", "tag": tag, "server": h, "server_port": port,
            "method": m, "password": p}


def conv_vmess(u):
    raw = u.netloc or (u.path or "").lstrip("/")
    d = json.loads(b64d(raw).decode())
    host = d.get("add") or ""
    o = {"type": "vmess", "tag": d.get("ps") or tag_of(u.fragment, "vmess", host),
         "server": host, "server_port": int(d.get("port") or 443),
         "uuid": d.get("id") or "", "security": d.get("scy") or "auto",
         "alter_id": int(d.get("aid") or 0)}
    tr = transport_of({"type": (d.get("net") or "tcp").lower(), "path": d.get("path") or "/",
                       "host": d.get("host") or "", "serviceName": d.get("path") or "",
                       "headerType": (d.get("type") or "").lower()})
    if tr:
        o["transport"] = tr
    if (d.get("tls") or "").lower() == "tls":
        t = {"enabled": True}
        if d.get("sni"):
            t["server_name"] = d["sni"]
        elif d.get("host"):
            t["server_name"] = d["host"]
        o["tls"] = t
    return o


def conv_anytls(u):
    q = qs(u.query)
    host = u.hostname or ""
    o = {"type": "anytls", "tag": tag_of(u.fragment, "anytls", host), "server": host,
         "server_port": u.port or 443, "password": urllib.parse.unquote(u.username or "")}
    t = {"enabled": True}
    if q.get("sni"):
        t["server_name"] = q["sni"]
    if q.get("insecure") in ("1", "true"):
        t["insecure"] = True
    o["tls"] = t
    return o


HANDLERS = {"vless": conv_vless, "trojan": conv_trojan, "hysteria2": conv_hy2, "hy2": conv_hy2,
            "tuic": conv_tuic, "ss": conv_ss, "vmess": conv_vmess, "anytls": conv_anytls}


def parse_links(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if "://" not in line:
            continue
        try:
            u = urllib.parse.urlsplit(line)
            fn = HANDLERS.get((u.scheme or "").lower())
            if not fn:
                continue
            o = fn(u)
            if o.get("server") and o.get("tag"):
                out.append(o)
        except Exception:
            continue
    return out


def unique_tags(nodes):
    seen = {}
    for n in nodes:
        t = n["tag"]
        if t in seen:
            seen[t] += 1
            n["tag"] = "%s-%d" % (t, seen[t])
        else:
            seen[t] = 1
    return nodes


# ============ Clash / Mihomo YAML 订阅支持 ============

def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v in ("true", "True", "yes", "on"):
        return True
    if v in ("false", "False", "no", "off"):
        return False
    if v in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _flow(s):
    """解析 {a: 1, b: {c: 2}, d: [1,2]} 这种行内写法"""
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    out, buf, depth, q = {}, [], 0, None
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
            buf.append(ch)
            continue
        if ch in "{[(":
            depth += 1
        if ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            _kv_into(out, "".join(buf))
            buf = []
            continue
        buf.append(ch)
    _kv_into(out, "".join(buf))
    return out


def _kv_into(d, part):
    q, idx = None, -1
    for i, ch in enumerate(part):
        if q:
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
            continue
        if ch == ":":
            idx = i
            break
    if idx < 0:
        return
    k = part[:idx].strip()
    v = part[idx + 1:].strip()
    if not k:
        return
    if v.startswith("{") or v.startswith("["):
        d[k] = _flow(v) if v.startswith("{") else [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    else:
        d[k] = _scalar(v)


def _ylines(text):
    out = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        out.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))
    return out


def _ymap(lines, i, indent):
    d = {}
    while i < len(lines):
        ind, s = lines[i]
        if ind < indent or s.startswith("- "):
            break
        if ":" not in s:
            i += 1
            continue
        k, _, v = s.partition(":")
        k, v = k.strip(), v.strip()
        if v == "":
            j = i + 1
            if j < len(lines) and lines[j][0] > ind:
                if lines[j][1].startswith("- "):
                    d[k], i = _ylist(lines, j, lines[j][0])
                else:
                    d[k], i = _ymap(lines, j, lines[j][0])
                continue
            d[k] = ""
            i += 1
            continue
        d[k] = _scalar(v)
        i += 1
    return d, i


def _ylist(lines, i, indent):
    out = []
    while i < len(lines):
        ind, s = lines[i]
        if ind < indent or not s.startswith("- "):
            break
        body = s[2:].strip()
        if not body:
            i += 1
            continue
        if body.startswith("{"):
            out.append(_flow(body))
            i += 1
            continue
        if ":" in body:
            k, _, v = body.partition(":")
            item = {}
            if v.strip() == "":
                j = i + 1
                if j < len(lines) and lines[j][0] > ind:
                    if lines[j][1].startswith("- "):
                        item[k.strip()], i = _ylist(lines, j, lines[j][0])
                    else:
                        item[k.strip()], i = _ymap(lines, j, lines[j][0])
                    out.append(item)
                    continue
                item[k.strip()] = ""
                i += 1
                out.append(item)
                continue
            item[k.strip()] = _scalar(v)
            j = i + 1
            if j < len(lines) and lines[j][0] > ind:
                sub, i = _ymap(lines, j, lines[j][0])
                item.update(sub)
            else:
                i = j
            out.append(item)
            continue
        out.append(_scalar(body))
        i += 1
    return out, i


def parse_clash(text):
    """从 Clash / Mihomo 订阅里取出 proxies 列表"""
    if "proxies:" not in text:
        return []
    lines = _ylines(text)
    for i, (ind, s0) in enumerate(lines):
        if s0.startswith("proxies:") and ind == 0:
            rest = s0[len("proxies:"):].strip()
            if rest.startswith("["):
                return [x for x in _flow(rest) if isinstance(x, dict)]
            j = i + 1
            if j < len(lines) and lines[j][1].startswith("- "):
                items, _ = _ylist(lines, j, lines[j][0])
                return [x for x in items if isinstance(x, dict)]
            return []
    return []


def _tls(p, default_name=None, alpn=None):
    t = {"enabled": True}
    sn = p.get("servername") or p.get("sni") or default_name
    if sn:
        t["server_name"] = sn
    a = alpn or p.get("alpn")
    if a:
        t["alpn"] = a if isinstance(a, list) else [x.strip() for x in str(a).split(",") if x.strip()]
    if p.get("skip-cert-verify") is True:
        t["insecure"] = True
    return t


def _transport(p):
    net = (p.get("network") or "tcp").lower()
    if net == "ws":
        o = p.get("ws-opts") or {}
        tr = {"type": "ws", "path": o.get("path") or "/"}
        h = o.get("headers") or {}
        if isinstance(h, dict) and h.get("host"):
            tr["headers"] = {"host": h["host"]}
        return tr
    if net == "grpc":
        o = p.get("grpc-opts") or {}
        return {"type": "grpc", "service_name": o.get("grpc-service-name") or ""}
    if net in ("http", "h2"):
        o = p.get("http-opts") or {}
        tr = {"type": "http", "path": (o.get("path") or ["/"])[0] if isinstance(o.get("path"), list) else (o.get("path") or "/")}
        h = o.get("headers") or {}
        if isinstance(h, dict) and h.get("Host"):
            v = h["Host"]
            tr["host"] = v if isinstance(v, list) else [v]
        return tr
    return None


def clash_node(p):
    """Clash 节点 → sing-box outbound"""
    ty = (p.get("type") or "").lower()
    name = str(p.get("name") or "").strip()
    server = str(p.get("server") or "").strip()
    if not server:
        return None
    try:
        port = int(p.get("port") or 443)
    except Exception:
        port = 443

    if ty == "ss":
        o = {"type": "shadowsocks", "tag": name or ("SS-" + server), "server": server,
             "server_port": port, "method": str(p.get("cipher") or "aes-128-gcm"),
             "password": str(p.get("password") or "")}
        return o

    if ty == "vmess":
        o = {"tag": name or ("VMESS-" + server), "type": "vmess", "server": server,
             "server_port": port, "uuid": str(p.get("uuid") or ""),
             "alter_id": int(p.get("alterId") or 0), "security": str(p.get("cipher") or "auto"),
             "tcp_fast_open": p.get("tfo") is True}
        if p.get("tls") is True:
            o["tls"] = _tls(p, server)
            if o["tls"].get("insecure") is not True:
                o["tls"].pop("insecure", None)
        tr = _transport(p)
        if tr:
            o["transport"] = tr
        return o

    if ty == "vless":
        o = {"type": "vless", "tag": name or ("VLESS-" + server), "server": server,
             "server_port": port, "uuid": str(p.get("uuid") or ""),
             "tcp_fast_open": p.get("tfo") is True}
        t = _tls(p, server)
        if p.get("tls") is not True and not p.get("reality-opts"):
            t = {}
        ro = p.get("reality-opts") or {}
        if ro:
            r = {"enabled": True}
            if ro.get("public-key"):
                r["public_key"] = ro["public-key"]
            if ro.get("short-id"):
                r["short_id"] = ro["short-id"]
            t["reality"] = r
        fp = p.get("client-fingerprint")
        if fp:
            t["utls"] = {"enabled": True, "fingerprint": fp}
        if t:
            o["tls"] = t
        if p.get("flow"):
            o["flow"] = p["flow"]
        tr = _transport(p)
        if tr:
            o["transport"] = tr
        return o

    if ty == "trojan":
        o = {"type": "trojan", "tag": name or ("TROJAN-" + server), "server": server,
             "server_port": port, "password": str(p.get("password") or ""), "tls": _tls(p, server)}
        tr = _transport(p)
        if tr:
            o["transport"] = tr
        return o

    if ty in ("hysteria2", "hy2"):
        return {"tag": name or ("HY2-" + server), "type": "hysteria2", "server": server,
                "server_port": port, "password": str(p.get("auth") or p.get("password") or ""),
                "tls": _tls(p, server)}

    if ty == "hysteria":
        return {"tag": name or ("HY-" + server), "type": "hysteria", "server": server,
                "server_port": port, "auth_str": str(p.get("auth-str") or p.get("auth") or ""),
                "tls": _tls(p, server)}

    if ty == "tuic":
        o = {"tag": name or ("TUIC-" + server), "type": "tuic", "server": server,
             "server_port": port, "uuid": str(p.get("uuid") or ""),
             "password": str(p.get("password") or ""),
             "tls": _tls(p, server, alpn=["h3"])}
        if p.get("congestion-controller"):
            o["congestion_control"] = p["congestion-controller"]
        return o

    if ty == "anytls":
        return {"tag": name or ("ANYTLS-" + server), "type": "anytls", "server": server,
                "server_port": port, "password": str(p.get("password") or ""),
                "tls": _tls(p, server)}

    if ty == "http":
        o = {"type": "http", "tag": name or ("HTTP-" + server), "server": server, "server_port": port}
        if p.get("username"):
            o["username"] = str(p["username"])
            o["password"] = str(p.get("password") or "")
        if p.get("tls") is True:
            o["tls"] = _tls(p, server)
        return o

    if ty == "socks5":
        o = {"type": "socks", "tag": name or ("SOCKS-" + server), "server": server, "server_port": port,
             "version": "5"}
        if p.get("username"):
            o["username"] = str(p["username"])
            o["password"] = str(p.get("password") or "")
        return o

    return None


def convert_clash(text):
    out = []
    for p in parse_clash(text):
        try:
            n = clash_node(p)
            if n and n.get("server") and n.get("tag"):
                out.append(n)
        except Exception:
            continue
    return out


def main():
    src, dst = sys.argv[1], sys.argv[2]
    raw = open(src, "rb").read()
    text = raw.decode("utf-8", "replace").strip()

    # 1) 已经是 sing-box JSON
    if text.startswith("{"):
        try:
            d = json.loads(text)
            if isinstance(d, dict) and isinstance(d.get("outbounds"), list):
                open(dst, "w", encoding="utf-8").write(text)
                return 0
        except Exception:
            pass

    # 2) Clash / Mihomo YAML
    nodes = convert_clash(text)
    if nodes:
        nodes = unique_tags(nodes)
        json.dump({"outbounds": nodes}, open(dst, "w", encoding="utf-8"), ensure_ascii=False)
        sys.stderr.write("converted %d nodes (clash yaml)\n" % len(nodes))
        return 0

    # 3) base64
    nodes = []
    try:
        dec = b64d(text).decode("utf-8", "replace")
        if "://" in dec:
            nodes = parse_links(dec)
    except Exception:
        pass

    # 4) 明文
    if not nodes:
        nodes = parse_links(text)

    if not nodes:
        sys.stderr.write("no supported nodes found\n")
        return 1

    nodes = unique_tags(nodes)
    json.dump({"outbounds": nodes}, open(dst, "w", encoding="utf-8"), ensure_ascii=False)
    sys.stderr.write("converted %d nodes\n" % len(nodes))
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ============ mihomo (Clash.Meta) 节点输出模式 ============
# 在 sing-box 中间结构的基础上再映射一层，得到 mihomo 的 proxies 条目。

def _ws_opts(tr):
    if not tr:
        return {}
    t = tr.get("type")
    if t == "ws":
        o = {"path": tr.get("path") or "/"}
        if tr.get("headers"):
            o["headers"] = dict(tr["headers"])
        return {"network": "ws", "ws-opts": o}
    if t == "grpc":
        return {"network": "grpc", "grpc-opts": {"grpc-service-name": tr.get("service_name") or ""}}
    if t == "http":
        o = {"path": [tr.get("path") or "/"]}
        if tr.get("host"):
            o["host"] = list(tr["host"])
        return {"network": "h2", "h2-opts": o}
    return {}


def _tls_common(o, tls):
    if not tls or not tls.get("enabled"):
        return
    o["tls"] = True
    if tls.get("server_name"):
        o["servername"] = tls["server_name"]
    if tls.get("insecure"):
        o["skip-cert-verify"] = True
    if tls.get("alpn"):
        o["alpn"] = list(tls["alpn"])
    u = tls.get("utls") or {}
    if u.get("fingerprint"):
        o["client-fingerprint"] = u["fingerprint"]
    r = tls.get("reality") or {}
    if r.get("enabled"):
        ro = {}
        if r.get("public_key"):
            ro["public-key"] = r["public_key"]
        if r.get("short_id"):
            ro["short-id"] = r["short_id"]
        if ro:
            o["reality-opts"] = ro


def to_mihomo_node(n):
    """sing-box 中间结构 -> mihomo proxies 条目（dict）"""
    ty = n.get("type")
    o = {"name": n.get("tag") or "-", "server": n.get("server"),
         "port": int(n.get("server_port") or 443)}
    if ty == "vless":
        o.update({"type": "vless", "uuid": n.get("uuid") or "", "udp": True})
        if n.get("flow"):
            o["flow"] = n["flow"]
        _tls_common(o, n.get("tls"))
        o.update(_ws_opts(n.get("transport")))
    elif ty == "vmess":
        o.update({"type": "vmess", "uuid": n.get("uuid") or "",
                  "alterId": int(n.get("alter_id") or 0),
                  "cipher": n.get("security") or "auto", "udp": True})
        _tls_common(o, n.get("tls"))
        o.update(_ws_opts(n.get("transport")))
    elif ty == "shadowsocks":
        o.update({"type": "ss", "cipher": n.get("method") or "aes-128-gcm",
                  "password": n.get("password") or "", "udp": True})
    elif ty == "trojan":
        o.update({"type": "trojan", "password": n.get("password") or "", "udp": True})
        _tls_common(o, n.get("tls") or {"enabled": True})
        o.update(_ws_opts(n.get("transport")))
    elif ty == "hysteria2":
        o.update({"type": "hysteria2", "password": n.get("password") or "", "udp": True})
        _tls_common(o, n.get("tls") or {"enabled": True})
    elif ty == "hysteria":
        o.update({"type": "hysteria", "auth-str": n.get("password") or "", "udp": True})
        _tls_common(o, n.get("tls") or {"enabled": True})
    elif ty == "tuic":
        o.update({"type": "tuic", "uuid": n.get("uuid") or "", "password": n.get("password") or "",
                  "congestion-controller": "bbr", "udp-relay-mode": "native"})
        _tls_common(o, n.get("tls") or {"enabled": True})
    elif ty == "anytls":
        o.update({"type": "anytls", "password": n.get("password") or "", "udp": True})
        _tls_common(o, n.get("tls") or {"enabled": True})
    else:
        return None
    return o


def unique_names(nodes):
    seen = {}
    for n in nodes:
        t = str(n.get("name") or "-")
        if t in seen:
            seen[t] += 1
            n["name"] = "%s-%d" % (t, seen[t])
        else:
            seen[t] = 1
    return nodes


def to_mihomo(text):
    """分享链接 / 文本 -> mihomo 节点列表（dict）"""
    out = []
    try:
        for n in parse_links(text or ""):
            m = to_mihomo_node(n)
            if m:
                out.append(m)
    except Exception:
        pass
    # 兼容直接粘贴的 Clash 风格 flow 单行：{name: x, type: ss, ...}
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-").strip()
        if not line.startswith("{") or "type:" not in line:
            continue
        try:
            d = _flow(line)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("name") and d.get("type") and d.get("type") not in ("direct", "dns"):
            out.append(d)
    return unique_names(out)
