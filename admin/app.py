#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群晖 mihomo 网页管理台（Python 标准库单文件）

页面：状态 / 机场 / 节点 / 策略 / 工具（全中文）
接口：POST /login  /api/gen  /api/panel-ui     GET /api/health  /api/status
生成：$SB_BASE/etc/config.yaml（mihomo / Clash.Meta 格式，纯文本手写输出，不依赖 pyyaml）
"""
import base64
import io
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import sub_convert          # 分享链接解析器（同目录）
except Exception:               # 缺失时不致命，只是不能加手动节点
    sub_convert = None

APP_NAME = "mihomo-admin"
APP_VERSION = "1.9"

BASE = os.environ.get("SB_BASE", "/data")
ETC = os.path.join(BASE, "etc")
PROVIDERS = os.path.join(ETC, "providers")
UI_DIR = os.path.join(ETC, "ui")
CONFIG = os.path.join(ETC, "config.yaml")
BACKUP_DIR = os.path.join(BASE, "backup")
LOG_PATH = os.path.join(BASE, "admin.log")
SETTINGS_PATH = os.path.join(BASE, "settings.json")
SUBURL_PATH = os.path.join(BASE, "sub.url")
SUBLINKS_PATH = os.path.join(BASE, "sublinks.json")

ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "9093") or 9093)
ADMIN_BIND = os.environ.get("ADMIN_BIND", "127.0.0.1") or "127.0.0.1"
# 代理/面板端口也支持从环境变量来，方便同机装第二套（compose 里会传进来）
PROXY_PORT = os.environ.get("PROXY_PORT", "") or "7890"
PANEL_PORT = os.environ.get("PANEL_PORT", "") or "9090"
# 旁路由：由部署侧（compose 里的 TUN_ENABLE）决定默认是否生成带 TUN 的配置
TUN_ENABLE = (os.environ.get("TUN_ENABLE", "") or "").strip().lower() in ("1", "true", "yes", "on")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "") or "admin"
HOST_BASE = os.environ.get("HOST_BASE", "").rstrip("/")
PROXY_ADDR = os.environ.get("PROXY_ADDR", "")
PANEL_ADDR = os.environ.get("PANEL_ADDR", "")
PANEL_URL = os.environ.get("PANEL_URL", "")
IMAGE_NAME = "metacubex/mihomo:latest"

CT_KERNEL = os.environ.get("CT_MH", "mihomo")
KERNEL_DIR = "/root/.config/mihomo"
KERNEL_CFG = KERNEL_DIR + "/config.yaml"

_UPD_LOCK = threading.Lock()
_SESSIONS = {}
_SESSION_LOCK = threading.Lock()

# ---- 会话持久化：管理台重启后不用重新登录 ----
def _sess_file():
    return os.path.join(BASE, ".sessions.json")


def _sess_load():
    try:
        with open(_sess_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    now = time.time()
    out = {}
    for k, v in (d or {}).items():
        try:
            if float(v) > now:
                out[str(k)] = float(v)
        except Exception:
            pass
    return out


def _sess_save():
    try:
        tmp = _sess_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_SESSIONS, f)
        os.replace(tmp, _sess_file())
    except Exception:
        pass
_LOG_LOCK = threading.Lock()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    """写 $SB_BASE/admin.log，超过 512KB 只保留最后 500 行"""
    line = "[%s] %s\n" % (now(), msg)
    try:
        with _LOG_LOCK:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
                with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-500:]
                with open(LOG_PATH, "w", encoding="utf-8") as f:
                    f.writelines(tail)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- 地区 / 业务组
# 图标与 CT200 参考实现同一套（s0lnce/mofish-icon）
ICON_BASE = "https://raw.githubusercontent.com/s0lnce/mofish-icon/refs/heads/main/mihomo"
ICON_PURE = ICON_BASE + "/pure/corner/%s.png"
ICON_FLAG = ICON_BASE + "/flag/corner/%s.png"

# (设置里的键, 显示名, 节点名过滤, 国旗图标名)
REGION_DEFS = [
    ("香港", "香港", r"(?i)^(HK|香港|Hong)", "hongkong"),
    ("日本", "日本", r"(?i)^(JP|日本|Japan|东京|大阪)", "japan"),
    ("新加坡", "狮城", r"(?i)^(SG|新加坡|狮城|Singapore)", "singapore"),
    ("美国", "美国", r"(?i)^(US|美国|United|洛杉矶|圣何塞|西雅图)", "usa"),
    ("台湾", "台湾", r"(?i)^(TW|台湾|臺灣)", "taiwan"),
    ("韩国", "韩国", r"(?i)^(KR|韩国|Korea|首尔)", "south_korea"),
    ("英国", "英国", r"(?i)^(UK|GB|英国|United Kingdom|伦敦|London)", "uk"),
    ("德国", "德国", r"(?i)^(DE|德国|Germany|法兰克福|Frankfurt)", "eu"),
    ("法国", "法国", r"(?i)^(FR|法国|France|巴黎|Paris)", "eu"),
    ("荷兰", "荷兰", r"(?i)^(NL|荷兰|Netherlands|阿姆斯特丹)", "eu"),
    ("意大利", "意大利", r"(?i)^(IT|意大利|Italy|米兰)", "eu"),
    ("西班牙", "西班牙", r"(?i)^(ES|西班牙|Spain|马德里)", "eu"),
    ("欧洲", "欧洲", r"(?i)^(EU|欧洲|Europe)", "eu"),
]
REGION_MAP = {r[0]: r for r in REGION_DEFS}

# (业务组名, 图标名, 优先地区键) —— 优先地区决定该组里「XX故转」排在第一位（= 默认线路）
BIZ_DEFS = [
    ("AI", "ai", "香港"),
    ("Apple", "apple", "香港"),
    ("GitHub", "github", "日本"),
    ("Google", "google", "香港"),
    ("Microsoft", "microsoft", "日本"),
    ("NETFLIX", "netflix", "香港"),
    ("OneDrive", "onedrive", "日本"),
    ("Steam", "steam", "香港"),
    ("Telegram", "telegram", "香港"),
    ("TikTok", "douyin", "香港"),
    ("Wallet", "wallet", "香港"),
    ("YouTube", "youtube", "香港"),
]
BIZ_GROUPS = [b[0] for b in BIZ_DEFS]
BIZ_MAP = {b[0]: b for b in BIZ_DEFS}

DEFAULT_PROXY = "默认代理"
NON_HK = "狮城自动"                 # 香港上不了 TikTok，AI 同理默认狮城


def region_label(key):
    r = REGION_MAP.get(key)
    return r[1] if r else ""


def biz_members(regions, pref_label):
    """业务组的可选项：地区故转 / 手动 / 自动 + 全部自动 / 全部手动 / 直连
    pref_label 所在地区的「故转」排最前（mihomo 里 select 组默认选第一个）"""
    labels = [REGION_MAP[r][1] for r in regions]
    fal = ["%s故转" % x for x in labels]
    if pref_label and ("%s故转" % pref_label) in fal:
        fal.remove("%s故转" % pref_label)
        fal.insert(0, "%s故转" % pref_label)
    return (fal + ["%s手动" % x for x in labels] + ["%s自动" % x for x in labels]
            + ["全部自动", "全部手动", "直连"])


def default_groups():
    g = {name: "%s故转" % region_label(pref) for name, _icon, pref in BIZ_DEFS}
    g["AI"] = NON_HK
    g["TikTok"] = NON_HK
    return g


def default_settings():
    return {
        "schedule_enabled": True,
        "schedule_time": "04:00",
        "last_run": "",
        "auto_update_panel": True,
        "auto_update_last": "",
        "update_available": "",
        "admin_bind": "",
        "proxy_ip": "",
        "proxy_port": PROXY_PORT,
        "panel_port": PANEL_PORT,
        "admin_port": str(ADMIN_PORT),
        "secret": ADMIN_PASS,
        "airports": [],                      # [{"name":..,"url":..,"interval":86400}]
        "nodes": [],                         # 手动节点原始分享链接（一行一个）
        "regions": ["香港", "日本", "新加坡"],
        "custom_groups": [],
        "groups": default_groups(),
        "ruleset_enabled": True,
        "tun_enable": TUN_ENABLE,            # 旁路由模式（TUN），默认由 compose 的 TUN_ENABLE 决定
        "direct_domains": "",                # 逗号分隔：这些域名走直连（旁路由下别绕代理）
    }


def load_settings():
    s = default_settings()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k, v in d.items():
                if k == "groups" and isinstance(v, dict):
                    g = s["groups"]
                    g.update(v)
                    s["groups"] = g
                elif k == "airports" and isinstance(v, list):
                    s["airports"] = [a for a in v if isinstance(a, dict) and a.get("url")]
                elif k == "nodes" and isinstance(v, list):
                    s["nodes"] = [str(x) for x in v if str(x).strip()]
                elif v is not None or k in ("last_run", "admin_bind", "proxy_ip"):
                    s[k] = v
    except Exception:
        pass
    # 端口留空时用环境变量（同机第二套 / 换机器时靠这个对齐）
    if not str(s.get("proxy_port") or "").strip():
        s["proxy_port"] = PROXY_PORT
    if not str(s.get("panel_port") or "").strip():
        s["panel_port"] = PANEL_PORT
    # 首次运行：把 sub.url 里已有的订阅导入成第一个机场
    if not s["airports"] and os.path.exists(SUBURL_PATH):
        try:
            with open(SUBURL_PATH, "r", encoding="utf-8", errors="replace") as f:
                url = f.read().strip().splitlines()
            url = url[0].strip() if url else ""
            if url.startswith("http"):
                s["airports"] = [{"name": "airport", "url": url, "interval": 86400}]
                save_settings(s)
        except Exception:
            pass
    return s


def save_settings(s):
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception as e:  # noqa: BLE001
        log("保存 settings.json 失败: %r" % (e,))
        raise


# ---------------------------------------------------------------- 小工具
def jloads(raw, default=None):
    """请求体可能是畸形 JSON，容错"""
    if not raw:
        return default
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return json.loads(raw)
    except Exception:
        # 退化尝试：当作表单
        try:
            return {k: v[-1] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        except Exception:
            return default


def ystr(v):
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    if re.match(r"^[A-Za-z0-9_./:@%+-]+$", s) and not re.match(
            r"^(true|false|null|yes|no|on|off|[0-9.]+)$", s, re.I):
        return s
    return json.dumps(s, ensure_ascii=False)


def ydump(obj, ind=0):
    """极简 YAML 输出器（够 mihomo 用）"""
    sp = "  " * ind
    out = []
    if isinstance(obj, dict):
        if not obj:
            return ["{}"]
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append("%s%s:" % (sp, k))
                out.extend(ydump(v, ind + 1))
            elif isinstance(v, (dict, list)):
                out.append("%s%s: %s" % (sp, k, "{}" if isinstance(v, dict) else "[]"))
            else:
                out.append("%s%s: %s" % (sp, k, ystr(v)))
        return out
    if isinstance(obj, list):
        if not obj:
            return ["[]"]
        for it in obj:
            if isinstance(it, dict) and it:
                first = True
                for k, v in it.items():
                    pre = "%s- " % sp if first else "%s  " % sp
                    first = False
                    if isinstance(v, (dict, list)) and v:
                        out.append("%s%s:" % (pre, k))
                        out.extend(ydump(v, ind + 2))
                    elif isinstance(v, (dict, list)):
                        out.append("%s%s: %s" % (pre, k, "{}" if isinstance(v, dict) else "[]"))
                    else:
                        out.append("%s%s: %s" % (pre, k, ystr(v)))
            else:
                out.append("%s- %s" % (sp, ystr(it)))
        return out
    return ["%s%s" % (sp, ystr(obj))]



# ---------------------------------------------------------------- 配置生成
HC_URL = "https://www.gstatic.com/generate_204"
MRS = "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@meta/geo/geosite/%s.mrs"
PROXYLITE = "https://cdn.jsdelivr.net/gh/qichiyuhub/rule@main/rules/proxy.list"
# include-all 时排除掉直连 / dns 这类内置代理
EXCL = r"(?i)^((?!(直连|dns)).)*$"

BIZ_RULES = [
    ("category-ai-!cn", "AI"),
    ("github_domain", "GitHub"),
    ("youtube_domain", "YouTube"),
    ("google_domain", "Google"),
    ("netflix_domain", "NETFLIX"),
    ("steam_domain", "Steam"),
    ("onedrive_domain", "OneDrive"),
    ("microsoft_domain", "Microsoft"),
    ("apple_domain", "Apple"),
    ("tiktok_domain", "TikTok"),
    ("telegram_domain", "Telegram"),
    ("paypal_domain", "Wallet"),
    ("gfw_domain", "漏网之鱼"),
]
# (rule-provider 名字, MetaCubeX meta-rules-dat 里对应的 geosite 文件名)
RULE_PROVIDERS = [
    ("tiktok_domain", "tiktok"),
    ("category-ai-!cn", "category-ai-!cn"),
    ("youtube_domain", "youtube"),
    ("google_domain", "google"),
    ("github_domain", "github"),
    ("telegram_domain", "telegram"),
    ("paypal_domain", "paypal"),
    ("onedrive_domain", "onedrive"),
    ("microsoft_domain", "microsoft"),
    ("apple_domain", "apple"),
    ("gfw_domain", "gfw"),
    ("netflix_domain", "netflix"),
    ("steam_domain", "steam"),
]


def manual_nodes(s):
    """settings.nodes（分享链接原文）-> mihomo 节点 dict 列表"""
    if not s.get("nodes") or sub_convert is None:
        return []
    try:
        return sub_convert.to_mihomo("\n".join(s["nodes"]))
    except Exception as e:  # noqa: BLE001
        log("解析手动节点失败: %r" % (e,))
        return []


def gen_config(s):
    """按 settings 生成 mihomo config.yaml 文本"""
    secret = s.get("secret") or ADMIN_PASS
    L = []
    a = L.append
    a("# ============================================================")
    a("# 群晖 mihomo 配置 —— 由网页管理台自动生成，请勿手动修改")
    a("# 生成时间：%s" % now())
    a("# 规则集分流引用：七尺宇 · qichiyuhub/rule（仅按 URL 引用，不搬运、不再分发）")
    a("# ============================================================")
    a("")
    a("mixed-port: 7890")
    a("allow-lan: true")
    a('bind-address: "*"')
    a("mode: rule")
    a("log-level: info")
    a("ipv6: false")
    a("unified-delay: true")
    a("tcp-concurrent: true")
    a("")
    a("external-controller: 127.0.0.1:9091")     # 只给管理台用（对外统一走管理台的 /clash 反代）
    # ⛔ 不再生成 external-ui：
    #   面板现在由管理台自己提供（/ui/），内核不需要注册它；
    #   而且内核会按"配置目录之外"的路径做 SAFE_PATHS 安全检查，
    #   写了 external-ui 反而会让内核启动即 fatal（path is not subpath of ...）
    a('secret: "%s"' % str(secret).replace('"', '\\"'))
    a("")
    a("geodata-mode: true")
    a("geo-auto-update: true")
    a("geox-url:")
    a('  geoip: "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.dat"')
    a('  geosite: "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geosite.dat"')
    a('  mmdb: "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/country.mmdb"')
    a("")
    a("profile:")
    a("  store-selected: true")
    a("  store-fake-ip: true")
    a("")
    tun_on = bool(s.get("tun_enable"))
    a("tun:")
    if tun_on:
        a("  enable: true            # 旁路由模式：接管本机+转发流量（需 host 网络 + NET_ADMIN + /dev/net/tun）")
        a("  device: Meta")
        a("  stack: gVisor")
        a("  auto-route: true")
        a("  auto-detect-interface: true")
        a("  auto-redirect: false    # 群晖没有 nft；auto-route 已足够接管流量")
        a("  inet4-address: [198.18.0.0/30]")
        a("  inet6-address: [fdfe:dcba:9876::1/126]")
        a("  dns-hijack:")
        a('    - "any:53"')
        a('    - "tcp://any:53"')
    else:
        a("  enable: false            # 端口模式：只在 mixed-port 提供代理给客户端")
    a("")
    a("dns:")
    a("  enable: true")
    a("  listen: %s" % ("0.0.0.0:1053" if tun_on else "0.0.0.0:53"))
    a("  enhanced-mode: fake-ip")
    a("  fake-ip-range: 198.18.0.1/16")
    a("  default-nameserver: [223.5.5.5, 114.114.114.114]")
    a("  nameserver:")
    a("    - https://dns.alidns.com/dns-query")
    a("    - 223.5.5.5")
    a("  proxy-server-nameserver: [https://dns.alidns.com/dns-query]")
    a("  direct-nameserver:")
    a("    - 223.5.5.5")
    a("    - 114.114.114.114")
    a("  respect-rules: true")
    a("  fake-ip-filter:")
    a('    - "+.msftconnecttest.com"')
    a('    - "+.msftncsi.com"')
    a("")

    # ---- 机场 = 独立 proxy-providers（用户明确要的设计）
    airports = s.get("airports") or []
    providers = []
    if not airports:
        a("proxy-providers: {}")
    else:
        a("proxy-providers:")
        for i, ap in enumerate(airports):
            nm = str(ap.get("name") or ("airport" if i == 0 else "airport%d" % (i + 1)))
            providers.append(nm)
            a("  %s:" % nm)
            a("    type: http")
            a('    url: "%s"' % str(ap.get("url") or "").replace('"', '\\"'))
            a("    interval: %s" % int(ap.get("interval") or 86400))
            a('    health-check: {enable: true, url: "%s", interval: 300}' % HC_URL)
            a("    path: ./providers/%s.yaml" % nm)
    a("")

    # ---- 手动节点
    manual = manual_nodes(s)
    mnames = [str(n.get("name")) for n in manual if n.get("name")]
    a("proxies:")
    a("  - {name: 直连, type: direct}")
    a("  - {name: dns, type: dns}")
    if manual:
        L.extend(ydump(manual, 1))
    a("")

    # ---- 策略组（结构与 CT200 参考实现一致）
    regions = [r for r in (s.get("regions") or []) if r in REGION_MAP]
    saved = s.get("groups") or {}
    dflt = default_groups()
    cgnames = [str(c.get("name")).strip() for c in (s.get("custom_groups") or [])
               if isinstance(c, dict) and str(c.get("name") or "").strip()]

    def _with_cg(mem):
        """把自定义策略组插到「全部自动」之前（地区三件套之后）"""
        out = list(mem)
        if not cgnames:
            return out
        try:
            pos = out.index("全部自动")
        except ValueError:
            pos = len(out)
        return out[:pos] + list(cgnames) + out[pos:]

    base12 = _with_cg(biz_members(regions, ""))
    base13 = [DEFAULT_PROXY] + base12
    groups = []

    # 12 个业务组：直接指向「地区故转 / 手动 / 自动」，默认取本组优先地区的故转
    for name, icon, pref in BIZ_DEFS:
        mem = _with_cg(biz_members(regions, region_label(pref)))
        t = saved.get(name) or dflt[name]
        if t not in mem:
            t = dflt[name]
        if t not in mem:
            t = mem[0]
        groups.append({"name": name, "type": "select", "proxies": mem,
                       "default-selected": t, "icon": ICON_PURE % icon})

    # 全局 / 一键组
    groups.append({"name": "GLOBAL", "type": "select", "proxies": base13,
                   "default-selected": DEFAULT_PROXY, "icon": ICON_PURE % "all"})
    groups.append({"name": "全部手动", "type": "select", "include-all": True,
                   "filter": EXCL, "icon": ICON_PURE % "all"})
    groups.append({"name": "全部自动", "type": "url-test", "url": HC_URL, "interval": 300,
                   "include-all": True, "filter": EXCL, "icon": ICON_PURE % "auto"})
    groups.append({"name": "延迟辅助", "type": "url-test", "url": HC_URL, "interval": 300,
                   "proxies": ["直连", "DIRECT"], "icon": ICON_PURE % "auto"})

    # 地区三件套：手动 / 故转 / 自动
    for r in regions:
        _key, label, flt, flag = REGION_MAP[r]
        for suffix, typ, icon in (("手动", "select", ICON_FLAG % flag),
                                  ("故转", "fallback", ICON_PURE % "failover"),
                                  ("自动", "url-test", ICON_PURE % "auto")):
            g = {"name": label + suffix, "type": typ, "include-all": True,
                 "filter": flt, "icon": icon}
            if typ != "select":
                g["url"] = HC_URL
                g["interval"] = 300
            groups.append(g)

    # ---- 自定义策略组（面板「🧩 自定义策略组」里自由增减）
    known = {g["name"] for g in groups}
    for cg in (s.get("custom_groups") or []):
        if not isinstance(cg, dict):
            continue
        nm = str(cg.get("name") or "").strip()
        if not nm or nm in known:
            continue
        typ = str(cg.get("type") or "select")
        if typ not in ("select", "url-test", "fallback", "load-balance"):
            typ = "select"
        mem = [str(x).strip() for x in (cg.get("members") or []) if str(x).strip()]
        mem = [x for x in mem if x != nm]
        flt = str(cg.get("filter") or "").strip()
        cgd = {"name": nm, "type": typ, "icon": ICON_PURE % "select"}
        if mem:
            cgd["proxies"] = mem
            if flt:
                cgd["filter"] = flt
        else:
            cgd["include-all"] = True
            cgd["filter"] = flt if flt else EXCL
        if typ != "select":
            cgd["url"] = HC_URL
            try:
                cgd["interval"] = int(cg.get("interval") or 300)
            except Exception:
                cgd["interval"] = 300
        groups.append(cgd)
        known.add(nm)

    groups.append({"name": "漏网之鱼", "type": "select", "proxies": base13,
                   "default-selected": DEFAULT_PROXY, "icon": ICON_PURE % "final"})
    groups.append({"name": DEFAULT_PROXY, "type": "select", "proxies": base12,
                   "default-selected": (base12[0] if base12 else "直连"),
                   "icon": ICON_PURE % "select"})
    valid_targets = {g["name"] for g in groups} | {"直连", "DIRECT"}
    a("proxy-groups:")
    L.extend(ydump(groups, 1))
    a("")

    # ---- 规则集分流
    if s.get("ruleset_enabled", True):
        a("rule-anchor:")
        a("  domain: &domain {type: http, interval: 86400, behavior: domain, format: mrs}")
        a("  class: &class {type: http, interval: 86400, behavior: classical, format: text}")
        a("")
        a("rule-providers:")
        for k, fn in RULE_PROVIDERS:
            a('  %s: {<<: *domain, url: "%s"}' % (k, MRS % fn))
        a('  proxylite: {<<: *class, url: "%s"}' % PROXYLITE)
        a("")
    a("rules:")
    rules = [
        "DOMAIN-SUFFIX,msftconnecttest.com,DIRECT",
        "DOMAIN-SUFFIX,msftncsi.com,DIRECT",
        "IP-CIDR,131.107.255.255/32,DIRECT,no-resolve",
        "IP-CIDR6,fd3e:4f5a:5b81::1/128,DIRECT,no-resolve",
        "AND,((NETWORK,UDP),(DST-PORT,53)),dns",
        "GEOIP,private,直连,no-resolve",
    ]
    # 额外直连域名（逗号分隔）：旁路由模式下让订阅/面板/自建服务走直连，别绕代理
    for _d in [x.strip() for x in str(s.get("direct_domains") or "").split(",") if x.strip()]:
        rules.append("DOMAIN-SUFFIX,%s,DIRECT" % _d)
    if s.get("ruleset_enabled", True):
        rt = s.get("rule_targets") or {}

        def _tgt(v, d):
            return v if v in valid_targets else d

        rules.append("RULE-SET,proxylite,%s" % _tgt(rt.get("proxylite"), DEFAULT_PROXY))
        for key, tgt in BIZ_RULES:
            rules.append("RULE-SET,%s,%s" % (key, _tgt(rt.get(key), tgt)))
    rules += ["GEOSITE,cn,直连", "GEOIP,CN,直连", "MATCH,漏网之鱼"]
    for r in rules:
        a("  - %s" % r)
    a("")
    return "\n".join(L)


def gen_config_min(s):
    """最小可用配置（首次启动自举 / 兜底），避免内核无限重启"""
    return "\n".join([
        "# 自举配置：管理台尚未生成正式配置时使用",
        "mixed-port: 7890",
        "allow-lan: true",
        'bind-address: "*"',
        "mode: rule",
        "log-level: info",
        "external-controller: 0.0.0.0:9090",
        "secret: \"%s\"" % str(s.get("secret") or ADMIN_PASS).replace('"', '\\"'),
        "proxy-groups:",
        "  - {name: 默认代理, type: select, proxies: [DIRECT]}",
        "rules:",
        "  - MATCH,DIRECT",
        "",
    ])


# ---------------------------------------------------------------- 运行支撑
def find_docker():
    for c in ("/usr/local/bin/docker", "/usr/bin/docker", "docker"):
        if os.path.sep in c:
            if os.path.exists(c):
                return c
        else:
            w = shutil.which(c)
            if w:
                return w
    return "docker"


DOCKER = find_docker()
RAW_BASE = os.environ.get("RAW_BASE", "https://raw.githubusercontent.com/amw1933/synology-mihomo/main")


def run_cmd(cmd, timeout=60, cwd=None):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except FileNotFoundError as e:  # noqa: BLE001
        return 127, "", "命令不存在：%r" % (e,)
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时（%d 秒）" % timeout
    except Exception as e:  # noqa: BLE001
        return 1, "", "%r" % (e,)


def self_cid():
    """自己在宿主机上的容器 ID（用于显示、以及识别宿主路径）"""
    try:
        h = socket.gethostname()
        if re.match(r"^[0-9a-f]{12,64}$", h):
            return h
    except Exception:
        pass
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r"/([0-9a-f]{64})", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""


def detect_host_base():
    """容器里看到的宿主安装目录（用于 compose / settings-apply）"""
    if HOST_BASE:
        return HOST_BASE
    cid = self_cid()
    if cid:
        rc, out, err = run_cmd([DOCKER, "inspect", "-f",
                                '{{range .Mounts}}{{.Source}}|{{.Destination}}\n{{end}}', cid], 30)
        if rc == 0:
            for line in out.splitlines():
                if "|/data" in line:
                    return line.split("|")[0]
    return BASE


def backup_config():
    try:
        if not os.path.exists(CONFIG):
            return ""
        os.makedirs(BACKUP_DIR, exist_ok=True)
        name = "config-%s.yaml" % datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(BACKUP_DIR, name)
        shutil.copy2(CONFIG, dst)
        files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("config-"))
        for old in files[:-10]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except Exception:
                pass
        return dst
    except Exception as e:  # noqa: BLE001
        log("备份配置失败: %r" % (e,))
        return ""


def validate_config():
    """docker exec mihomo <mihomo> -t ...（官方镜像里内核在 /mihomo，不在 PATH）"""
    last = ""
    for b in ("/mihomo", "mihomo"):
        rc, out, err = run_cmd([DOCKER, "exec", CT_KERNEL, b, "-t",
                                "-d", KERNEL_DIR, "-f", KERNEL_CFG], timeout=90)
        txt = (out + "\n" + err).strip()
        low = txt.lower()
        if "命令不存在" in txt:
            return False, "管理台容器里没有 docker 命令，无法校验配置"
        if "no such container" in low or "is not running" in low:
            return False, "内核容器 %s 未运行，无法校验配置" % CT_KERNEL
        if "executable file not found" in low or "no such file or directory" in low:
            last = txt
            continue
        if rc == 0 and "test failed" not in low:
            return True, txt or "校验通过"
        return False, txt or ("校验命令返回码 %d" % rc)
    return False, last or "找不到内核可执行文件"


def ctrl_base(s):
    host = s.get("admin_bind") or ADMIN_BIND
    if host in ("", "0.0.0.0", "::", "::0"):
        host = "127.0.0.1"
    port = str(s.get("panel_port") or "9091")
    return "http://%s:%s" % (host, port)


def ctrl_base(s):
    """内核控制接口固定走本机回环：9091 只监听 127.0.0.1，不对外。
    （「绑定 IP」只用于显示/提示，不能拿它去连内核，否则面板接口会 502）"""
    return "http://127.0.0.1:%s" % str(s.get("panel_port") or "9091")


def ctrl_get(s, path, timeout=8):
    req = urllib.request.Request(ctrl_base(s) + path)
    req.add_header("Authorization", "Bearer %s" % (s.get("secret") or ADMIN_PASS))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, "%r" % (e,)


def ctrl_put(s, path, obj, timeout=8):
    req = urllib.request.Request(ctrl_base(s) + path,
                                data=json.dumps(obj).encode("utf-8"), method="PUT")
    req.add_header("Authorization", "Bearer %s" % (s.get("secret") or ADMIN_PASS))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, "%r" % (e,)


def sync_group_selections(s=None):
    """只同步「在管理台里被改过」的策略组，做到「谁最后动就以谁为准」。

    内核开了 store-selected，会记住在面板里手点过的线路（存 cache.db，重启也在）。
    所以这里绝不能每次把全部组都推一遍 —— 那会把面板里的选择冲掉。
    做法：跟上次同步过的值比，只有管理台改过的组才推。
    """
    s = s or load_settings()
    gs = s.get("groups") or {}
    applied = dict(s.get("groups_applied") or {})
    ok, fail, skip = [], [], 0
    for name, want in gs.items():
        if not want or want == applied.get(name):
            skip += 1
            continue
        code, body = ctrl_put(s, "/proxies/%s" % urllib.parse.quote(name), {"name": want})
        if code in (200, 204):
            ok.append("%s→%s" % (name, want))
            applied[name] = want
        else:
            fail.append("%s→%s(%s)" % (name, want, code))
    if ok:
        s["groups_applied"] = applied
        try:
            save_settings(s)
        except Exception as e:  # noqa: BLE001
            log("记录 groups_applied 失败：%r" % (e,))
    return ok, fail, skip


def reload_config(s):
    """PUT /configs?force=true 让内核热重载"""
    url = ctrl_base(s) + "/configs?force=true"
    body = json.dumps({"path": KERNEL_CFG}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Authorization", "Bearer %s" % (s.get("secret") or ADMIN_PASS))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, r.read().decode("utf-8", "replace") or "ok"
    except Exception as e:  # noqa: BLE001
        return False, "%r" % (e,)


def restart_kernel():
    rc, out, err = run_cmd([DOCKER, "restart", CT_KERNEL], timeout=180)
    if rc == 0:
        return True, out.strip() or "已重启"
    return False, (err or out).strip()


# ---------------------------------------------------------------- 内核版本检测 / 一键更新
KERNEL_IMAGE = "metacubex/mihomo:latest"
_KLV = {"latest": "", "html": "", "ts": 0.0, "err": "", "busy": False}


def _ver_tuple(v):
    """'v1.19.34' -> (1,19,34)；解析不出来就返回空元组（不误报有更新）"""
    m = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in m[:4]) if m else ()


def kernel_latest(force=False):
    """后台线程查 GitHub 最新内核版本，缓存 6 小时；绝不阻塞页面请求。"""
    if _KLV.get("busy"):
        return
    age = time.time() - float(_KLV.get("ts") or 0)
    if not force and _KLV.get("latest") and age < 6 * 3600:
        return
    _KLV["busy"] = True

    def _work():
        urls = [
            "https://gh-proxy.com/https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
            "https://ghfast.top/https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
            "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
        ]
        try:
            for u in urls:
                data, err = http_fetch(u, timeout=15, tries=1)
                if not data:
                    _KLV["err"] = err
                    continue
                try:
                    d = json.loads(data.decode("utf-8", "replace"))
                except Exception as e:  # noqa: BLE001
                    _KLV["err"] = repr(e)
                    continue
                tag = (d.get("tag_name") or "").strip()
                if tag:
                    _KLV.update({"latest": tag, "html": d.get("html_url") or "",
                                 "ts": time.time(), "err": ""})
                    log("检测到内核最新版本：%s" % tag)
                    break
        finally:
            _KLV["busy"] = False

    threading.Thread(target=_work, daemon=True).start()


def update_kernel():
    """拉最新内核镜像 + 用 compose 重建内核容器（配置 / 线路 / 端口 / TUN 能力都不动）"""
    if not _UPD_LOCK.acquire(timeout=900):
        return {"ok": False, "error": "已有任务在执行，请稍候再试"}
    try:
        log("开始更新内核：拉取 %s" % KERNEL_IMAGE)
        rc, out, err = run_cmd([DOCKER, "pull", KERNEL_IMAGE], timeout=1800)
        if rc != 0:
            msg = (err or out).strip()[:300]
            log("内核镜像拉取失败：%s" % msg)
            return {"ok": False, "error": "拉取镜像失败：" + msg}
        log("内核镜像拉取完成")
        path = ""
        for cand in ("compose.yaml", "docker-compose.yml", "docker-compose.yaml"):
            p2 = os.path.join(BASE, cand)
            if os.path.exists(p2):
                path = p2
                break
        if not path:
            return {"ok": False, "error": "没找到 compose 文件，无法自动重建内核容器，请在 SSH 里手动执行 compose pull && up -d"}
        rc, out, err = run_cmd([DOCKER, "compose", "-f", path, "up", "-d", "--force-recreate", CT_KERNEL],
                               timeout=600)
        tail = " | ".join((out or err).strip().splitlines()[-4:])
        log("内核容器重建：rc=%s %s" % (rc, tail))
        if rc != 0:
            return {"ok": False, "error": "重建内核容器失败：" + (err or out).strip()[:300]}
        _KLV["ts"] = 0.0          # 让下次状态刷新重新检测版本
        time.sleep(3)
        code, body = ctrl_get(load_settings(), "/version")
        ver = (jloads(body, {}) or {}).get("version") or "" if code == 200 else ""
        return {"ok": True, "msg": "内核已更新到最新镜像并重建完成" + ("（当前 %s）" % ver if ver else "")}
    finally:
        _UPD_LOCK.release()


def update_config(s=None):
    """写配置 -> 校验 -> 热重载 -> 兜底重启（全程加锁串行）"""
    if not _UPD_LOCK.acquire(timeout=300):
        return {"ok": False, "error": "已有一个更新任务在进行，请稍候再试"}
    try:
        s = s or load_settings()
        for d in (ETC, PROVIDERS, UI_DIR, BACKUP_DIR):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        text = gen_config(s)
        bak = backup_config()
        tmp = CONFIG + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, CONFIG)
        log("生成 config.yaml：%d 字节（备份 %s）" % (len(text), os.path.basename(bak) or "无"))
        ok, out = validate_config()
        if not ok:
            # 校验没过：把刚写进去的坏配置回滚成上一份，别让内核下次重启起不来
            if bak and os.path.exists(bak):
                try:
                    shutil.copy2(bak, CONFIG)
                    log("配置校验失败，已回滚到 %s" % os.path.basename(bak))
                except Exception as e:  # noqa: BLE001
                    log("回滚失败：%r" % (e,))
            log("配置校验失败：%s" % out[-300:])
            return {"ok": False, "error": "配置校验未通过（已保留上一份可用配置）：%s" % out[-400:]}
        rok, rout = reload_config(s)
        if rok:
            ok_g, fail_g, skip_g = sync_group_selections(s)
            log("热重载成功；默认线路已同步 %d 个（未变 %d 个）%s"
                % (len(ok_g), skip_g, ("，失败：" + "、".join(fail_g)) if fail_g else ""))
            return {"ok": True, "msg": "配置已更新并热重载生效（改了 %d 个默认线路）" % len(ok_g),
                    "detail": out[-300:], "groups_failed": fail_g}
        r2ok, r2out = restart_kernel()
        if r2ok:
            ok_g, fail_g, skip_g = sync_group_selections(s)
            log("热重载失败，已重启内核：%s" % rout[:200])
            return {"ok": True, "msg": "热重载失败，已重启内核容器（改了 %d 个默认线路）" % len(ok_g),
                    "warn": rout[:200], "groups_failed": fail_g}
        log("热重载与重启均失败")
        return {"ok": False, "error": "热重载失败且重启失败：%s / %s" % (rout[:200], r2out[:200])}
    except Exception as e:  # noqa: BLE001
        log("更新配置异常：%r" % (e,))
        return {"ok": False, "error": "更新失败：%r" % (e,)}
    finally:
        _UPD_LOCK.release()


def container_state(name):
    rc, out, err = run_cmd([DOCKER, "ps", "-a", "--filter", "name=^/%s$" % name,
                            "--format", "{{.Status}}"], timeout=30)
    if rc != 0:
        return "unknown", (err or out).strip()
    st = out.strip()
    return ("up" if st.lower().startswith("up") else "down") if st else "missing", st or "未找到该容器"


def guess_host(s):
    """猜一个能用的宿主地址（用于健康自检 / 状态页），不用用户填 IP"""
    for c in (s.get("proxy_ip"), s.get("admin_bind"),
              (PROXY_ADDR.split(":")[0] if PROXY_ADDR else ""),
              detect_ip()):
        if c and str(c) not in ("0.0.0.0", "::", "::0", "127.0.0.1"):
            return str(c)
    return "127.0.0.1"


def check_proxy(s):
    host = guess_host(s)
    port = str(s.get("proxy_port") or "7890")
    if not host:
        return False, "未设置代理地址"
    proxy = "http://%s:%s" % (host, port)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with op.open("https://www.gstatic.com/generate_204", timeout=15) as r:
            return r.status in (200, 204), "经 %s 返回 HTTP %s" % (proxy, r.status)
    except Exception as e:  # noqa: BLE001
        return False, "经 %s 连接失败：%r" % (proxy, e)


def health(s=None):
    s = s or load_settings()
    items = []
    st, detail = container_state(CT_KERNEL)
    items.append({"name": "内核容器运行", "ok": st == "up", "detail": detail})
    vok, vout = validate_config()
    items.append({"name": "配置有效", "ok": vok, "detail": vout[-160:]})
    pok, pout = check_proxy(s)
    items.append({"name": "代理可用", "ok": pok, "detail": pout[:160]})
    code, _ = ctrl_get(s, "/version")
    items.append({"name": "面板接口", "ok": code == 200, "detail": "HTTP %s %s" % (code, ctrl_base(s))})
    try:
        du = shutil.disk_usage(BASE)
        free_gb = du.free / 1073741824.0
        items.append({"name": "磁盘空间", "ok": du.free > 512 * 1024 * 1024,
                      "detail": "剩余 %.1f GB" % free_gb})
    except Exception as e:  # noqa: BLE001
        items.append({"name": "磁盘空间", "ok": False, "detail": "%r" % (e,)})
    # 程序文件：兼容两种部署方式（SSH 有 compose.yaml+admin/，Container Manager 是 docker-compose.yml+镜像内置）
    cfg_ok = os.path.exists(CONFIG)
    compose_ok = any(os.path.exists(os.path.join(BASE, n)) for n in
                     ("compose.yaml", "docker-compose.yml", "docker-compose.yaml"))
    parts = ["config.yaml" if cfg_ok else "缺少 config.yaml",
             "compose 文件" if compose_ok else "缺少 compose 文件",
             "admin/app.py" if os.path.exists(os.path.join(BASE, "admin", "app.py"))
             else "管理台代码镜像内置"]
    items.append({"name": "程序文件", "ok": cfg_ok and compose_ok, "detail": " + ".join(parts)})
    sch = bool(s.get("schedule_enabled"))
    items.append({"name": "定时更新", "ok": sch, "detail": "每天 %s（上次 %s）"
                  % (s.get("schedule_time") or "04:00", s.get("last_run") or "从未")})
    okn = sum(1 for i in items if i["ok"])
    return {"ok": okn == len(items), "passed": okn, "total": len(items),
            "items": items, "time": now()}


def api_status(s=None):
    s = s or load_settings()
    st, detail = container_state(CT_KERNEL)
    ast, adetail = container_state("mihomo-admin")
    ver = ""
    code, body = ctrl_get(s, "/version")
    if code == 200:
        d = jloads(body, {})
        ver = (d or {}).get("version") or ""
    prov = []
    code, body = ctrl_get(s, "/providers/proxies")
    if code == 200:
        d = jloads(body, {}) or {}
        for k, v in (d.get("providers") or {}).items():
            prov.append({"name": k, "nodes": len(v.get("proxies") or []),
                         "updated": v.get("updatedAt") or ""})
    manual = manual_nodes(s)
    out_ip = ""
    try:
        host = guess_host(s)
        proxy = "http://%s:%s" % (host, s.get("proxy_port") or "7890")
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        with op.open("https://api.ipify.org", timeout=10) as r:
            out_ip = r.read().decode("utf-8", "replace").strip()
    except Exception:
        out_ip = ""
    kernel_latest()          # 后台查最新版本（缓存 6 小时，不阻塞）
    _latest = _KLV.get("latest") or ""
    _cur, _new = _ver_tuple(ver), _ver_tuple(_latest)
    return {
        "kernel": {"name": CT_KERNEL, "state": st, "detail": detail, "version": ver},
        "kernel_update": {"latest": _latest, "has_update": bool(_cur and _new and _new > _cur),
                          "url": _KLV.get("html") or "", "checked": bool(_KLV.get("ts")),
                          "error": _KLV.get("err") or ""},
        "admin": {"name": "mihomo-admin", "state": ast, "detail": adetail},
        "airports": s.get("airports") or [],
        "providers": prov,
        "manual_count": len(manual),
        "regions": s.get("regions") or [],
        "proxy_addr": "%s:%s" % (guess_host(s), s.get("proxy_port") or "7890"),
        "panel_url": PANEL_URL or "/ui/",                 # 面板由管理台同端口提供
        "panel_url_full": "http://%s:%s/ui/" % (guess_host(s), s.get("admin_port") or ADMIN_PORT),
        "out_ip": out_ip,
        "weak_pass": str(s.get("secret") or "") in ("", "admin"),
        "settings": s,
        "time": now(),
    }


def http_fetch(url, timeout=60, tries=2):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mihomo-admin/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), ""
        except Exception as e:  # noqa: BLE001
            last = e
    return None, "%r" % (last,)


def install_panel():
    """下载 metacubexd（compressed-dist.tgz 或 zip）并解到 etc/ui/"""
    asset = "compressed-dist.tgz"
    urls = [
        # 代理镜像优先：群晖在国内直连 GitHub 常常卡死，直连放最后兜底
        "https://gh-proxy.com/https://github.com/MetaCubeX/metacubexd/releases/latest/download/" + asset,
        "https://ghfast.top/https://github.com/MetaCubeX/metacubexd/releases/latest/download/" + asset,
        "https://ghproxy.net/https://github.com/MetaCubeX/metacubexd/releases/latest/download/" + asset,
        "https://github.com/MetaCubeX/metacubexd/releases/latest/download/" + asset,
        "https://github.com/MetaCubeX/metacubexd/releases/latest/download/metacubexd.zip",
    ]
    data = None
    err = ""
    for u in urls:
        data, err = http_fetch(u, timeout=60)
        if data and len(data) > 20000:
            log("面板下载成功：%s（%d 字节）" % (u, len(data)))
            break
        data = None
    if not data:
        log("面板下载失败：%s" % err)
        return {"ok": False, "error": "面板下载失败（代理镜像与 GitHub 直连都试过了）：%s" % err}
    try:
        os.makedirs(UI_DIR, exist_ok=True)
        pairs = []          # [(相对路径, 原始文件名)]
        if data[:2] == b"\x1f\x8b":      # tar.gz
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
            members = [m for m in tf.getmembers() if m.isfile()]
            tops = set(m.name.split("/")[0] for m in members)
            prefix = ""
            if len(tops) == 1 and "/" in members[0].name:
                prefix = members[0].name.split("/")[0] + "/"
            blob = io.BytesIO(data)
            for m in members:
                rel = m.name[len(prefix):] if prefix and m.name.startswith(prefix) else m.name
                if not rel or ".." in rel:
                    continue
                src = tf.extractfile(m)
                if src is None:
                    continue
                pairs.append((rel, src.read()))
        else:                            # zip
            zf = zipfile.ZipFile(io.BytesIO(data))
            names = [n for n in zf.namelist() if not n.endswith("/")]
            prefix = ""
            if names and "/" in names[0] and "index.html" not in names:
                prefix = names[0].split("/")[0] + "/"
            for n in names:
                rel = n[len(prefix):] if prefix and n.startswith(prefix) else n
                if not rel or ".." in rel:
                    continue
                pairs.append((rel, zf.read(n)))
        n = 0
        for rel, content in pairs:
            dst = os.path.join(UI_DIR, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst) or UI_DIR, exist_ok=True)
            with open(dst, "wb") as f:
                f.write(content)
            n += 1
        ok = os.path.exists(os.path.join(UI_DIR, "index.html"))
        log("面板解包 %d 个文件，index.html=%s" % (n, ok))
        msg = "面板已安装到 etc/ui/（%d 个文件）" % n
        if ok:
            # mihomo 只在启动时注册 external-ui，装完面板要重启一次内核才生效
            rok, rout = restart_kernel()
            msg += ("，已重启内核使面板生效" if rok
                    else "；重启内核失败（%s），请手动重启" % str(rout)[:120])
        return {"ok": ok, "files": n, "msg": msg}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "面板解包失败：%r" % (e,)}


def sync_scripts():
    """从仓库同步 settings-apply.sh / dsm-fix.sh 等（带时间戳防 CDN 缓存）"""
    res = []
    ts = int(time.time())
    for name in ("settings-apply.sh", "dsm-fix.sh", "compose.yaml"):
        data, err = http_fetch("%s/%s?t=%d" % (RAW_BASE.rstrip("/"), name, ts), timeout=30)
        if not data:
            res.append({"name": name, "ok": False, "error": err})
            continue
        try:
            dst = os.path.join(BASE, name)
            with open(dst, "wb") as f:
                f.write(data)
            if name.endswith(".sh"):
                os.chmod(dst, 0o755)
            res.append({"name": name, "ok": True, "bytes": len(data)})
        except Exception as e:  # noqa: BLE001
            res.append({"name": name, "ok": False, "error": "%r" % (e,)})
    return {"ok": all(r["ok"] for r in res), "items": res}


def fetch_app_py():
    """下载仓库里的 admin/app.py（带国内镜像兜底，返回 (bytes, err)）"""
    import hashlib as _hl
    ts = int(time.time())
    rel = "admin/app.py?t=%d" % ts
    urls = [
        "%s/%s" % (RAW_BASE.rstrip("/"), rel),
        "https://gh-proxy.com/https://raw.githubusercontent.com/amw1933/synology-mihomo/main/" + rel,
        "https://ghfast.top/https://raw.githubusercontent.com/amw1933/synology-mihomo/main/" + rel,
        "https://ghproxy.net/https://raw.githubusercontent.com/amw1933/synology-mihomo/main/" + rel,
        "https://cdn.jsdelivr.net/gh/amw1933/synology-mihomo@main/admin/app.py?t=%d" % ts,
    ]
    err = ""
    for u in urls:
        data, e = http_fetch(u, timeout=60)
        if data and len(data) > 50000:
            txt = data.decode("utf-8", "replace")
            if "def main(" in txt and "proxy-groups" in txt:
                return data, ""
            e = "内容不像 app.py（%d 字节）" % len(data)
        err = e or ("%d 字节" % len(data or b""))
    return None, err


def _self_restart():
    """重启管理台（改完自己的程序文件后生效）

    注意：在容器内部调用 `docker restart` 会被自己的 stop 阶段杀掉，
    结果容器停在 Exited 不回来（踩过这个坑）。所以这里优先"退出进程"，
    由 Docker 的 restart 策略(unless-stopped / always)自动拉起；
    只有当策略是 no 时才退而求其次去调 docker restart。
    """
    time.sleep(2)
    policy = ""
    try:
        rc, out, err = run_cmd([DOCKER, "inspect", APP_NAME,
                                "--format", "{{.HostConfig.RestartPolicy.Name}}"], timeout=30)
        if rc == 0:
            policy = (out or "").strip()
    except Exception:
        policy = ""
    log("重启管理台：restart 策略=%s" % (policy or "unknown"))
    if policy in ("unless-stopped", "always"):
        os._exit(1)          # 交给 Docker 自动重启（干净可靠）
    try:
        run_cmd([DOCKER, "restart", APP_NAME], timeout=90)
    except Exception as e:  # noqa: BLE001
        log("重启管理台异常：%r" % (e,))
    time.sleep(3)
    os._exit(1)


def self_update(apply=True):
    """从仓库拉最新管理台程序（admin/app.py）：语法校验 -> 备份 -> 替换 -> 自动重启面板。

    apply=False 时只做检查，不写任何文件。
    """
    data, err = fetch_app_py()
    if not data:
        log("更新管理台程序失败：下载不到最新文件（%s）" % err)
        return {"ok": False, "error": "下载失败：%s" % err}
    src = data.decode("utf-8", "replace")
    cur_path = os.path.abspath(__file__)
    try:
        with open(cur_path, "r", encoding="utf-8", errors="replace") as f:
            cur = f.read()
    except Exception:
        cur = ""
    note = ""
    cur_bytes = len(cur.encode("utf-8"))
    # ① 先看是不是本来就已经一样了 —— 这种情况直接说清楚，不做任何写入
    if cur and cur.strip() == src.strip():
        return {"ok": True, "msg": "已经是最新版本（%d 字节）" % len(data)}
    if len(data) < cur_bytes:
        note = "（注意：仓库这版比当前小 %d 字节，可能是旧版）" % (cur_bytes - len(data))
    # ② 再校验下载到的东西像不像我们的程序
    try:
        compile(src, "app.py", "exec")
    except SyntaxError as e:
        log("更新管理台程序失败：新版本语法检查未通过")
        return {"ok": False, "error": "新版本语法检查未通过，已放弃：%s" % e}
    for mark in ("LOGIN_PAGE", "def main(", "proxy-groups"):
        if mark not in src:
            log("更新管理台程序失败：新版本内容不完整（缺少 %s），已放弃" % mark)
            return {"ok": False, "error": "新版本内容不完整（缺少 %s），已放弃" % mark}
    if not apply:
        return {"ok": True,
                "msg": "检查通过：可以更新到 %d 字节（当前 %d 字节）%s，尚未写入任何文件" % (len(data), cur_bytes, note),
                "detail": {}}
    ts = time.strftime("%Y%m%d-%H%M%S")
    bp = ""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        bp = os.path.join(BACKUP_DIR, "app-%s.py" % ts)
        with open(bp, "w", encoding="utf-8", newline="") as f:
            f.write(cur)
    except Exception as e:  # noqa: BLE001
        log("备份管理台程序失败：%r" % (e,))
    try:
        tmp = cur_path + ".new"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(src)
        os.replace(tmp, cur_path)
        try:
            with open(os.path.join(BASE, "app.py.orig"), "w", encoding="utf-8", newline="") as f:
                f.write(src)
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "写入失败：%r" % (e,)}
    log("管理台程序已更新：%d -> %d 字节（备份 %s）" % (cur_bytes, len(data), os.path.basename(bp) or "无"))
    try:
        _s2 = load_settings()
        _sha = repo_latest_sha()
        if _sha:
            _s2["auto_update_last"] = _sha
        _s2["update_available"] = ""
        save_settings(_s2)   # 现在判断以文件内容为准，这里只是留个记录
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=_self_restart, daemon=True).start()
    return {"ok": True,
            "msg": "已更新到最新版（%d 字节）%s，正在重启管理台，约 8 秒后自动刷新页面" % (len(data), note),
            "detail": {"backup": os.path.basename(bp)}}


def repo_latest_sha():
    """取仓库里 admin/app.py 的最新提交号（用于判断有没有新版本）"""
    try:
        u = "https://api.github.com/repos/amw1933/synology-mihomo/commits?path=admin/app.py&per_page=1"
        data, err = http_fetch(u, timeout=30)
        if not data:
            return ""
        j = jloads(data.decode("utf-8", "replace"), None)
        if isinstance(j, list) and j:
            return str(j[0].get("sha") or "")
    except Exception as e:  # noqa: BLE001
        log("查询仓库版本失败：%r" % (e,))
    return ""


def update_check_once():
    """检查仓库有没有新版：按【文件内容】判断（比对 md5），只写提示标记，不下载不安装"""
    s = load_settings()
    if not s.get("auto_update_panel", True):
        return {"ok": True, "enabled": False, "msg": "自动检查更新已关闭"}
    data, err = fetch_app_py()
    if not data:
        log("更新检查：下载失败（%s）" % err)
        return {"ok": False, "error": "读取仓库版本失败：%s" % err}
    try:
        cur = open(os.path.abspath(__file__), "rb").read()
    except Exception:
        cur = b""
    if data == cur:
        if s.get("update_available"):
            s["update_available"] = ""
            save_settings(s)
        return {"ok": True, "available": False, "msg": "已是最新（%d 字节）" % len(cur)}
    import hashlib as _hl
    mark = _hl.md5(data).hexdigest()[:8]
    s["update_available"] = mark
    save_settings(s)
    log("更新检查：发现新版（仓库 %d 字节 %s / 本地 %d 字节）" % (len(data), mark, len(cur)))
    return {"ok": True, "available": True, "sha": mark,
            "msg": "发现新版 %s —— 点「更新管理台程序」安装" % mark}


def auto_update_loop():
    """后台线程：每 6 小时检查一次有没有新版（只提示）"""
    time.sleep(90)
    while True:
        try:
            r = update_check_once()
            if not r.get("ok"):
                log("更新检查失败：%s" % r.get("error"))
        except Exception as e:  # noqa: BLE001
            log("更新检查线程异常：%r" % (e,))
        time.sleep(6 * 3600)


def bootstrap():
    """启动自举：目录 + settings + 最小配置 + sub.url 导入"""
    for d in (BASE, ETC, PROVIDERS, UI_DIR, BACKUP_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    s = load_settings()
    if not os.path.exists(SETTINGS_PATH):
        save_settings(s)
        log("已初始化 settings.json")
    if not os.path.exists(CONFIG):
        with open(CONFIG, "w", encoding="utf-8") as f:
            f.write(gen_config_min(s))
        log("未发现 config.yaml，已写入自举配置")
    return s


def scheduler_loop():
    last_day = ""
    while True:
        try:
            s = load_settings()
            if s.get("schedule_enabled"):
                t = (s.get("schedule_time") or "04:00").strip()
                cur = datetime.now().strftime("%H:%M")
                day = datetime.now().strftime("%Y-%m-%d")
                if cur == t and last_day != day:
                    last_day = day
                    log("定时更新开始（%s）" % t)
                    r = update_config(s)
                    s["last_run"] = now()
                    save_settings(s)
                    log("定时更新结果：%s" % json.dumps(r, ensure_ascii=False)[:300])
        except Exception as e:  # noqa: BLE001
            log("定时线程异常：%r" % (e,))
        time.sleep(45)


# ---------------------------------------------------------------- 会话 / 鉴权
def _auth_blank(v):
    """请求头里的 Authorization 为空 / 只有 'Bearer' 而没带 token 时，视为"没带认证"。

    面板零配置模式下 secret 传空串，会发出一个没有 token 的 Authorization，
    这种情况也要让管理台按会话把真正的密钥代填进去。
    """
    v = (v or "").strip()
    return (not v) or v.lower() in ("bearer", "bearer:", "token", "null", "undefined")


def _eq(a, b):
    a, b = str(a or ""), str(b or "")
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a.encode("utf-8"), b.encode("utf-8")):
        r |= x ^ y
    return r == 0


def current_pass():
    """当前生效的密码：以 settings.secret 为准（网页里改的才算数），没写过就用环境变量"""
    try:
        return str(load_settings().get("secret") or ADMIN_PASS)
    except Exception:
        return ADMIN_PASS


def sess_new():
    tok = os.urandom(16).hex()
    with _SESSION_LOCK:
        _SESSIONS[tok] = time.time() + 7 * 86400
    return tok


def remove_node(s, name):
    keep, removed = [], False
    for line in s.get("nodes") or []:
        match = False
        if not removed and sub_convert is not None:
            try:
                got = sub_convert.to_mihomo(line)
                match = bool(got) and str(got[0].get("name")) == str(name)
            except Exception:
                match = False
        if match:
            removed = True
            continue
        keep.append(line)
    s["nodes"] = keep
    return removed


def _default_gw():
    """从 /proc/net/route 取默认网关（hex 小端）"""
    try:
        with open("/proc/net/route", "r") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 3 and p[1] == "00000000" and (int(p[3], 16) & 2):
                    h = p[2]
                    return ".".join(str(int(h[i:i + 2], 16)) for i in (6, 4, 2, 0))
    except Exception:
        pass
    return ""


def _bad_ip(ip):
    """TUN/fake-ip/回环等都不是宿主真实地址（旁路由模式下会出现 198.18.x）"""
    if not ip or ip == "0.0.0.0":
        return True
    return ip.startswith(("127.", "169.254.", "198.18.", "198.19."))


SIOCGIFADDR = 0x8915


def _iface_ips():
    """枚举宿主网卡 IPv4（管理台是 host 网络，能直接看到宿主网卡）"""
    out = []
    try:
        import fcntl
        import struct
        for _, name in socket.if_nameindex():
            if name.startswith(("lo", "docker", "veth", "tun", "br-", "virbr")):
                continue
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                r = fcntl.ioctl(sk.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
                ip = socket.inet_ntoa(r[20:24])
                if not _bad_ip(ip):
                    out.append((name, ip))
            except Exception:
                pass
            finally:
                sk.close()
    except Exception:
        pass
    return out


def detect_ip():
    """识别宿主 LAN IP。
    旁路由（TUN）模式下往外探测，源地址会变成隧道地址（198.18.x），
    所以优先直接读宿主网卡；并按「与默认网关同网段」优先挑（避开 docker / VPN 网段）。
    """
    gw = _default_gw()
    cands = _iface_ips()
    if gw:
        pre = ".".join(gw.split(".")[:3]) + "."
        for _, ip in cands:
            if ip.startswith(pre):
                return ip
    if cands:
        return cands[0][1]
    for probe in ([gw] if gw else []) + ["1.1.1.1", "223.5.5.5"]:
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.settimeout(3)
            sk.connect((probe, 80))
            ip = sk.getsockname()[0]
            sk.close()
            if not _bad_ip(ip):
                return ip
        except Exception:
            pass
    return ""


def apply_ports(s):
    """端口/绑定地址改动后应用：能自己跑就跑 settings-apply.sh，否则给出手动命令"""
    host_base = detect_host_base()
    script = os.path.join(BASE, "settings-apply.sh")
    if os.path.exists(script):
        rc, out, err = run_cmd(["sh", script], timeout=300, cwd=BASE)
        if rc == 0:
            return {"ok": True, "msg": "已应用并重建容器", "detail": (out or err)[-400:]}
        tip = "sh %s" % script
        return {"ok": False, "manual": True,
                "error": "自动重建失败（管理台容器里没有 docker CLI）：%s" % (err or out)[-200:],
                "hint": "请在群晖 SSH 里执行：sudo sh %s/settings-apply.sh" % host_base,
                "cmd": tip}
    return {"ok": False, "manual": True,
            "error": "没找到 settings-apply.sh",
            "hint": "请在群晖 SSH 里执行：sudo sh %s/settings-apply.sh" % host_base}


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "mihomo-admin/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):      # 用自家日志，不往 stderr 刷
        pass

    # ---- 输出
    def _send(self, data, ctype, code=200, headers=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code=200, headers=None):
        self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", code, headers)

    def _html(self, text, code=200):
        self._send(text, "text/html; charset=utf-8", code)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return b""
        try:
            return self.rfile.read(min(n, 4 * 1024 * 1024))
        except Exception:
            return b""

    def _payload(self):
        raw = self._read_body()
        d = jloads(raw, None)
        if isinstance(d, dict):
            return d
        if isinstance(d, list):
            return {"items": d}
        try:
            return {k: v[-1] for k, v in urllib.parse.parse_qs(
                raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
        except Exception:
            return {}

    def _args(self):
        q = urllib.parse.urlsplit(self.path).query
        return {k: v[-1] for k, v in urllib.parse.parse_qs(q, keep_blank_values=True).items()}

    # ---- 面板静态资源：由管理台自己发（对外只占一个端口）
    def _serve_ui(self, p):
        rel = urllib.parse.unquote(p[len("/ui"):]).lstrip("/") or "index.html"
        if rel == "config.js":
            host = self.headers.get("Host") or "127.0.0.1"
            base = "http://%s/clash" % host
            # defaultBackendURL：setup 页里预填的地址
            # endpoint：metacubexd 内建的"零配置"入口 —— 会自动添加并选中该后端，
            #           直接进面板主页，用户不用填地址/密钥（密钥留空，由管理台按会话代填）
            js = ("window.__METACUBEXD_CONFIG__={defaultBackendURL:'%s',"
                  "endpoint:{url:'%s',secret:''},githubToken:''};\n" % (base, base))
            return self._send(js, "application/javascript; charset=utf-8")
        root = os.path.normpath(UI_DIR)
        full = os.path.normpath(os.path.join(root, rel.replace("/", os.sep)))
        if not full.startswith(root) or not os.path.isfile(full):
            return self._send("404 not found", "text/plain; charset=utf-8", 404)

        # index.html：注入"零配置"脚本 —— 在面板启动之前，把后端地址写进 localStorage，
        # 这样打开 /ui/ 就直接进面板，不用填地址、不用填密钥。
        # · 只在用户还没自己配过后端时才写（不会覆盖用户的选择）
        # · 密钥故意留空：管理台会按登录会话把真密钥代填（见 _auth_blank）
        # · 键名写了多套（endpoint:xxx 与 xxx）—— 面板不同版本的键名前缀不一样，多写几个不影响
        if rel in ("index.html", ""):
            host = self.headers.get("Host") or "127.0.0.1"
            base = "http://%s/clash" % host
            inject = (
                "<script>(function(){try{var L=window.localStorage;var url='%s';"
                # ① 先把后端写进 localStorage（面板若认，就直接进主页）
                "try{if(!L.getItem('endpoint:selectedEndpoint')&&!L.getItem('selectedEndpoint')){"
                "var ep={id:'mh-auto',url:url,secret:'',label:'本机（自动）'};"
                "var list=JSON.stringify([ep]);var sid=JSON.stringify('mh-auto');"
                "L.setItem('endpoint:endpointList',list);L.setItem('endpoint:selectedEndpoint',sid);"
                "L.setItem('endpointList',list);L.setItem('selectedEndpoint',sid);"
                "L.setItem('endpoint',JSON.stringify({selectedEndpoint:'mh-auto',endpointList:[ep]}));}}catch(e){}"
                # ② 兜底：如果面板仍停在设置页（有「连接」按钮），就自动填好地址并点连接
                "function sv(el,v){try{var d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
                "if(d&&d.set){d.set.call(el,v);}else{el.value=v;}"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));}catch(e){}}"
                "function mhfill(){try{var bs=document.querySelectorAll('button'),go=null,i;"
                "for(i=0;i<bs.length;i++){if((bs[i].textContent||'').trim()==='连接'){go=bs[i];break;}}"
                "if(!go)return false;"
                "var ins=document.querySelectorAll('input'),addr=null,sec=null;"
                "for(i=0;i<ins.length;i++){var t=((ins[i].placeholder||'')+(ins[i].name||'')+(ins[i].id||'')).toLowerCase();"
                "if(t.indexOf('secret')>=0){sec=ins[i];}else if(t.indexOf('host')>=0||t.indexOf('port')>=0||t.indexOf('http')>=0){addr=ins[i];}}"
                "if(!addr&&ins.length){addr=ins[0];}"
                "if(addr){sv(addr,url);}if(sec){sv(sec,'');}"
                "setTimeout(function(){try{go.click();}catch(e){}},200);"
                "return true;}catch(e){return false;}}"
                "function mhboot(){if(mhfill()){return;}var n=0;var t=setInterval(function(){n++;if(mhfill()||n>25){clearInterval(t);}},600);}"
                "if(document.readyState!=='loading'){mhboot();}else{document.addEventListener('DOMContentLoaded',mhboot);}"
                "}catch(e){}})();</script>" % base)
            try:
                with open(full, "rb") as f:
                    html = f.read().decode("utf-8", "replace")
                i = html.find("<head>")
                html = (html[:i + 6] + inject + html[i + 6:]) if i >= 0 else (inject + html)
                return self._send(html, "text/html; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                return self._send("读取失败：%r" % e, "text/plain; charset=utf-8", 500)

        ct = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
              ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
              ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
              ".map": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8"}.get(
            os.path.splitext(full)[1].lower(), "application/octet-stream")
        try:
            with open(full, "rb") as f:
                return self._send(f.read(), ct)
        except Exception as e:  # noqa: BLE001
            return self._send("读取失败：%r" % e, "text/plain; charset=utf-8", 500)

    # ---- 内核控制接口反代：面板走 /clash/…（也认根路径，方便面板里少打几个字）
    MIHOMO_API = ("/version", "/configs", "/proxies", "/rules", "/providers", "/connections",
                  "/logs", "/traffic", "/memory", "/group", "/delay", "/dns")

    def _serve_sub(self, p):
        """公开的订阅输出（不需要登录）：/sub/<token>?target=clash|v2ray"""
        tok = p[len("/sub/"):].strip("/")
        if "/" in tok:
            tok = tok.split("/")[0]
        args = self._args()
        txt, err = sub_render(tok, args.get("target", "clash"))
        if txt is None:
            log("订阅拉取失败：%s" % err)
            return self._json({"ok": False, "error": err}, 404)
        return self._send(txt.encode("utf-8"), "text/plain; charset=utf-8", 200,
                          {"Content-Disposition": 'attachment; filename="sub-%s.yaml"' % tok[:8],
                           "Profile-Update-Interval": "24",
                           "Subscription-Userinfo": "upload=0; download=0; total=0; expire=0"})

    def _is_kernel_path(self, p):
        for m in self.MIHOMO_API:
            if p == m or p.startswith(m + "/"):
                return True
        return False

    def _proxy_clash(self, prefix="/clash"):
        u = urllib.parse.urlsplit(ctrl_base(load_settings()))
        host, port = u.hostname or "127.0.0.1", (u.port or 9091)
        up_path = self.path if prefix == "/" else (self.path[len(prefix):] or "/")
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._proxy_ws(host, port, up_path)
        body = self._read_body()
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "connection", "content-length", "accept-encoding")}
        hdrs["Host"] = "%s:%d" % (host, port)
        if _auth_blank(hdrs.get("Authorization")) and self._authed():
            # 同一浏览器登录过管理台 → 由管理台替面板带上密钥，面板就不用手填密码了
            # （面板零配置模式下 secret 传空串，会发一个没有 token 的 Authorization，这里也认）
            hdrs["Authorization"] = "Bearer %s" % (load_settings().get("secret") or "")
        try:
            conn = http.client.HTTPConnection(host, port, timeout=30)
            conn.request(self.command, up_path, body=body or None, headers=hdrs)
            r = conn.getresponse()
            data, code = r.read(), r.status
            out = {k: v for k, v in r.getheaders()
                   if k.lower() not in ("transfer-encoding", "connection", "content-length", "keep-alive")}
            ct = r.getheader("Content-Type") or "application/json; charset=utf-8"
            conn.close()
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": "内核接口不可达：%r" % e}, 502)
        return self._send(data, ct, code, out)

    def _proxy_ws(self, host, port, up_path):
        """WebSocket（面板的流量/日志实时通道）原样透传"""
        import select
        try:
            up = socket.create_connection((host, port), timeout=10)
        except Exception as e:  # noqa: BLE001
            return self._send("内核接口不可达：%r" % e, "text/plain; charset=utf-8", 502)
        lines = ["%s %s HTTP/1.1" % (self.command, up_path)]
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length"):
                continue
            lines.append("%s: %s" % (k, v))
        lines.append("Host: %s:%d" % (host, port))       # 上游必须要 Host
        if _auth_blank(self.headers.get("Authorization")) and self._authed():
            lines.append("Authorization: Bearer %s" % (load_settings().get("secret") or ""))
        try:
            up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                ch = up.recv(4096)
                if not ch:
                    break
                buf += ch
            head, _, rest = buf.partition(b"\r\n\r\n")
            self.connection.sendall(head + b"\r\n\r\n" + rest)
            if b" 101 " not in head:
                up.close()
                self.close_connection = True
                return
            while True:
                r, _, x = select.select([self.connection, up], [], [self.connection, up], 180)
                if not r and not x:
                    break
                for sk in r:
                    data = sk.recv(65536)
                    if not data:
                        raise OSError("peer closed")
                    (up if sk is self.connection else self.connection).sendall(data)
        except Exception:
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass
            self.close_connection = True

    def do_PUT(self):
        p = self._path()
        if p.startswith("/clash") or self._is_kernel_path(p):
            return self._proxy_clash("/clash" if p.startswith("/clash") else "/")
        return self._json({"ok": False, "error": "未知路径"}, 404)

    def do_PATCH(self):
        return self.do_PUT()

    def do_DELETE(self):
        return self.do_PUT()

    def do_OPTIONS(self):
        return self._send("", "text/plain; charset=utf-8", 204,
                          {"Allow": "GET, POST, PUT, PATCH, DELETE, OPTIONS"})

    def _path(self):
        return urllib.parse.urlsplit(self.path).path

    # ---- 鉴权
    def _token(self):
        m = re.search(r"mh_sid=([0-9a-f]+)", self.headers.get("Cookie") or "")
        return m.group(1) if m else ""

    def _authed(self):
        tok = self._token()
        if not tok:
            return False
        with _SESSION_LOCK:
            if not _SESSIONS:
                _SESSIONS.update(_sess_load())
            exp = _SESSIONS.get(tok)
            if not exp or exp < time.time():
                _SESSIONS.pop(tok, None)
                return False
        return True

    def _need_auth(self):
        self._json({"ok": False, "error": "未登录或会话已过期，请重新登录"}, 401)
        return None

    # ---- GET
    def do_GET(self):
        p, args = self._path(), self._args()
        if p in ("/", "/index.html"):
            return self._html(PAGE if self._authed() else LOGIN_PAGE)
        if p == "/favicon.ico":
            return self._send(b"", "image/x-icon", 204)
        if p == "/logout":
            tok = self._token()
            with _SESSION_LOCK:
                _SESSIONS.pop(tok, None)
            _sess_save()
            return self._send("", "text/html; charset=utf-8", 302,
                              {"Set-Cookie": "mh_sid=; Path=/; Max-Age=0",
                               "Location": "/"})
        if p.startswith("/sub/"):
            return self._serve_sub(p)
        if p == "/ui" or p.startswith("/ui/"):
            return self._serve_ui(p)
        if p == "/clash" or p.startswith("/clash/"):
            return self._proxy_clash()
        if self._is_kernel_path(p):
            return self._proxy_clash("/")        # 面板地址填 http://群晖IP:9090 也能用
        if not p.startswith("/api/"):
            return self._json({"ok": False, "error": "未知路径"}, 404)
        if not self._authed():
            return self._need_auth()
        try:
            if p == "/api/health":
                return self._json(health())
            if p == "/api/status":
                return self._json(api_status())
            if p == "/api/settings":
                return self._json({"ok": True, "settings": load_settings()})
            if p == "/api/logs":
                return self._json({"ok": True, "log": tail_log(args.get("n", "120"))})
            if p == "/api/version":
                code, body = ctrl_get(load_settings(), "/version")
                return self._json({"ok": code == 200, "data": jloads(body, {})})
            if p == "/api/proxies":
                s = load_settings()
                code, body = ctrl_get(s, "/proxies")
                return self._json({"ok": code == 200, "data": jloads(body, {})})
            if p == "/api/providers":
                s = load_settings()
                code, body = ctrl_get(s, "/providers/proxies")
                return self._json({"ok": code == 200, "data": jloads(body, {})})
            if p == "/api/backups":
                return self._json({"ok": True, "items": list_backups()})
            if p == "/api/ip":
                return self._json({"ok": True, "ip": detect_ip()})
            if p == "/api/sublinks":
                return self._json({"ok": True, "items": sublinks_list()})
            if p == "/api/nodes":
                s = load_settings()
                return self._json({"ok": True, "count": len(manual_nodes(s)),
                                   "lines": len(s.get("nodes") or [])})
            return self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as e:  # noqa: BLE001
            log("GET %s 异常：%r" % (p, e))
            return self._json({"ok": False, "error": "%r" % (e,)}, 500)

    # ---- POST
    def do_POST(self):
        p = self._path()
        if p == "/clash" or p.startswith("/clash/"):
            return self._proxy_clash()
        if self._is_kernel_path(p):
            return self._proxy_clash("/")
        d = self._payload()
        try:
            if p == "/login":
                fd = d if isinstance(d, dict) else {}
                is_form = "x-www-form-urlencoded" in (self.headers.get("Content-Type") or "").lower()
                if _eq(str(fd.get("pass", "")), current_pass()):
                    tok = sess_new()
                    days = 30 if str(fd.get("remember") or "") not in ("", "0", "false", "False") else 7
                    with _SESSION_LOCK:
                        _SESSIONS[tok] = time.time() + days * 86400
                    _sess_save()
                    log("登录成功（记住 %d 天）" % days)
                    ck = "mh_sid=%s; Path=/; Max-Age=%d" % (tok, days * 86400)
                    if is_form:
                        return self._send("", "text/html; charset=utf-8", 302,
                                          {"Set-Cookie": ck, "Location": "/"})
                    return self._json({"ok": True, "msg": "登录成功"}, headers={"Set-Cookie": ck})
                log("登录失败（密码错误）")
                if is_form:
                    return self._html(LOGIN_PAGE.replace(
                        '<div class=err id=e></div>', '<div class=err id=e>密码错误，请重试</div>'))
                return self._json({"ok": False, "error": "密码错误"}, 401)
            if not self._authed():
                return self._need_auth()
            s = load_settings()
            if p == "/api/gen":
                r = update_config(s)
                return self._json(r)
            if p == "/api/kernel-update":
                return self._json(update_kernel())
            if p == "/api/kernel-check":
                kernel_latest(force=True)
                time.sleep(4)
                return self._json({"ok": True, "msg": "已重新检查内核版本",
                                   "latest": _KLV.get("latest") or "",
                                   "error": _KLV.get("err") or ""})
            if p == "/api/panel-ui":
                return self._json(install_panel())
            if p == "/api/sync":
                return self._json(sync_scripts())
            if p == "/api/self-update":
                _fd = d if isinstance(d, dict) else {}
                return self._json(self_update(apply=bool(_fd.get("apply"))))
            if p == "/api/auto-update":
                _fd = d if isinstance(d, dict) else {}
                _s = load_settings()
                if "enabled" in _fd:
                    _s["auto_update_panel"] = bool(_fd.get("enabled"))
                    save_settings(_s)
                    log("自动更新管理台程序：%s" % ("开启" if _s["auto_update_panel"] else "关闭"))
                return self._json({"ok": True, "enabled": bool(_s.get("auto_update_panel", True)),
                                   "last": str(_s.get("auto_update_last") or "")})
            if p == "/api/update-check":
                return self._json(update_check_once())
            if p == "/api/backup":
                b = backup_config()
                return self._json({"ok": bool(b), "file": os.path.basename(b) if b else "",
                                   "msg": "已备份" if b else "备份失败"})
            if p == "/api/restore":
                return self._json(restore_backup(d.get("name", "")))
            if p == "/api/kernel-restart":
                ok, out = restart_kernel()
                return self._json({"ok": ok, "msg": out})
            if p == "/api/airports":
                return self._json(save_airports(s, d))
            if p == "/api/sublinks":
                return self._json(save_sublinks(s, d))
            if p == "/api/nodes":
                return self._json(save_nodes(s, d))
            if p == "/api/groups":
                return self._json(save_groups(s, d))
            if p == "/api/settings":
                return self._json(save_tools(s, d))
            if p == "/api/apply":
                r = apply_ports(s)
                return self._json(r)
            return self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as e:  # noqa: BLE001
            log("POST %s 异常：%r" % (p, e))
            return self._json({"ok": False, "error": "%r" % (e,)}, 500)


# ---------------------------------------------------------------- 设置写入
# ---------------------------------------------------------------- 订阅生成（sublink）
def sublinks_load():
    """订阅表：{token: {name, urls, regions, enable, created}}"""
    try:
        with io.open(SUBLINKS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def sublinks_save(d):
    tmp = SUBLINKS_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=2))
    os.replace(tmp, SUBLINKS_PATH)
    return d


def sublinks_list():
    d = sublinks_load()
    out = []
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        out.append({"token": k, "name": v.get("name") or k,
                    "urls": v.get("urls") or [], "regions": v.get("regions") or [],
                    "enable": bool(v.get("enable", True)), "created": v.get("created") or "",
                    "kind": v.get("kind") or "clash"})
    out.sort(key=lambda x: x.get("created") or "")
    return out


def sub_token():
    """16 位小写 token（base32）"""
    return base64.b32encode(os.urandom(10)).decode().rstrip("=").lower()


def _sub_share_lines(data):
    """把一个订阅的内容变成分享链接列表（支持 base64 / 明文；Clash YAML 留到阶段2）"""
    try:
        txt = data.decode("utf-8", "replace")
    except Exception:
        txt = ""
    cand = []
    # base64 订阅
    try:
        pad = txt.strip().replace("\n", "").replace("\r", "")
        pad += "=" * (-len(pad) % 4)
        dec = base64.b64decode(pad).decode("utf-8", "replace")
        if "://" in dec:
            cand.append(dec)
    except Exception:
        pass
    cand.append(txt)
    for c in cand:
        lines = []
        for ln in c.splitlines():
            ln = ln.strip()
            if "://" in ln and not ln.startswith("#"):
                lines.append(ln)
        if lines:
            return lines
    return []


def sub_render(token, target="clash"):
    """返回 (内容, 错误)。token 无效或停用时返回 (None, 原因)"""
    item = sublinks_load().get(str(token or ""))
    if not isinstance(item, dict):
        return None, "订阅不存在（或已被删除）"
    if not item.get("enable", True):
        return None, "这条订阅已被停用"
    urls = [str(u).strip() for u in (item.get("urls") or []) if str(u).strip().startswith("http")]
    if not urls:
        return None, "这条订阅还没有填来源链接"
    tgt = (target or "clash").strip().lower()
    if tgt in ("clash", "mihomo", "clashmeta", "clash-verge"):
        import copy as _copy
        s2 = _copy.deepcopy(load_settings())
        # 来源当成 proxy-providers（客户端自己拉，节点不进面板）
        s2["airports"] = [{"name": "sub%d" % (i + 1), "url": u, "interval": 86400}
                          for i, u in enumerate(urls)]
        s2["nodes"] = []
        s2["tun_enable"] = False
        if item.get("regions"):
            s2["regions"] = [r for r in item["regions"] if r in REGION_MAP]
        s2["custom_groups"] = item.get("custom_groups") or []
        txt = gen_config(s2)
        # 订阅内容里不该带内核控制密钥
        txt = re.sub(r"(?m)^secret:.*$", 'secret: ""', txt)
        return txt, ""
    if tgt in ("v2ray", "v2rayn", "v2rayng", "base64", "v2", "shadowrocket", "mihomo-base64"):
        out = []
        for u in urls:
            data, err = http_fetch(u, timeout=40)
            if not data:
                continue
            out.extend(_sub_share_lines(data))
        if not out:
            return None, "来源里没解析到分享链接（如果这是 Clash 订阅，请用 target=clash）"
        return base64.b64encode("\n".join(out).encode("utf-8")).decode("ascii"), ""
    return None, "暂不支持的目标 %s（sing-box / Surge 在阶段 2）" % target


def save_sublinks(s, d):
    act = str(d.get("action") or "save")
    tbl = sublinks_load()
    tok = str(d.get("token") or "").strip()
    if act == "del":
        tbl.pop(tok, None)
        sublinks_save(tbl)
        log("订阅：已删除 %s" % tok[:8])
        return {"ok": True, "msg": "已删除这条订阅（链接立刻失效）"}
    if act == "toggle":
        if tok not in tbl:
            return {"ok": False, "error": "订阅不存在"}
        tbl[tok]["enable"] = bool(d.get("enable"))
        sublinks_save(tbl)
        return {"ok": True, "msg": "已%s" % ("启用" if tbl[tok]["enable"] else "停用")}
    urls = [str(x).strip() for x in (d.get("urls") or []) if str(x).strip()]
    urls = [u for u in urls if u.startswith("http")]
    if not urls:
        return {"ok": False, "error": "至少要填一个 http 开头的订阅链接"}
    name = str(d.get("name") or "").strip() or "我的订阅"
    regions = [str(r) for r in (d.get("regions") or []) if str(r) in REGION_MAP]
    if not tok:
        tok = sub_token()
        while tok in tbl:
            tok = sub_token()
        tbl[tok] = {"created": now()}
    tbl[tok].update({"name": name, "urls": urls, "regions": regions,
                     "enable": bool(d.get("enable", True))})
    sublinks_save(tbl)
    log("订阅：已保存「%s」(%d 个来源)" % (name, len(urls)))
    return {"ok": True, "msg": "已保存订阅「%s」" % name, "token": tok}


def save_airports(s, d):
    act = d.get("action") or "save"
    name = str(d.get("name") or "").strip()
    url = str(d.get("url") or "").strip()
    old = str(d.get("old") or "").strip()
    aps = s.get("airports") or []
    if act == "del":
        s["airports"] = [a for a in aps if str(a.get("name")) != (old or name)]
    else:
        if not url.startswith("http"):
            return {"ok": False, "error": "订阅链接必须以 http 开头"}
        if not name:
            name = "airport" if not aps else "airport%d" % (len(aps) + 1)
        cur = None
        for a in aps:
            if str(a.get("name")) == (old or name):
                cur = a
                break
        if cur is None:
            cur = {"name": name}
            aps.append(cur)
        cur["name"] = name
        cur["url"] = url
        try:
            cur["interval"] = int(d.get("interval") or 86400)
        except Exception:
            cur["interval"] = 86400
        s["airports"] = aps
    save_settings(s)
    try:
        with open(SUBURL_PATH, "w", encoding="utf-8") as f:
            f.write((s.get("airports") or [{}])[0].get("url", "") if s.get("airports") else "")
    except Exception:
        pass
    r = update_config(s)
    r["airports"] = s.get("airports") or []
    return r


def save_nodes(s, d):
    act = d.get("action") or "add"
    if act == "add":
        text = str(d.get("text") or "")
        if sub_convert is None:
            return {"ok": False, "error": "缺少 sub_convert.py，无法解析分享链接"}
        got = sub_convert.to_mihomo(text)
        if not got:
            return {"ok": False, "error": "没有解析到可用节点，请检查分享链接格式"}
        lines = [ln.strip() for ln in text.splitlines() if "://" in ln or ln.strip().startswith("{")]
        s["nodes"] = (s.get("nodes") or []) + lines
    elif act == "del":
        remove_node(s, d.get("name", ""))
    elif act == "clear":
        s["nodes"] = []
    save_settings(s)
    r = update_config(s)
    r["count"] = len(manual_nodes(s))
    return r


def save_groups(s, d):
    if isinstance(d.get("regions"), list):
        s["regions"] = [str(x) for x in d["regions"] if str(x) in REGION_MAP]
    if isinstance(d.get("groups"), dict):
        g = s.get("groups") or {}
        g.update({str(k): str(v) for k, v in d["groups"].items()})
        s["groups"] = g
    if isinstance(d.get("custom_groups"), list):
        out = []
        for cg in d["custom_groups"]:
            if not isinstance(cg, dict):
                continue
            nm = str(cg.get("name") or "").strip()[:24]
            if not nm or nm in ("直连", "DIRECT", "dns"):
                continue
            typ = str(cg.get("type") or "select")
            if typ not in ("select", "url-test", "fallback", "load-balance"):
                typ = "select"
            mem = [str(x).strip()[:80] for x in (cg.get("members") or []) if str(x).strip()]
            mem = [x for x in mem if x != nm]
            try:
                itv = int(cg.get("interval") or 300)
            except Exception:
                itv = 300
            out.append({"name": nm, "type": typ, "members": mem[:60],
                        "filter": str(cg.get("filter") or "").strip()[:200],
                        "icon": str(cg.get("icon") or "select")[:32], "interval": itv})
        s["custom_groups"] = out
    if isinstance(d.get("rule_targets"), dict):
        s["rule_targets"] = {str(k): str(v) for k, v in d["rule_targets"].items()}
    if "ruleset_enabled" in d:
        s["ruleset_enabled"] = bool(d["ruleset_enabled"])
    save_settings(s)
    return update_config(s)


def save_tools(s, d):
    changed_port = False
    for k in ("proxy_ip", "admin_bind", "proxy_port", "panel_port", "admin_port",
              "schedule_time", "secret"):
        if k in d:
            if k in ("proxy_port", "panel_port", "admin_port"):
                v = str(d[k]).strip()
                if not re.match(r"^[0-9]+$", v):
                    return {"ok": False, "error": "%s 必须是数字端口" % k}
                if s.get(k) != v:
                    changed_port = True
            s[k] = str(d[k]).strip()
    if "schedule_enabled" in d:
        s["schedule_enabled"] = bool(d["schedule_enabled"])
    save_settings(s)
    r = update_config(s)
    if changed_port:
        ap = apply_ports(s)
        r["apply"] = ap
        if not ap.get("ok"):
            r["ok"] = True
            r["msg"] = (r.get("msg") or "配置已更新") + "；端口改动需要重建容器"
            r["hint"] = ap.get("hint", "")
            r["warn"] = ap.get("error", "")
    return r


# ---------------------------------------------------------------- 备份 / 日志
def list_backups():
    out = []
    try:
        if os.path.isdir(BACKUP_DIR):
            for n in sorted(os.listdir(BACKUP_DIR), reverse=True):
                if n.startswith("config-") and n.endswith(".yaml"):
                    p = os.path.join(BACKUP_DIR, n)
                    out.append({"name": n, "size": os.path.getsize(p),
                                "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")})
    except Exception:
        pass
    return out[:20]


def restore_backup(name):
    name = os.path.basename(str(name or ""))
    if not name.startswith("config-") or not name.endswith(".yaml"):
        return {"ok": False, "error": "备份文件名不合法"}
    src = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(src):
        return {"ok": False, "error": "备份不存在：%s" % name}
    try:
        shutil.copy2(src, CONFIG)
        s = load_settings()
        ok, out = validate_config()
        if not ok:
            return {"ok": False, "error": "回滚后的配置校验未通过：%s" % out[-300:]}
        rok, rout = reload_config(s)
        if rok:
            return {"ok": True, "msg": "已回滚到 %s 并热重载" % name}
        r2ok, r2out = restart_kernel()
        return {"ok": bool(r2ok), "msg": "已回滚到 %s" % name,
                "error": "" if r2ok else r2out[:200]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%r" % (e,)}


def tail_log(n=120):
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max(1, min(2000, int(n))):])
    except Exception as e:  # noqa: BLE001
        return "(读取日志失败：%r)" % (e,)


# ---------------------------------------------------------------- 页面
LOGIN_PAGE = """<!doctype html>
<html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>mihomo 管理台 · 登录</title>
<style>
*{box-sizing:border-box}body{margin:0;font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:linear-gradient(135deg,#101828,#1d2939);color:#e4e7ec;display:flex;align-items:center;
justify-content:center;min-height:100vh}
.card{width:360px;max-width:92vw;background:#1d2939;border:1px solid #344054;border-radius:14px;padding:28px}
h1{margin:0 0 4px;font-size:19px}p.sub{margin:0 0 20px;color:#98a2b3;font-size:13px}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid #475467;background:#101828;color:#e4e7ec;font-size:15px}
button{width:100%;margin-top:14px;padding:11px;border:0;border-radius:9px;background:#3b82f6;color:#fff;font-size:15px;cursor:pointer}
button:hover{background:#2563eb}.err{color:#f97066;margin-top:12px;min-height:20px;font-size:13px}
</style></head><body>
<form class=card method=post action="/login" autocomplete="on">
<h1>🛡️ mihomo 网页管理台</h1><p class=sub>群晖 NAS · 请输入管理台密码</p>
<input type=text name=username value="mihomo-admin" autocomplete="username" style="display:none">
<input id=p name=pass type=password placeholder=管理台密码 autocomplete="current-password" autofocus>
<label style="display:flex;align-items:center;gap:8px;margin:12px 0 0;color:#98a2b3;font-size:13px">
<input type=checkbox name=remember value=1 checked style="width:auto;margin:0"> 记住我（30 天免登录）</label>
<button type=submit>登 录</button><div class=err id=e></div>
<p class=sub style="margin:14px 0 0">首次登录后浏览器会提示「保存密码」✓，之后自动填入 ✓</p></form></body></html>
"""

PAGE = """<!doctype html>
<html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>mihomo 网页管理台</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1d2939}
header{background:#101828;color:#fff;padding:12px 18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:600}
header .sp{flex:1}
nav{display:flex;gap:6px;background:#1d2939;padding:0 12px;flex-wrap:wrap}
nav a{color:#cbd5e1;text-decoration:none;padding:11px 14px;font-size:14px;border-bottom:2px solid transparent;cursor:pointer}
nav a.on{color:#fff;border-bottom-color:#3b82f6;font-weight:600}
main{max-width:1080px;margin:16px auto;padding:0 14px 60px}
.card{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:16px 18px;margin-bottom:16px}
.card h2{margin:0 0 12px;font-size:15px;display:flex;align-items:center;gap:8px}
label{display:block;font-size:13px;color:#475467;margin:8px 0 4px}
input,select,textarea{width:100%;padding:8px 10px;border:1px solid #d0d5dd;border-radius:8px;font-size:14px;font-family:inherit;background:#fff;color:#1d2939}
textarea{min-height:96px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1;min-width:150px}
button.btn{padding:8px 14px;border:0;border-radius:8px;background:#3b82f6;color:#fff;font-size:13.5px;cursor:pointer}
button.btn:hover{background:#2563eb}button.gray{background:#667085}button.gray:hover{background:#475467}
button.red{background:#d92d20}button.red:hover{background:#b42318}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #eaecf0;vertical-align:top}
th{color:#475467;font-weight:600;font-size:12.5px}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:#ecfdf3;color:#027a48}
.pill.bad{background:#fef3f2;color:#b42318}.pill.warn{background:#fffaeb;color:#b54708}
.kv{display:flex;gap:8px;flex-wrap:wrap}.kv>div{background:#f9fafb;border:1px solid #eaecf0;border-radius:9px;padding:8px 12px;min-width:150px}
.kv b{display:block;font-size:16px}.kv span{font-size:12px;color:#667085}
.hint{font-size:12.5px;color:#667085;margin-top:6px}
.warnbox{background:#fffaeb;border:1px solid #fedf89;color:#b54708;border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:13.5px}
.chk{display:inline-flex;align-items:center;gap:6px;margin:4px 12px 4px 0;font-size:13.5px}
.chk input{width:auto}
#logs{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12px;max-height:340px;overflow:auto;background:#101828;color:#d0d5dd;padding:12px;border-radius:9px}
#upbox2{display:none;position:fixed;inset:0;background:rgba(16,24,40,.55);z-index:99;align-items:center;justify-content:center;padding:16px}
#upbox2 .box{background:#fff;border-radius:14px;padding:22px;max-width:520px;width:100%;font-size:14px}
#upbox2 .box .err{color:#b42318}#upbox2 .box .warn{color:#b54708}#upbox2 .box .tip{color:#667085;font-size:12.5px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
</style></head><body>
<header>
 <h1>🛡️ mihomo 网页管理台</h1>
 <span class="pill" id=ver style="background:#3b82f6">v1.9</span>
 <span class="pill" id=hd>群晖 NAS</span>
 <span class=sp></span>
 <a class=btn style="background:#475467;text-decoration:none" href="/logout">退出登录</a>
</header>
<nav>
 <a data-t=status class=on>状态</a><a data-t=airports>机场</a><a data-t=nodes>节点</a>
 <a data-t=groups>策略</a><a data-t=sublinks>订阅</a><a data-t=tools>工具</a>
</nav>
<main>
 <div id=wp class=warnbox style="display:none">⚠️ 你还在用默认密码，请立刻到「工具」页改掉，否则同网段任何人都能打开管理台并看到你的节点。</div>
 <section id=tab-status>
  <div class=card><h2>📊 运行状态 <span class=sp></span>
    <button class="btn" onclick="gen()">立即更新配置</button></h2>
   <div class=kv id=kv></div>
   <div id=kupd></div></div>
  <div class=card><h2>🩺 健康自检（7 项）</h2><table><tbody id=hc></tbody></table>
   <div class=hint>健康自检会真实尝试：容器状态、配置校验、经代理出网、面板接口、磁盘、程序文件、定时任务。</div></div>
  <div class=card><h2>📦 代理提供者（机场）</h2><table>
   <thead><tr><th>提供者</th><th>节点数</th><th>更新时间</th></tr></thead><tbody id=pv></tbody></table></div>
 </section>

 <section id=tab-airports hidden>
  <div class=card><h2>✈️ 添加 / 修改机场订阅</h2>
   <div class=row><div><label>名称（英文/数字，作为提供者名）</label><input id=ap_n placeholder=airport></div>
   <div style="flex:2"><label>订阅链接（Clash / mihomo 格式）</label><input id=ap_u placeholder=https://...></div>
   <div><label>更新间隔（秒）</label><input id=ap_i value=86400></div></div>
   <div style="margin-top:12px"><button class=btn onclick="apSave()">保存并生成配置</button></div>
   <div class=hint>机场会生成独立的 <b>proxy-providers</b>，策略组用 <code>use</code> 引用；换机场只改这里。</div></div>
  <div class=card><h2>当前机场</h2><table>
   <thead><tr><th>名称</th><th>订阅链接</th><th>间隔</th><th>操作</th></tr></thead><tbody id=apt></tbody></table></div>
 </section>

 <section id=tab-nodes hidden>
  <div class=card><h2>➕ 添加手动节点</h2>
   <label>批量粘贴分享链接，一行一个（支持 vmess / vless(含 reality) / ss / trojan / hysteria2 / hysteria / tuic / anytls / http / socks5，也可贴 Clash 风格单行）</label>
   <textarea id=nd_t placeholder="vless://...&#10;vmess://..."></textarea>
   <div style="margin-top:12px"><button class=btn onclick="ndAdd()">解析并添加</button>
     <button class="btn red" onclick="ndClear()">清空全部手动节点</button></div>
   <div class=hint id=nd_h>当前手动节点：0 个</div></div>
  <div class=card><h2>手动节点列表</h2><table>
   <thead><tr><th>名称</th><th>类型</th><th>服务器</th><th>端口</th><th>操作</th></tr></thead><tbody id=ndt></tbody></table></div>
 </section>

 <section id=tab-groups hidden>
  <div class=card><h2>🌍 地区组（三件套）</h2><div id=rg></div>
   <div class=hint>勾选的地区会生成 手动(select) / 故转(fallback) / 自动(url-test) 三个策略组。</div></div>
  <div class=card><h2>🎯 业务组默认线路</h2><div class=grid2 id=bg></div>
   <div class=hint>TikTok 与 AI 默认走「狮城自动」（香港上不了 TikTok）；其余业务组默认走各自优先地区的「故转」，可自行改动。</div></div>
  <div class=card><h2>🧩 自定义策略组（自由增减）</h2>
   <div id=cg></div>
   <div style="margin-top:10px"><button class=btn onclick="cgAdd()">➕ 添加策略组</button>
     <span class=hint style="margin-left:8px">成员填名字、逗号分隔（如 <code>香港自动,日本故转</code>，也可直接写节点名）；成员留空则按「过滤规则」自动收录节点，过滤规则也留空 = 收录全部节点。</span></div>
   <div class=hint>支持类型：select（手动选）/ url-test（自动测速）/ fallback（故障转移）/ load-balance（负载均衡）。改完记得点下面「保存策略并生成配置」。</div>
  </div>
  <div class=card><h2>🧭 规则集分流</h2>
   <label class=chk><input type=checkbox id=rs_en onchange=rsToggle()> 启用规则集分流（七尺宇 qichiyuhub/rule + MetaCubeX meta-rules-dat）</label>
   <div class=hint>关闭后只保留基础规则（国内直连、其余走漏网之鱼）。</div></div>
  <div style="margin-top:8px"><button class=btn onclick="grSave()">保存策略并生成配置</button></div>
 </section>

 <section id=tab-tools hidden>
  <div class=card><h2>🔌 端口与地址</h2>
   <div class=row>
    <div><label>绑定 IP（本机地址）</label><input id=t_ip readonly style="background:#f2f4f7;color:#98a2b3;cursor:not-allowed"></div>
    <div><label>管理台绑定</label><input id=t_ab readonly style="background:#f2f4f7;color:#98a2b3;cursor:not-allowed"></div>
    <div><label>代理端口</label><input id=t_pp></div>
    <div><label>内核接口端口（仅本机）</label><input id=t_pa></div>
    <div><label>管理台端口（对外唯一）</label><input id=t_ad></div>
   </div>
   <div class=row><div><label>管理台密码（= 面板密钥）</label><input id=t_sc></div></div>
   <div style="margin-top:12px"><button class=btn onclick="tlSave()">保存</button>
    <button class="btn gray" onclick="applyAll()">应用并重建容器</button>
    <span class=hint style="margin-left:8px">保存密码</span></div>
   <div class=hint><b>单端口设计</b>：节点面板（<code>/ui/</code>）和管理台「共用一个端口」= 管理台端口，对外只需要放通这一个。
    <b>内核接口端口</b>（默认 9091）只绑 <code>127.0.0.1</code>，是管理台和内核之间的内部通道，不对外、也不用在路由器上放通。</div>
   <div class=hint>改<b>密码</b>：点「保存」即可 ✓；改<b>端口</b>：先点「保存」✓，再点「应用并重建容器」才算生效 ✓</div></div>

  <div class=card><h2>⏰ 定时自动更新</h2>
   <label class=chk><input type=checkbox id=t_sch> 启用</label>
   <div class=row><div><label>每天更新时间（HH:MM）</label><input id=t_st placeholder=04:00></div></div>
   <label class=chk style="margin-top:10px"><input type=checkbox id=t_au onchange="auToggle()">
    自动检查更新</label>
   <div style="margin-top:12px"><button class=btn onclick="tlSave()">保存</button></div></div>

  <div class=card><h2>🧰 维护工具</h2>
   <div class=row>
    <div><button class=btn onclick="gen()">立即更新配置</button><div class=hint>重新生成 + 校验 + 热重载</div></div>
    <div><button class=btn onclick="chkUpd()">检查更新</button><div class=hint>看仓库有没有新版 <span id=upTip></span></div></div>
    <div><button class=btn onclick="selfUpd()">更新管理台程序</button><div class=hint>拉取最新 + 校验 + 自动重启</div></div>
    <div><button class=btn onclick="apiPost('/api/backup',{},1)">立即备份配置</button><div class=hint>保留最近 10 份</div></div>
    <div><button class="btn red" onclick="kdRestart()">重启内核容器</button><div class=hint>热重载不生效时用</div></div>
   </div></div>

  <div class=card><h2>💾 备份与回滚</h2><table>
   <thead><tr><th>备份文件</th><th>大小</th><th>时间</th><th>操作</th></tr></thead><tbody id=bk></tbody></table></div>

  <div class=card><h2>📜 运行日志</h2>
   <div style="margin-bottom:8px"><button class=btn onclick="loadLogs()">刷新日志</button></div>
   <div id=logs>点击「刷新日志」加载</div></div>
 </section>

 <section id=tab-sublinks hidden>
  <div class=card><h2>🔗 订阅生成 <span class=sp></span><span class=hint>把自己的订阅变成一个可分享的链接</span></h2>
   <div class=hint>填名称 + 来源链接（可多行，多个会<b>合并</b>）→ 保存后得到两条链接：<b>Clash</b>（含你面板这套策略组）和 <b>V2Ray</b>（base64）。数据只存在你家 NAS（<code>/data/sublinks.json</code>），不经过任何第三方。</div>
   <div class=row style="margin-top:10px">
    <div style="flex:1 1 260px"><label>名称</label><input id=sb_name placeholder="例如 给朋友的香港节点"></div>
    <div style="flex:0 0 150px"><label>&nbsp;</label><button class=btn onclick="sbSave()">保存 / 新增</button></div>
   </div>
   <div style="margin-top:10px"><label>订阅来源（每行一个 http 链接）</label>
    <textarea id=sb_urls rows=3 style="width:100%;padding:9px 10px;border:1px solid #d0d5dd;border-radius:8px;font-size:13px" placeholder="https://机场A/api/v1/client/subscribe?token=xxx&#10;https://机场B/api/v1/client/subscribe?token=yyy"></textarea></div>
   <div style="margin-top:10px"><label>只保留这些地区（不勾 = 全部；对 Clash 输出生效）</label>
    <div id=sb_rg style="display:flex;gap:14px;flex-wrap:wrap;margin-top:6px"></div></div>
  </div>
  <div class=card><h2>📋 已有订阅</h2><div id=sb_list></div></div>
 </section>
</main>
<div id=upbox2><div class=box id=upmsg></div></div>
<script>
function $(s){return document.querySelector(s)}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function apiPost(url,body,quiet){
  if(!quiet) upModal('⏳ 正在更新配置…');
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})
   .then(r=>{ if(r.status===401){location.href='/';return null;} return r.json(); })
   .then(d=>{ if(quiet){ showRes(d); return d; } showRes(d); return d; })
   .catch(e=>{ upFail('请求失败：'+e); return null; });
}
function upModal(t){$('#upmsg').innerHTML=t;$('#upbox2').style.display='flex'}
function upClose(){$('#upbox2').style.display='none'}
function upFail(t){upModal('<div class=err>❌ '+esc(t)+'</div><div style="margin-top:14px"><button class=btn onclick="upClose()">关闭</button></div>')}
function showRes(d){
  if(!d){return}
  if(d.ok){
    var h='✅ '+esc(d.msg||'更新完毕');
    if(d.hint){h+='<div class=tip>'+esc(d.hint)+'</div>'}
    if(d.warn){h+='<div class=warn>⚠ '+esc(d.warn)+'</div>'}
    if(d.files!==undefined){h+='<div class=tip>已写入 '+d.files+' 个文件</div>'}
    upModal(h+'<div style="margin-top:14px"><button class=btn onclick="upClose();reload()">知道了</button></div>');
  } else {
    var e='❌ '+esc(d.error||'操作失败');
    if(d.hint){e+='<div class=tip>'+esc(d.hint)+'</div>'}
    upModal('<div class=err>'+e+'</div><div style="margin-top:14px"><button class=btn onclick="upClose()">关闭</button></div>');
  }
}
function show(s){return String(s==null?'':s)}
var TAB='status';
function tab(t){
  TAB=t;
  ['status','airports','nodes','groups','sublinks','tools'].forEach(function(k){$('#tab-'+k).hidden=(k!==t)});
  document.querySelectorAll('nav a').forEach(function(a){a.className=(a.dataset.t===t?'on':'')});
  load();
}
document.querySelectorAll('nav a').forEach(function(a){a.onclick=function(){try{tab(a.dataset.t)}catch(e){};if(a.dataset.t==='sublinks'){sbLoad()}}});
function gen(){apiPost('/api/gen',{},0)}
function reload(){load()}

function load(){
  if(TAB==='status'){loadStatus()}
  else if(TAB==='airports'){loadAirports()}
  else if(TAB==='nodes'){loadNodes()}
  else if(TAB==='groups'){loadGroups()}
  else if(TAB==='tools'){loadTools()}
  else if(TAB==='sublinks'){sbLoad()}
}
function chkKernel(){
  upModal('⏳ 正在检查内核最新版本…');
  fetch('/api/kernel-check',{method:'POST'}).then(r=>r.json()).then(function(){setTimeout(function(){upClose();loadStatus()},800)})
   .catch(function(){upFail('检查失败（网络不通）')});
}
function updKernel(){
  if(!confirm('更新内核：会拉取最新镜像并重建内核容器，约 1~2 分钟，期间代理短暂中断；配置/订阅/线路都不会丢。继续？'))return;
  upModal('⏳ 正在更新内核（拉取镜像 + 重建容器，约 1~2 分钟）…');
  apiPost('/api/kernel-update',{},1).then(function(){setTimeout(loadStatus,1500)});
}
function loadStatus(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    $('#wp').style.display = d.weak_pass ? 'block' : 'none';
    $('#kv').innerHTML=
      '<div><b>'+esc(d.kernel.state)+'</b><span>内核容器 '+esc(d.kernel.name)+'</span></div>'+
      '<div><b>'+esc(d.kernel.version||'-')+((d.kernel_update&&d.kernel_update.has_update)?' <span class="pill bad">有新版 '+esc(d.kernel_update.latest)+'</span>':'')+'</b><span>mihomo 版本</span></div>'+
      '<div><b>'+esc(d.airports.length)+'</b><span>机场数量</span></div>'+
      '<div><b>'+esc(d.manual_count)+'</b><span>手动节点</span></div>'+
      '<div><b style="font-size:13px">'+esc(d.out_ip||'未测出')+'</b><span>代理出口 IP</span></div>'+
      '<div><b style="font-size:13px">'+esc(d.proxy_addr)+'</b><span>代理地址</span></div>'+
      '<div><b style="font-size:13px"><a href="'+esc(d.panel_url)+'" target="_blank" style="color:#58a6ff">打开节点面板 ↗</a></b><span>'+esc(d.panel_url_full||d.panel_url)+'（同一端口，内嵌）</span></div>';
    var h='';d.providers.forEach(function(p){
      h+='<tr><td>'+esc(p.name)+'</td><td>'+esc(p.nodes)+'</td><td>'+esc(p.updated)+'</td></tr>'});
    $('#pv').innerHTML=h||'<tr><td colspan=3>还没有机场，去「机场」页面添加</td></tr>';
    var ku=d.kernel_update||{};
    $('#kupd').innerHTML = ku.has_update
      ? '<div style="margin-top:10px"><button class=btn onclick="updKernel()">🔄 一键更新内核到 '+esc(ku.latest)+'</button></div>'+
        '<div class=hint>拉取最新镜像并重建内核容器，配置 / 订阅 / 线路 / 端口都不会丢，期间代理会短暂中断。</div>'
      : (ku.latest
          ? '<div class=hint style="margin-top:8px">内核已是最新（最新 '+esc(ku.latest)+'）　<button class=btn2 onclick="chkKernel()">重新检查更新</button></div>'
          : '<div class=hint style="margin-top:8px">正在检查内核最新版本…　<button class=btn2 onclick="chkKernel()">重新检查</button></div>');
  });
  fetch('/api/health').then(r=>r.json()).then(function(d){
    var h=(d.items||[]).map(function(i){
      return '<tr><td style="width:32px">'+(i.ok?'<span class=pill>通过</span>':'<span class="pill bad">异常</span>')+
             '</td><td style="width:150px">'+esc(i.name)+'</td><td>'+esc(i.detail)+'</td></tr>'}).join('');
    $('#hc').innerHTML=h+'<tr><td colspan=3><b>共 '+esc(d.passed)+'/'+esc(d.total)+' 项通过</b>（'+esc(d.time)+'）</td></tr>';
  });
}
function loadAirports(){
  fetch('/api/settings').then(r=>r.json()).then(function(d){
    var aps=(d.settings||{}).airports||[];var h='';
    aps.forEach(function(a){
      h+='<tr><td>'+esc(a.name)+'</td><td style="word-break:break-all">'+esc(a.url)+'</td><td>'+esc(a.interval||86400)+
         '</td><td><button class="btn red" onclick="apDel(`'+esc(a.name)+'`)">删除</button></td></tr>'});
    $('#apt').innerHTML=h||'<tr><td colspan=4>还没有机场</td></tr>';
  });
}
function apSave(){
  var n=$('#ap_n').value.trim(),u=$('#ap_u').value.trim(),i=$('#ap_i').value.trim();
  if(!u){upFail('请填订阅链接');return}
  apiPost('/api/airports',{action:'save',name:n,url:u,interval:i},0);
}
function apDel(n){ if(!confirm('删除机场“'+n+'”？')) return; apiPost('/api/airports',{action:'del',name:n},0); }
function loadNodes(){
  fetch('/api/status').then(r=>r.json()).then(function(d){ $('#nd_h').textContent='当前手动节点：'+d.manual_count+' 个'; });
  fetch('/api/proxies').then(r=>r.json()).then(function(d){
    var ps=(d.data&&d.data.proxies)||{};var h='';var n=0;
    Object.keys(ps).forEach(function(k){var v=ps[k];
      if(v.type==='Direct'||v.type==='Dns') return;
      if(v.provider!==undefined) return; // 机场节点不在这里列
      n++;h+='<tr><td>'+esc(k)+'</td><td>'+esc(v.type)+'</td><td>'+esc(v.server||'-')+'</td><td>'+esc(v.port||'-')+
        '</td><td><button class="btn red" onclick="ndDel(`'+esc(k)+'`)">删除</button></td></tr>'});
    $('#ndt').innerHTML=h||'<tr><td colspan=5>没有手动节点（机场节点请在「节点面板」里看）</td></tr>';
  });
}
function ndAdd(){var t=$('#nd_t').value;if(!t.trim()){upFail('请粘贴分享链接');return}apiPost('/api/nodes',{action:'add',text:t},0)}
function ndDel(n){ if(!confirm('删除节点“'+n+'”？')) return; apiPost('/api/nodes',{action:'del',name:n},0); }
function ndClear(){ if(!confirm('清空全部手动节点？')) return; apiPost('/api/nodes',{action:'clear'},0); }
function loadGroups(){
  fetch('/api/settings').then(r=>r.json()).then(function(d){
    var s=d.settings||{},regs=s.regions||[],g=s.groups||{};
    fetch('/api/status').then(r=>r.json()).then(function(st){
      var lab={'香港':'香港','日本':'日本','新加坡':'狮城','美国':'美国','台湾':'台湾','韩国':'韩国','英国':'英国','德国':'德国','法国':'法国','荷兰':'荷兰','意大利':'意大利','西班牙':'西班牙','欧洲':'欧洲'};
      var labels=regs.map(function(r){return lab[r]||r});
      var mem=[].concat(labels.map(function(l){return l+'故转'}),labels.map(function(l){return l+'手动'}),
        labels.map(function(l){return l+'自动'}),['全部自动','全部手动','直连']);
      var cgs=[];
      (s.custom_groups||[]).forEach(function(x){ if(x && x.name){ cgs.push(x.name); } });
      if(cgs.length){
        var cgp=mem.indexOf('全部自动'); if(cgp<0){ cgp=mem.length; }
        mem=mem.slice(0,cgp).concat(cgs, mem.slice(cgp));
      }
      // 地区勾选
      var all=['香港','日本','新加坡','美国','台湾','韩国','英国','德国','法国','荷兰','意大利','西班牙','欧洲'];var h='';
      all.forEach(function(r){h+='<label class=chk><input type=checkbox class=rgc value="'+esc(r)+'"'+
        (regs.indexOf(r)>=0?' checked':'')+'> '+esc(lab[r]||r)+'</label>'});
      $('#rg').innerHTML=h;
      // 业务组
      var biz=['AI','Apple','GitHub','Google','Microsoft','NETFLIX','OneDrive','Steam','Telegram','TikTok','Wallet','YouTube'];
      var b='';
      biz.forEach(function(n){
        var cur=g[n]||mem[0];
        var o=mem.map(function(x){return '<option'+(x===cur?' selected':'')+'>'+esc(x)+'</option>'}).join('');
        b+='<div><label>'+esc(n)+'</label><select class=bgs data-n="'+esc(n)+'">'+o+'</select></div>';
      });
      $('#bg').innerHTML=b;
      $('#rs_en').checked=(s.ruleset_enabled!==false);
      loadCustom();
    });
  });
}
/* ===== 自定义策略组（自由增减）由 DSH 加入 ===== */
var CG=[];
function loadCustom(){
  fetch('/api/settings').then(function(r){ return r.json(); }).then(function(d){
    var s=(d&&d.settings)||{};
    CG=(s.custom_groups||[]).slice();
    cgRender();
  }).then(null,function(){
    var box=document.getElementById('cg');
    if(box){ box.innerHTML='<div class=hint>读取自定义策略组失败，请刷新页面重试（如果你刚改过密码，需要重建容器才生效）。</div>'; }
  });
}
function cgRender(){
  var box=document.getElementById('cg');
  if(!box){ return; }
  if(!CG.length){
    box.innerHTML='<div class=hint>还没有自定义策略组 —— 点下面的「➕ 添加策略组」就能加，想加几个都行。</div>';
    return;
  }
  var types=['select','url-test','fallback','load-balance'];
  var h='', i, t;
  for(i=0;i<CG.length;i++){
    var g=CG[i]||{};
    var opts='';
    for(t=0;t<types.length;t++){ opts+='<option'+(g.type===types[t]?' selected':'')+'>'+types[t]+'</option>'; }
    h+='<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;padding:10px 0;border-bottom:1px solid #e6ecf5">'
      +'<div style="flex:0 0 150px"><label>组名</label><input class=cg_name value="'+esc(g.name||'')+'" placeholder="例如 流媒体"></div>'
      +'<div style="flex:0 0 140px"><label>类型</label><select class=cg_type>'+opts+'</select></div>'
      +'<div style="flex:1 1 240px"><label>成员（逗号分隔，可留空）</label><input class=cg_mem value="'+esc((g.members||[]).join(','))+'" placeholder="香港自动,日本故转,全部自动"></div>'
      +'<div style="flex:1 1 240px"><label>过滤规则（可留空）</label><input class=cg_flt value="'+esc(g.filter||'')+'" placeholder="留空=全部节点；也可写 香港 或 ^(HK|US)"></div>'
      +'<div><button class="btn red" onclick="cgDel('+i+')">删除</button></div>'
      +'</div>';
  }
  h='<div class=hint style="margin-bottom:6px">当前已保存 <b>'+CG.length+'</b> 个自定义策略组 —— 下面每一行都能直接改（改完点「保存策略并生成配置」生效）：</div>'+h;
  box.innerHTML=h;
}
function cgAdd(){
  CG.push({name:'',type:'select',members:[],filter:'',icon:'select',interval:300});
  cgRender();
}
function cgDel(i){
  if(!confirm('删除这个策略组？')){ return; }
  CG.splice(i,1);
  cgRender();
}
function cgCollect(){
  var ns=document.querySelectorAll('.cg_name'), ts=document.querySelectorAll('.cg_type'),
      ms=document.querySelectorAll('.cg_mem'), fs=document.querySelectorAll('.cg_flt');
  var out=[], i, j;
  for(i=0;i<ns.length;i++){
    var parts=ms[i].value.split(','), mem=[];
    for(j=0;j<parts.length;j++){ var v=parts[j].trim(); if(v){ mem.push(v); } }
    out.push({name:ns[i].value.trim(), type:ts[i].value, members:mem,
              filter:fs[i].value.trim(), icon:'select', interval:300});
  }
  return out;
}
if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded', function(){ loadCustom(); });
} else {
  loadCustom();
}
function rsToggle(){}
function grSave(){
  var cg=cgCollect(), cgi;
  for(cgi=0;cgi<cg.length;cgi++){
    if(!cg[cgi].name){ alert('第 '+(cgi+1)+' 个自定义策略组的「组名」还没填 —— 填上再保存就会生成 ✓'); return; }
    if(cg[cgi].members.indexOf(cg[cgi].name)>=0){ alert('第 '+(cgi+1)+' 个自定义策略组的「成员」里不能填组名自己（会死循环）。要自动收录节点，请把「过滤规则」填成 优选 之类 ✓'); return; }
  }
  var regs=[];document.querySelectorAll('.rgc').forEach(function(c){if(c.checked)regs.push(c.value)});
  var g={};document.querySelectorAll('.bgs').forEach(function(sel){g[sel.dataset.n]=sel.value});
  apiPost('/api/groups',{regions:regs,groups:g,ruleset_enabled:$('#rs_en').checked,custom_groups:cg},0);
}
function loadTools(){
  fetch('/api/settings').then(r=>r.json()).then(function(d){
    var s=d.settings||{};
    $('#t_ip').value=s.proxy_ip||'';$('#t_ab').value=s.admin_bind||'';
    $('#t_pp').value=s.proxy_port||'7890';$('#t_pa').value=s.panel_port||'9091';
    $('#t_ad').value=s.admin_port||'9090';$('#t_sc').value=s.secret||'';
    $('#t_sch').checked=(s.schedule_enabled!==false);$('#t_st').value=s.schedule_time||'04:00';
  });
  loadBackups();
}
function tlSave(){
  apiPost('/api/settings',{proxy_ip:$('#t_ip').value.trim(),admin_bind:$('#t_ab').value.trim(),
    proxy_port:$('#t_pp').value.trim(),panel_port:$('#t_pa').value.trim(),admin_port:$('#t_ad').value.trim(),
    secret:$('#t_sc').value, schedule_enabled:$('#t_sch').checked, schedule_time:$('#t_st').value.trim()},0);
}
function ipDetect(){ fetch('/api/ip').then(r=>r.json()).then(function(d){ if(d.ip){$('#t_ip').value=d.ip;$('#t_ab').value=d.ip;} }); }
function applyAll(){ apiPost('/api/apply',{},0); }
function kdRestart(){ if(!confirm('重启内核容器 mihomo？')) return; apiPost('/api/kernel-restart',{},0); }
function loadBackups(){
  fetch('/api/backups').then(r=>r.json()).then(function(d){
    var h=(d.items||[]).map(function(b){
      return '<tr><td>'+esc(b.name)+'</td><td>'+esc(b.size)+'</td><td>'+esc(b.mtime)+
        '</td><td><button class=btn onclick="rsGo(`'+esc(b.name)+'`)">回滚</button></td></tr>'}).join('');
    $('#bk').innerHTML=h||'<tr><td colspan=4>暂无备份</td></tr>';
  });
}
function rsGo(n){ if(!confirm('回滚到 '+n+' ？')) return; apiPost('/api/restore',{name:n},0); }
function selfUpd(){ doUpdate(); }
function auToggle(){
  apiPost('/api/auto-update',{enabled:$('#t_au').checked},0);
}
fetch('/api/settings').then(r=>r.json()).then(function(d){
  var s=d.settings||{}, c=document.getElementById('t_au'), tip=document.getElementById('upTip');
  var vv=document.getElementById('ver');
  if(vv && s.auto_update_last){ vv.title='上次同步的提交：'+String(s.auto_update_last).slice(0,8); }
  if(c){ c.checked=(s.auto_update_panel!==false); }
  if(tip && s.update_available){
    tip.innerHTML='<b style="color:#d92d20">🔔 发现新版（'+String(s.update_available).slice(0,8)+'）—— 点左边按钮更新</b>';
  }
});
function chkUpd(){
  var tip=document.getElementById('upTip');
  if(tip){ tip.innerHTML='<span style="color:#98a2b3">正在检查…</span>'; }
  fetch('/api/update-check',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
   .then(function(r){ return r.json(); })
   .then(function(d){
     if(d && d.available){
       var sha=String(d.sha||'').slice(0,8);
       if(tip){ tip.innerHTML='<b style="color:#d92d20">🔔 发现新版（'+sha+'）</b>'; }
       upModal('<div style="font-size:15px">🔔 发现新版：<b>'+esc(sha)+'</b></div>'
         +'<div class=hint style="margin-top:8px">更新前会自动备份当前版本；更新期间面板会重启（约 3-8 秒）</div>'
         +'<div style="margin-top:18px;display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap">'
         +'<button class=btn onclick="doUpdate()">立即更新</button>'
         +'<button class=btn2 onclick="upClose()">以后再说</button></div>');
     }else if(d && d.ok){
       if(tip){ tip.innerHTML='<b style="color:#12b76a">✓ '+esc(d.msg||'已是最新')+'</b>'; }
       upModal('<div style="font-size:15px">✓ '+esc(d.msg||'已是最新')+'</div>'
         +'<div style="margin-top:18px;text-align:right"><button class=btn onclick="upClose()">知道了</button></div>');
     }else{
       if(tip){ tip.innerHTML='<b style="color:#d92d20">检查失败：'+esc((d&&d.error)||'')+'</b>'; }
       upFail('检查失败：'+esc((d&&d.error)||''));
     }
   })
   .catch(function(e){ if(tip){ tip.innerHTML='<b style="color:#d92d20">检查失败：'+esc(e)+'</b>'; } upFail('检查失败：'+esc(e)); });
}
function doUpdate(){
  upModal('⏳ 正在更新管理台程序…<div class=hint style="margin-top:8px">下载 → 语法校验 → 备份 → 替换 → 重启面板</div>');
  apiPost('/api/self-update',{apply:true},1).then(function(d){
    if(d && d.ok && !/已经是最新/.test(d.msg||'')){ setTimeout(function(){location.reload()},9000); }
  });
}
function loadLogs(){ fetch('/api/logs?n=200').then(r=>r.json()).then(function(d){$('#logs').textContent=d.log||''}); }
/* ===== 订阅生成（由 DSH 加入）===== */
var SB=[];
function sbLink(t,target){ return location.origin+'/sub/'+t+'?target='+target; }
function sbLoad(){
  var rg=document.getElementById('sb_rg');
  if(rg && !rg.dataset.done){
    var all=['香港','日本','新加坡','美国','台湾','韩国','英国','德国','法国','荷兰','意大利','西班牙','欧洲'];
    var lab={'香港':'香港','日本':'日本','新加坡':'狮城','美国':'美国','台湾':'台湾','韩国':'韩国','英国':'英国','德国':'德国','法国':'法国','荷兰':'荷兰','意大利':'意大利','西班牙':'西班牙','欧洲':'欧洲'};
    var h='';
    for(var i=0;i<all.length;i++){ h+='<label class=chk><input type=checkbox class=sbrg value="'+esc(all[i])+'"> '+esc(lab[all[i]]||all[i])+'</label>'; }
    rg.innerHTML=h; rg.dataset.done='1';
  }
  fetch('/api/sublinks').then(function(r){return r.json()}).then(function(d){ SB=(d&&d.items)||[]; sbRender(); });
}
function sbRender(){
  var box=document.getElementById('sb_list');
  if(!box){ return; }
  if(!SB.length){ box.innerHTML='<div class=hint>还没有订阅 —— 上面填好名称和来源，点「保存 / 新增」。</div>'; return; }
  var h='', i, it, l1, l2;
  for(i=0;i<SB.length;i++){
    it=SB[i]; l1=sbLink(it.token,'clash'); l2=sbLink(it.token,'v2ray');
    h+='<div style="padding:12px 0;border-bottom:1px solid #e6ecf5">'
      +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
      +'<b>'+esc(it.name)+'</b>'
      +(it.enable?'<span class="pill" style="background:#12b76a">启用</span>':'<span class="pill" style="background:#98a2b3">已停用</span>')
      +'<span class=hint>'+esc((it.urls||[]).length)+' 个来源'+((it.regions&&it.regions.length)?' · 地区：'+esc(it.regions.join('/')):'')+'</span>'
      +'<span class=sp></span>'
      +'<button class=btn2 data-act=toggle data-tok="'+esc(it.token)+'" data-en="'+(it.enable?1:0)+'">'+(it.enable?'停用':'启用')+'</button>'
      +'<button class="btn2 red" data-act=del data-tok="'+esc(it.token)+'">删除</button>'
      +'</div>'
      +'<div class=hint style="margin-top:8px;word-break:break-all">Clash：<code>'+esc(l1)+'</code> <button class=btn2 data-act=copy data-txt="'+esc(l1)+'">复制</button></div>'
      +'<div class=hint style="word-break:break-all">V2Ray：<code>'+esc(l2)+'</code> <button class=btn2 data-act=copy data-txt="'+esc(l2)+'">复制</button></div>'
      +'</div>';
  }
  box.innerHTML=h;
  box.onclick=function(ev){
    var el=ev.target, b=null;
    while(el && el!==box){ if(el.getAttribute && el.getAttribute('data-act')){ b=el; break; } el=el.parentNode; }
    if(!b){ return; }
    var a=b.getAttribute('data-act')||'';
    if(a==='toggle'){ sbToggle(b.getAttribute('data-tok')||'', b.getAttribute('data-en')==='1'?0:1); }
    else if(a==='del'){ sbDel(b.getAttribute('data-tok')||''); }
    else if(a==='copy'){ sbCopy(b.getAttribute('data-txt')||''); }
  };
}
function sbCopy(txt){
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(function(){ upModal('✅ 已复制到剪贴板<div style="margin-top:12px"><button class=btn onclick="upClose()">知道了</button></div>'); },
      function(){ prompt('手动复制：',txt); });
  } else { prompt('手动复制：',txt); }
}
function sbSave(){
  var raw=((document.getElementById('sb_urls')||{}).value||'').split(String.fromCharCode(10)), urls=[], i, u;
  for(i=0;i<raw.length;i++){ u=raw[i].trim(); if(u){ urls.push(u); } }
  var regions=[];
  document.querySelectorAll('.sbrg').forEach(function(c){ if(c.checked){ regions.push(c.value); } });
  apiPost('/api/sublinks',{action:'save',name:((document.getElementById('sb_name')||{}).value||''),urls:urls,regions:regions},1)
   .then(function(d){
     if(d && d.ok){
       var ta=document.getElementById('sb_urls'); if(ta){ ta.value=''; }
       var nm=document.getElementById('sb_name'); if(nm){ nm.value=''; }
       sbLoad();
     }
   });
}
function sbDel(t){ if(!confirm('删除这条订阅？链接会立刻失效，朋友那边就用不了了。')){ return; } apiPost('/api/sublinks',{action:'del',token:t},1).then(function(){ sbLoad(); }); }
function sbToggle(t,e){ apiPost('/api/sublinks',{action:'toggle',token:t,enable:!!e},1).then(function(){ sbLoad(); }); }

loadStatus();
</script>
<button id=mhuiBtn onclick="(function(){var d=document.getElementById('mhui');var f=document.getElementById('mhuiF');if(d.style.display==='block'){d.style.display='none';}else{d.style.display='block';if(!f.getAttribute('src'))f.setAttribute('src','/ui/');}})()" style="position:fixed;right:18px;bottom:18px;z-index:9999;padding:10px 16px;border:0;border-radius:22px;background:#2f81f7;color:#fff;font-size:14px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.35)">节点面板（内嵌）</button>
<div id=mhui style="display:none;position:fixed;inset:0;z-index:9998;background:#0d1117">
 <div style="height:38px;line-height:38px;padding:0 12px;background:#161b22;color:#c9d1d9;font-size:13px">
   节点面板 metacubexd —— 地址已自动带出（<b>http://当前地址/clash</b>），密钥可留空（管理台登录过就自动带上）
   <button onclick="document.getElementById('mhui').style.display='none'" style="float:right;margin-top:8px;padding:2px 10px;border:1px solid #30363d;border-radius:6px;background:#21262d;color:#c9d1d9;cursor:pointer">关闭 ✕</button>
 </div>
 <iframe id=mhuiF style="width:100%;height:calc(100% - 38px);border:0"></iframe>
</div>
</body></html>
"""


def panel_bootstrap():
    """首次启动自动把节点面板装上（省得进管理台再点一次）。

    Container Manager 粘贴 compose 的装法没法跑脚本，只能靠这里；
    下载源在国内经常不稳，所以失败会重试几次，最后才放弃（放弃后仍可在管理台点按钮）。
    """
    if os.path.isfile(os.path.join(UI_DIR, "index.html")):
        return
    for attempt in range(1, 4):
        log("首次启动：自动安装节点面板（第 %d 次尝试）" % attempt)
        try:
            r = install_panel()
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": repr(e)}
        if r.get("ok") and os.path.isfile(os.path.join(UI_DIR, "index.html")):
            log("节点面板已自动安装完成 ✓")
            return
        log("面板自动安装失败：%s" % (r.get("error") or r.get("msg") or r))
        time.sleep(20)
    log("面板自动安装最终失败 —— 可在管理台点「一键安装面板界面」重试")


def main():
    s = bootstrap()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=panel_bootstrap, daemon=True).start()
    threading.Thread(target=auto_update_loop, daemon=True).start()
    log("=== 管理台启动 ===")
    log("BASE=%s  bind=%s:%s  pass_len=%d  docker=%s"
        % (BASE, ADMIN_BIND, ADMIN_PORT, len(ADMIN_PASS), DOCKER))
    if sub_convert is None:
        log("警告：未找到 sub_convert.py，手动节点解析不可用")
    log("host_base=%s  raw=%s" % (detect_host_base(), RAW_BASE))
    srv = ThreadingHTTPServer((ADMIN_BIND, ADMIN_PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
