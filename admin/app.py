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
# 统一本地时区：容器默认是 UTC，会让日志、自检时间比本地差 8 小时
try:
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
except Exception:  # noqa: BLE001
    pass

APP_VERSION = "2.9.7"

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


def ruleset_defaults():
    """默认规则集清单（key/默认地址/行为），界面用来显示备注与一键恢复"""
    out = []
    for k, fn in RULE_PROVIDERS:
        out.append({"key": k, "url": MRS % fn, "behavior": "domain"})
    out.append({"key": "proxylite", "url": PROXYLITE, "behavior": "classical"})
    return out


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
    for _d in [x.strip().lstrip("*.") for x in str(s.get("direct_domains") or "").split(",") if x.strip()]:
        a('    - "+.%s"' % _d)
    for _it in (s.get("custom_routes") or []):
        if isinstance(_it, dict) and str(_it.get("domain") or "").strip():
            a('    - "+.%s"' % str(_it.get("domain")).strip())
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
        _urls = s.get("ruleset_urls") or {}
        for k, fn in RULE_PROVIDERS:
            a('  %s: {<<: *domain, url: "%s"}' % (k, str(_urls.get(k) or (MRS % fn)).strip()))
        a('  proxylite: {<<: *class, url: "%s"}' % str(_urls.get("proxylite") or PROXYLITE).strip())
        for _c in (s.get("custom_rulesets") or []):
            if not isinstance(_c, dict):
                continue
            _ck, _cu = str(_c.get("key") or "").strip(), str(_c.get("url") or "").strip()
            if not _ck or not _cu:
                continue
            _cb = str(_c.get("behavior") or "domain").strip().lower()
            a('  %s: {<<: *%s, url: "%s"}' % (_ck, "class" if _cb.startswith("class") else "domain", _cu))
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
    # 注意：这些域名同时会加进 dns.fake-ip-filter，DNS 返回真实 IP，直连才真的通
    for _d in [x.strip().lstrip("*.") for x in str(s.get("direct_domains") or "").split(",") if x.strip()]:
        _r = "DOMAIN-SUFFIX,%s,DIRECT" % _d
        if _r not in rules:
            rules.append(_r)
    # 自定义分流：按网站指定 直连 / 走代理 / 指定策略组（排在规则集前面，优先级最高）
    for _it in (s.get("custom_routes") or []):
        if not isinstance(_it, dict):
            continue
        _dm = str(_it.get("domain") or "").strip()
        _tg = re.sub(r"[^\w\u4e00-\u9fa5 .&-]", "", str(_it.get("target") or "").strip())[:40] or "漏网之鱼"
        if _tg.upper() == "DIRECT":
            _tg = "直连"
        if not _dm or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,251}$", _dm):
            continue
        _r2 = "DOMAIN-SUFFIX,%s,%s" % (_dm, _tg)
        if _r2 not in rules:
            rules.append(_r2)
    if s.get("ruleset_enabled", True):
        rt = s.get("rule_targets") or {}

        def _tgt(v, d):
            return v if v in valid_targets else d

        rules.append("RULE-SET,proxylite,%s" % _tgt(rt.get("proxylite"), DEFAULT_PROXY))
        for key, tgt in BIZ_RULES:
            rules.append("RULE-SET,%s,%s" % (key, _tgt(rt.get(key), tgt)))
        for _c in (s.get("custom_rulesets") or []):
            if not isinstance(_c, dict):
                continue
            _ck, _cu = str(_c.get("key") or "").strip(), str(_c.get("url") or "").strip()
            if _ck and _cu:
                rules.append("RULE-SET,%s,%s" % (_ck, _tgt(_c.get("target"), "漏网之鱼")))
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


# ---------------------------------------------------------------- 当前网络信息 / 分流测试 / IP 详情
NET_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

IPV4_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")

# 延迟测试站点：(名称, 分组, 探测地址) —— 名称与前端图标表一一对应
LAT_SITES = [
    ('字节抖音', '国内', 'https://lf3-zlink-tos.ugurl.cn/obj/zebra-public/resource_lmmizj_1632398893.png'),
    ('Bilibili', '国内', 'https://i0.hdslb.com/bfs/face/member/noface.jpg@24w_24h_1c'),
    ('腾讯微信', '国内', 'https://res.wx.qq.com/a/wx_fed/assets/res/NTI4MWU5.ico'),
    ('阿里淘宝', '国内', 'https://img.alicdn.com/imgextra/i2/O1CN01qnQCrN1VkzAWiU4Hs_!!6000000002692-2-tps-33-33.png'),
    ('GitHub', '国际', 'https://github.github.io/janky/images/bg_hr.png'),
    ('Telegram.DC5', '国际', 'https://flora.web.telegram.org/'),
    ('X.com', '国际', 'https://abs.twimg.com/favicons/twitter.3.ico'),
    ('YouTube', '国际', 'https://www.youtube.com/favicon.ico'),
]

# 分流测试目标：(key, 名称, 分组, 探针, 地址)
#   trace  = 访问 https://<地址>/cdn-cgi/trace，正文 ip= 就是该站点看到的客户端 IP
#   header = 请求地址，从响应头 X-Request-Ip / cdn-user-ip 取
#   json   = 请求地址，正文里取第一个 IPv4
# 分流测试目标：与「策略」页的业务组一一对应 (业务组名, 代表站点)
#   做法：请求该站点，同时读 mihomo 的 /connections，
#   拿到「命中的规则」和「实际策略链」（链尾就是最终业务组），据此判定走向是否正确。
SPLIT_TARGETS = [
    ("AI", "chatgpt.com"),
    ("GitHub", "api.github.com"),
    ("YouTube", "www.youtube.com"),
    ("Google", "www.google.com"),
    ("NETFLIX", "www.netflix.com"),
    ("Steam", "store.steampowered.com"),
    ("OneDrive", "onedrive.live.com"),
    ("Microsoft", "www.microsoft.com"),
    ("Apple", "www.apple.com"),
    ("TikTok", "www.tiktok.com"),
    ("Telegram", "t.me"),
    ("Wallet", "www.paypal.com"),
    ("漏网之鱼", "www.cloudflare.com"),
    ("直连", "www.taobao.com"),
]

CN_SRC = [("太平洋", "https://whois.pconline.com.cn/ipJson.jsp?json=true"),
          ("腾讯", "https://r.inews.qq.com/api/ip2city?otype=json"),
          ("淘宝", "https://www.taobao.com/help/getip.php")]
OS_SRC = [("ipapi.is", "https://api.ipapi.is/"), ("ip.sb", "https://api.ip.sb/geoip")]
WALL_SRC = [("Google", "https://www.google.com/generate_204"),
            ("推特", "https://x.com/"),
            ("YouTube", "https://www.youtube.com/")]

_NI_CACHE = {"t": 0.0, "d": None}
_LAT_CACHE = {"t": 0.0, "d": None}
_SPLIT_CACHE = {"t": 0.0, "d": None}
_GEO_CACHE = {}
_NI_LOCK = threading.Lock()


def _http_probe(url, timeout=7.0, maxbody=32768):
    """GET 一个 URL，返回 (毫秒, 状态码, 正文, 响应头小写字典)。失败返回 (0, 0, '', {})"""
    t0 = time.time()
    try:
        rq = urllib.request.Request(url, headers={"User-Agent": NET_UA,
                                                  "Accept": "*/*", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            body = r.read(maxbody)
            code = getattr(r, "status", 200) or 200
            hs = {str(k).lower(): v for k, v in r.headers.items()}
        return int((time.time() - t0) * 1000), code, _dec(body), hs
    except Exception:
        return 0, 0, "", {}


def _dec(raw):
    """探测回来的正文解码：先 UTF-8，失败再按 GB18030（太平洋等国内站返回 GBK）"""
    if not raw:
        return ""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _lat_round(url, timeout=6.0, samples=2):
    """一轮暖连接测延迟：同一条连接上预热 1 次，再连测 samples 次取最小。
    用 HEAD 只等响应头、不下载正文，所以才是真正的「延迟」而不是「延迟+下载」。
    服务器不支持 HEAD（4xx）就退回 GET，并且只读到响应头就断开。
    """
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        port = u.port or (443 if u.scheme == "https" else 80)
    except Exception:
        return 0
    if not host:
        return 0
    best = 0
    conn = None
    hdr = {"User-Agent": NET_UA, "Accept": "*/*", "Connection": "keep-alive"}
    try:
        conn = (http.client.HTTPSConnection(host, port, timeout=timeout)
                if u.scheme == "https" else http.client.HTTPConnection(host, port, timeout=timeout))
        method = "HEAD"
        for i in range(samples + 1):
            t0 = time.time()
            conn.request(method, path, headers=hdr)
            r = conn.getresponse()
            ms = int((time.time() - t0) * 1000)
            if method == "HEAD" and int(getattr(r, "status", 200) or 200) >= 400:
                method = "GET"         # 不支持 HEAD，改 GET（只读响应头就够）
                try:
                    conn.close()
                except Exception:
                    pass
                conn = (http.client.HTTPSConnection(host, port, timeout=timeout)
                        if u.scheme == "https" else http.client.HTTPConnection(host, port, timeout=timeout))
                continue
            try:
                r.read(1024 if method == "GET" else 0)
            except Exception:
                pass
            if i >= 1 and ms > 0:
                best = ms if best == 0 else min(best, ms)
    except Exception:
        pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    return best


def _lat_one(url, timeout=6.0, samples=2):
    """测延迟：失败/超时会换新连接再试一轮，两轮取到值就返回。"""
    ms = _lat_round(url, timeout, samples)
    if ms:
        return ms
    return _lat_round(url, timeout, samples)


def _ipv4(text):
    for m in IPV4_RE.finditer(text or ""):
        ip = m.group(1)
        try:
            if all(0 <= int(x) <= 255 for x in ip.split(".")) and not ip.startswith(("0.", "127.", "255.")):
                return ip
        except Exception:
            continue
    return ""


def _parallel(jobs, timeout=20.0):
    """jobs=[(key, fn)]，并发执行，返回 {key: 结果 or None}"""
    out = {}
    lk = threading.Lock()

    def run(k, fn):
        try:
            v = fn()
        except Exception:
            v = None
        with lk:
            out[k] = v

    ths = [threading.Thread(target=run, args=(k, f), daemon=True) for k, f in jobs]
    for th in ths:
        th.start()
    for th in ths:
        th.join(timeout)
    return out


def _first_of(jobs, timeout=20.0):
    """按顺序取第一个非空结果（保证可复现，不是谁先返回用谁）"""
    res = _parallel(jobs, timeout)
    for k, _ in jobs:
        if res.get(k):
            return res[k]
    return None


def _cn_one(name, url):
    ms, code, body, hs = _http_probe(url, 7.0)
    ip = _ipv4(body)
    if not ip:
        return None
    loc = ""
    try:
        d = json.loads(body)
        loc = " ".join(str(x) for x in (d.get("pro"), d.get("city"), d.get("addr")) if x)
    except Exception:
        k = body.find("来自于")
        if k >= 0:
            loc = body[k + 3:].split("：")[-1].strip()
    return {"ip": ip, "loc": loc.strip()[:60], "src": name, "ms": ms}


def _os_one(name, url):
    ms, code, body, hs = _http_probe(url, 8.0)
    try:
        d = json.loads(body)
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("ip"):
        return None
    bits = [d.get("country") or d.get("country_code") or "", d.get("city") or "",
            d.get("isp") or d.get("company") or ""]
    return {"ip": str(d.get("ip")),
            "loc": (" ".join(str(x).strip() for x in bits if str(x).strip()))[:80],
            "src": name, "ms": ms,
            "asn": str(d.get("asn") or "")[:40],
            "org": str(d.get("company") or d.get("isp") or "")[:60]}


def net_info(force=False):
    """三框：国内（直连出口）/ 国外（代理出口）/ 墙外（Google·推特·YouTube 可达性）"""
    now = time.time()
    with _NI_LOCK:
        if not force and _NI_CACHE["d"] and now - _NI_CACHE["t"] < 90:
            return _NI_CACHE["d"]
    cn = _first_of([(n, (lambda n=n, u=u: _cn_one(n, u))) for n, u in CN_SRC], 10) or \
        {"ip": "", "loc": "", "src": "", "ms": 0}
    os_ = _first_of([(n, (lambda n=n, u=u: _os_one(n, u))) for n, u in OS_SRC], 11) or \
        {"ip": "", "loc": "", "src": "", "ms": 0, "asn": "", "org": ""}
    raw = _parallel([(n, (lambda u=u: _http_probe(u, 7.0)[0])) for n, u in WALL_SRC], 10)
    wall = {n: int(raw.get(n) or 0) for n, _ in WALL_SRC}
    d = {"cn": cn, "os": os_, "wall": wall, "ts": int(now)}
    with _NI_LOCK:
        _NI_CACHE["d"] = d
        _NI_CACHE["t"] = now
    return d


def latency_all(force=False):
    """8 个站点延迟（国内 4 走直连、国际 4 走代理），服务端实测"""
    now = time.time()
    with _NI_LOCK:
        if not force and _LAT_CACHE["d"] and now - _LAT_CACHE["t"] < 60:
            return _LAT_CACHE["d"]
    res = _parallel([(i, (lambda u=u: _lat_one(u, 6.0, 3))) for i, (n, r, u) in enumerate(LAT_SITES)], 26)
    items = []
    for i, (n, r, u) in enumerate(LAT_SITES):
        items.append({"name": n, "region": r, "ms": int(res.get(i) or 0)})
    with _NI_LOCK:
        _LAT_CACHE["d"] = items
        _LAT_CACHE["t"] = now
    return items


def _geo_one(ip):
    """单个 IP 的归属地（带缓存）"""
    if not ip:
        return {}
    if ip in _GEO_CACHE:
        return _GEO_CACHE[ip]
    ms, code, body, hs = _http_probe("https://api.ipapi.is/?q=" + urllib.parse.quote(ip), 8.0)
    try:
        d = json.loads(body)
    except Exception:
        d = None
    out = {}
    if isinstance(d, dict) and d:
        out = {"country": str(d.get("country") or d.get("country_code") or ""),
               "city": str(d.get("city") or ""),
               "region": str(d.get("region") or ""),
               "asn": str(d.get("asn") or "")[:40],
               "org": str(d.get("company") or "")[:60],
               "lat": d.get("lat"), "lon": d.get("lon"),
               "tz": str(d.get("timezone") or "")}
        _GEO_CACHE[ip] = out
    return out


def _conn_host(c):
    """从 /connections 的一条记录里取主机名"""
    m = (c or {}).get("metadata") or {}
    return str(m.get("host") or m.get("sniffHost") or "").strip().lower()


def _conn_match(want, host):
    """主机名是否匹配（含子域双向包含）"""
    if not want or not host:
        return False
    return host == want or host.endswith("." + want) or want.endswith("." + host)


SPLIT_LAT_PATHS = ("/favicon.ico", "/robots.txt", "/")


_SPLIT_LAT = {"running": False}


def _split_lat(rows):
    """给 rows 补延迟：三条路径全部并发，**谁先测完就先写进对应行**（逐格出现，不等全部）。"""
    if _SPLIT_LAT.get("running"):
        return
    _SPLIT_LAT["running"] = True
    try:
        lock = threading.Lock()
        bygrp = {r["group"]: r for r in rows}

        def one(group, host, path):
            ms = _lat_round("https://%s%s" % (host, path), 2.5, 1)
            if not ms:
                return
            with lock:
                r = bygrp.get(group)
                if r is not None:
                    cur = r.get("ms") or 0
                    if cur == 0 or ms < cur:
                        r["ms"] = int(ms)

        ths = []
        for r in rows:
            for p in SPLIT_LAT_PATHS:
                th = threading.Thread(target=one, args=(r["group"], r["host"], p), daemon=True)
                th.start()
                ths.append(th)
        for th in ths:
            th.join(9)
    finally:
        _SPLIT_LAT["running"] = False


def _wait_lat(rows, secs=5.0):
    """等后台延迟任务补完（有限等待，等不到就先把已有值返回）"""
    t0 = time.time()
    while time.time() - t0 < secs:
        if not any(not r.get("ms") for r in rows):
            return
        if not _SPLIT_LAT.get("running"):
            return
        time.sleep(0.15)


def split_test(force=False, with_lat=False):
    """分流测试：每个业务组取一个代表站点，请求它的同时读 /connections，
    拿到「命中的规则 + 实际策略链」，再和该组应有的走向比对。
    with_lat=False 只抓连接（快，2~4 秒出表）；True 再把延迟补上。"""
    now = time.time()
    with _NI_LOCK:
        cached = _SPLIT_CACHE["d"]
        age = now - _SPLIT_CACHE["t"]
    if cached and not force and age < 60:
        return cached
    s = load_settings()
    # 先把空表建好、写进缓存，并立刻在后台并行测延迟（不阻塞任何返回）
    rows = [{"group": g, "host": h, "rule": "", "payload": "", "chain": "",
             "top": "", "ok": -1, "ms": 0} for g, h in SPLIT_TARGETS]
    by = {r["group"]: r for r in rows}
    d = {"rows": rows, "ts": int(now)}
    with _NI_LOCK:
        _SPLIT_CACHE["d"] = d
        _SPLIT_CACHE["t"] = now
    threading.Thread(target=_split_lat, args=(rows,), daemon=True).start()

    def fire(host):
        """发一个请求并短暂保持连接 —— 只为让 /connections 抓得到，不关心耗时"""
        try:
            rq = urllib.request.Request("https://%s/" % host,
                                        headers={"User-Agent": NET_UA, "Accept": "*/*"})
            with urllib.request.urlopen(rq, timeout=6.0) as r:
                r.read(512)
                time.sleep(1.0)
        except Exception:
            pass

    for grp, host in SPLIT_TARGETS:
        threading.Thread(target=fire, args=(host,), daemon=True).start()

    found = {}
    t_end = time.time() + 6
    while time.time() < t_end and len(found) < len(SPLIT_TARGETS):
        try:
            code, body = ctrl_get(s, "/connections")
            if code == 200:
                payload = json.loads(body)      # 注意：不能叫 d，d 是我们的结果字典
                for c in (payload.get("connections") or []):
                    h = _conn_host(c)
                    if not h:
                        continue
                    for grp, host in SPLIT_TARGETS:
                        if grp in found:
                            continue
                        if _conn_match(host.lower(), h):
                            found[grp] = c
        except Exception:
            pass
        if len(found) < len(SPLIT_TARGETS):
            time.sleep(0.15)

    for grp, host in SPLIT_TARGETS:
        c = found.get(grp) or {}
        chains = [str(x) for x in (c.get("chains") or [])]
        top = chains[-1] if chains else ""
        r = by[grp]
        r["rule"] = str(c.get("rule") or "")
        r["payload"] = str(c.get("rulePayload") or "")
        r["chain"] = " > ".join(chains)
        r["top"] = top
        if not c:
            r["ok"] = -1
        elif grp == "直连":
            r["ok"] = 1 if (top == "直连" or "direct" in top.lower()) else 0
        else:
            r["ok"] = 1 if top == grp else 0
    return d


def ip_detail(ip):
    """IP 详情（服务端代查，避免跨域与混合内容）"""
    ip = re.sub(r"[^0-9A-Fa-f:.]", "", str(ip or ""))[:45]
    if not ip:
        return {"ip": "", "src": "", "raw": {}}
    for name, tmpl in (("ipapi.is", "https://api.ipapi.is/?q=%s"),
                       ("ip.sb", "https://api.ip.sb/geoip/%s")):
        ms, code, body, hs = _http_probe(tmpl % urllib.parse.quote(ip), 9.0)
        try:
            d = json.loads(body)
        except Exception:
            d = None
        if isinstance(d, dict) and d:
            return {"ip": ip, "src": name, "raw": d}
    return {"ip": ip, "src": "", "raw": {}}


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
        tok = p.split("/", 2)[2].strip("/")
        if "/" in tok:
            tok = tok.split("/")[0]
        args = self._args()
        tgt = (args.get("target") or "").strip().lower()
        if tgt in ("", "auto", "adaptive", "any"):
            tgt = guess_target(self.headers.get("User-Agent"))
            log("订阅自适应：UA=%s → %s" % ((self.headers.get("User-Agent") or "")[:70], tgt))
        txt, err = sub_render(tok, tgt)
        if txt is None:
            log("订阅拉取失败：%s" % err)
            return self._json({"ok": False, "error": err}, 404)
        return self._send(txt.encode("utf-8"), "text/plain; charset=utf-8", 200,
                          {"Content-Disposition": 'attachment; filename="sub-%s%s"'
                                                  % (tok[:8], ".json" if tgt == "singbox" else (".conf" if tgt == "surge" else ".yaml")),
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
        if p.startswith("/sub/") or p.startswith("/c/"):
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
                return self._json({"ok": True, "settings": load_settings(),
                                   "ruleset_defaults": ruleset_defaults()})
            if p == "/api/netinfo":
                return self._json({"ok": True, "data": net_info("force" in args)})
            if p == "/api/latency":
                return self._json({"ok": True, "items": latency_all("force" in args)})
            if p == "/api/splittest":
                return self._json({"ok": True, "data": split_test("force" in args, "lat" in args)})
            if p == "/api/ipinfo":
                return self._json({"ok": True, "data": ip_detail(args.get("ip"))})
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
                    "kind": v.get("kind") or "clash",
                    "n_nodes": len(v.get("nodes") or []), "n_urls": len(v.get("urls") or [])})
    out.sort(key=lambda x: x.get("created") or "")
    return out


def sub_token():
    """16 位小写 token（base32）"""
    return base64.b32encode(os.urandom(10)).decode().rstrip("=").lower()


def sub_code_ok(c):
    """自定义短链：字母/数字/下划线/短横线，1~40 位"""
    c = str(c or "").strip()
    if not re.match(r"^[A-Za-z0-9_-]{1,40}$", c):
        return None
    return c


def guess_target(ua):
    """自适应：按客户端 UA 猜要哪种格式"""
    u = str(ua or "").lower()
    if "surge" in u:
        return "surge"
    if any(k in u for k in ("sing-box", "singbox", "hiddify", "karing", "sfa", "sfi", "sfm")):
        return "singbox"
    if any(k in u for k in ("clash", "mihomo", "stash", "flclash", "clashx", "verge", "openclash", "meta")):
        return "clash"
    return "v2ray"


def _sb_tls(p):
    """sing-box 的 tls 段"""
    if not p.get("tls"):
        return None
    d = {"enabled": True}
    if p.get("servername"):
        d["server_name"] = p["servername"]
    if p.get("skip-cert-verify"):
        d["insecure"] = True
    if p.get("alpn"):
        d["alpn"] = p["alpn"]
    if p.get("client-fingerprint"):
        d["utls"] = {"enabled": True, "fingerprint": p["client-fingerprint"]}
    r = p.get("reality-opts") or {}
    if r:
        d["reality"] = {"enabled": True,
                        "public_key": r.get("public-key") or r.get("public_key") or "",
                        "short_id": r.get("short-id") or r.get("short_id") or ""}
    return d


def _sb_transport(p):
    net = str(p.get("network") or "").lower()
    if net == "ws":
        o = p.get("ws-opts") or {}
        tr = {"type": "ws", "path": o.get("path") or "/"}
        hd = o.get("headers") or {}
        if hd:
            tr["headers"] = hd
        return tr
    if net == "grpc":
        o = p.get("grpc-opts") or {}
        return {"type": "grpc", "service_name": o.get("grpc-service-name") or ""}
    if net in ("h2", "http"):
        return {"type": "http"}
    return None


def sub_as_singbox(proxies):
    """把节点做成一份 sing-box 配置（JSON）"""
    outs, tags = [], []
    for p in proxies:
        ty = str(p.get("type") or "").lower()
        o = {"tag": p.get("name") or "node", "server": p.get("server"),
             "server_port": int(p.get("port") or 0)}
        if ty == "ss":
            o.update({"type": "shadowsocks", "method": p.get("cipher"), "password": p.get("password")})
        elif ty == "vmess":
            o.update({"type": "vmess", "uuid": p.get("uuid"), "alter_id": int(p.get("alterId") or 0),
                      "security": p.get("cipher") or "auto"})
        elif ty == "vless":
            o.update({"type": "vless", "uuid": p.get("uuid")})
            if p.get("flow"):
                o["flow"] = p["flow"]
        elif ty == "trojan":
            o.update({"type": "trojan", "password": p.get("password")})
        elif ty in ("hysteria2", "hysteria"):
            o.update({"type": "hysteria2", "password": p.get("password")})
        elif ty == "tuic":
            o.update({"type": "tuic", "uuid": p.get("uuid"), "password": p.get("password")})
            if p.get("congestion-controller"):
                o["congestion_control"] = p["congestion-controller"]
        elif ty == "anytls":
            o.update({"type": "anytls", "password": p.get("password")})
        else:
            continue
        tls, tr = _sb_tls(p), _sb_transport(p)
        if tls:
            o["tls"] = tls
        if tr:
            o["transport"] = tr
        outs.append(o)
        tags.append(o["tag"])
    if not tags:
        return ""
    cfg = {
        "log": {"level": "info"},
        "inbounds": [
            {"type": "tun", "tag": "tun-in", "address": ["172.19.0.1/30"],
             "auto_route": True, "stack": "mixed", "sniff": True},
            {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 2080},
        ],
        "outbounds": [
            {"type": "selector", "tag": "选择节点", "outbounds": ["自动选择"] + tags, "default": "自动选择"},
            {"type": "urltest", "tag": "自动选择", "outbounds": tags,
             "url": "http://www.gstatic.com/generate_204", "interval": "3m", "tolerance": 50},
        ] + outs + [{"type": "direct", "tag": "direct"}],
        "route": {"rules": [{"action": "sniff"},
                            {"protocol": "dns", "action": "hijack-dns"},
                            {"ip_is_private": True, "outbound": "direct"}],
                  "final": "选择节点", "auto_detect_interface": True},
    }
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def sub_as_surge(proxies):
    """把节点做成 Surge 的 [Proxy] / [Proxy Group] / [Rule] 段"""
    lines, names = [], []
    for p in proxies:
        ty = str(p.get("type") or "").lower()
        nm = str(p.get("name") or "node").replace(",", " ")
        host = "%s, %s" % (p.get("server"), p.get("port"))
        extra = []
        if p.get("tls"):
            extra.append("tls=true")
            if p.get("servername"):
                extra.append("sni=%s" % p["servername"])
            if p.get("skip-cert-verify"):
                extra.append("skip-cert-verify=true")
        if str(p.get("network") or "").lower() == "ws":
            o = p.get("ws-opts") or {}
            hd = o.get("headers") or {}
            extra.append("ws=true")
            extra.append("ws-path=%s" % (o.get("path") or "/"))
            if hd:
                extra.append("ws-headers=%s" % ",".join("%s:%s" % (k, v) for k, v in hd.items()))
        if ty == "ss":
            ln = "%s = ss, %s, encrypt-method=%s, password=%s" % (nm, host, p.get("cipher"), p.get("password"))
        elif ty == "vmess":
            ln = "%s = vmess, %s, username=%s" % (nm, host, p.get("uuid"))
        elif ty == "vless":
            ln = "%s = vless, %s, username=%s" % (nm, host, p.get("uuid"))
        elif ty == "trojan":
            ln = "%s = trojan, %s, password=%s" % (nm, host, p.get("password"))
        elif ty in ("hysteria2", "hysteria"):
            ln = "%s = hysteria2, %s, password=%s" % (nm, host, p.get("password"))
        elif ty == "tuic":
            ln = "%s = tuic, %s, token=%s" % (nm, host, p.get("password"))
        else:
            continue            # Surge 不支持 anytls 之类，跳过
        if extra:
            ln += ", " + ", ".join(extra)
        lines.append(ln)
        names.append(nm)
    if not names:
        return ""
    out = "[Proxy]\n" + "\n".join(lines) + "\n\n[Proxy Group]\n"
    out += "选择节点 = select, 自动选择, " + ", ".join(names) + "\n"
    out += "自动选择 = url-test, " + ", ".join(names) + \
           ", url=http://www.gstatic.com/generate_204, interval=300, tolerance=50\n"
    out += "\n[Rule]\nGEOIP,CN,DIRECT\nFINAL,选择节点\n"
    return out



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
    nodes = [str(x).strip() for x in (item.get("nodes") or []) if str(x).strip()]
    if not urls and not nodes:
        return None, "这条订阅还没有填节点或来源链接"
    tgt = (target or "clash").strip().lower()
    if tgt in ("clash", "mihomo", "clashmeta", "clash-verge"):
        import copy as _copy
        s2 = _copy.deepcopy(load_settings())
        # 来源当成 proxy-providers（客户端自己拉，节点不进面板）
        s2["airports"] = [{"name": "sub%d" % (i + 1), "url": u, "interval": 86400}
                          for i, u in enumerate(urls)]
        s2["nodes"] = nodes            # ← 贴进来的节点直接内联成 proxies
        s2["tun_enable"] = False
        if item.get("regions"):
            s2["regions"] = [r for r in item["regions"] if r in REGION_MAP]
        s2["custom_groups"] = item.get("custom_groups") or []
        txt = gen_config(s2)
        # 订阅内容里不该带内核控制密钥
        txt = re.sub(r"(?m)^secret:.*$", 'secret: ""', txt)
        return txt, ""
    if tgt in ("v2ray", "v2rayn", "v2rayng", "base64", "v2", "shadowrocket", "mihomo-base64"):
        out = list(nodes)              # ← 直接贴的节点原样输出
        for u in urls:
            data, err = http_fetch(u, timeout=40)
            if not data:
                continue
            out.extend(_sub_share_lines(data))
        if not out:
            return None, "来源里没解析到分享链接（如果这是 Clash 订阅，请用 target=clash）"
        return base64.b64encode("\n".join(out).encode("utf-8")).decode("ascii"), ""
    if tgt in ("singbox", "sing-box", "sfa", "sfi", "sfm", "hiddify"):
        import copy as _copy
        s2 = _copy.deepcopy(load_settings())
        s2["airports"] = []
        s2["nodes"] = nodes            # 只支持贴进来的节点（订阅链接节点在云端，面板无法枚举）
        s2["tun_enable"] = False
        pr = manual_nodes(s2)
        if not pr:
            return None, "sing-box 输出需要「贴节点」的订阅（纯订阅链接请用 target=clash）"
        return sub_as_singbox(pr), ""
    if tgt in ("surge", "surge4", "surge5"):
        import copy as _copy
        s2 = _copy.deepcopy(load_settings())
        s2["airports"] = []
        s2["nodes"] = nodes
        s2["tun_enable"] = False
        pr = manual_nodes(s2)
        if not pr:
            return None, "Surge 输出需要「贴节点」的订阅（纯订阅链接请用 target=clash）"
        out = sub_as_surge(pr)
        if not out:
            return None, "这个订阅里的节点 Surge 都不支持"
        return out, ""
    return None, "不支持的格式 %s（可选 clash / v2ray / singbox / surge）" % target


def save_sublinks(s, d):
    act = str(d.get("action") or "save")
    tbl = sublinks_load()
    tok = str(d.get("token") or "").strip()
    if act == "rename":
        if tok not in tbl:
            return {"ok": False, "error": "订阅不存在"}
        code = sub_code_ok(d.get("code"))
        if not code:
            return {"ok": False, "error": "短链只能用 字母/数字/下划线/短横线，1~40 位"}
        if code == tok:
            return {"ok": True, "msg": "短链没变（还是 %s）" % code, "token": code}
        if code in tbl:
            return {"ok": False, "error": "短链「%s」已被占用，换一个" % code}
        tbl[code] = tbl.pop(tok)
        sublinks_save(tbl)
        log("订阅：短链 %s → %s" % (tok[:12], code))
        return {"ok": True, "msg": "短链已改为「%s」：新链接立刻生效，旧链接立刻失效" % code, "token": code}
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
    urls, nodes, bad = [], [], []
    for x in (d.get("urls") or []):
        x = str(x).strip()
        if not x:
            continue
        if x.startswith("http"):
            urls.append(x)
        elif "://" in x:
            nodes.append(x)            # 节点分享链接
        else:
            bad.append(x[:40])
    if bad:
        return {"ok": False, "error": "有 %d 行看不懂（既不是订阅链接也不是节点）：%s" % (len(bad), bad[0])}
    if not urls and not nodes:
        return {"ok": False, "error": "至少要填一个节点分享链接（vmess:// 等）或订阅链接（http 开头）"}
    name = str(d.get("name") or "").strip() or "我的订阅"
    regions = [str(r) for r in (d.get("regions") or []) if str(r) in REGION_MAP]
    if not tok:
        want = sub_code_ok(d.get("code"))
        if str(d.get("code") or "").strip() and not want:
            return {"ok": False, "error": "短链只能用 字母/数字/下划线/短横线，1~40 位"}
        if want and want in tbl:
            return {"ok": False, "error": "短链「%s」已被占用，换一个" % want}
        tok = want or sub_token()
        while tok in tbl:
            tok = sub_token()
        tbl[tok] = {"created": now()}
    tbl[tok].update({"name": name, "urls": urls, "nodes": nodes, "regions": regions,
                     "enable": bool(d.get("enable", True))})
    sublinks_save(tbl)
    log("订阅：已保存「%s」(%d 个节点 / %d 个来源)" % (name, len(nodes), len(urls)))
    return {"ok": True,
            "msg": "已保存订阅「%s」（%d 个节点 / %d 个来源），短链 /sub/%s" % (name, len(nodes), len(urls), tok),
            "token": tok}


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
    if isinstance(d.get("custom_routes"), list):
        out = []
        for it in d["custom_routes"]:
            if not isinstance(it, dict):
                continue
            dm = str(it.get("domain") or "").strip().lower()
            dm = dm.replace("http://", "").replace("https://", "").split("/")[0].strip()
            dm = dm.lstrip("*.").strip(".")
            if not dm or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,251}$", dm) or ".." in dm:
                continue
            tg = str(it.get("target") or "").strip() or "漏网之鱼"
            item = {"domain": dm, "target": tg[:40]}
            if item not in out:
                out.append(item)
            if len(out) >= 200:
                break
        s["custom_routes"] = out
    if isinstance(d.get("ruleset_urls"), dict):
        out = {}
        for k, v in d["ruleset_urls"].items():
            k, v = str(k or "").strip()[:40], str(v or "").strip()[:400]
            if k and v.startswith(("http://", "https://")):
                out[k] = v
        s["ruleset_urls"] = out
    if isinstance(d.get("custom_rulesets"), list):
        out = []
        for it in d["custom_rulesets"]:
            if not isinstance(it, dict):
                continue
            k = re.sub(r"[^A-Za-z0-9_.-]", "", str(it.get("key") or ""))[:40]
            u = str(it.get("url") or "").strip()[:400]
            if not k or not u.startswith(("http://", "https://")):
                continue
            bh = "classical" if str(it.get("behavior") or "").lower().startswith("class") else "domain"
            item = {"key": k, "url": u, "behavior": bh,
                    "target": str(it.get("target") or "漏网之鱼").strip()[:40] or "漏网之鱼"}
            if item not in out:
                out.append(item)
            if len(out) >= 30:
                break
        s["custom_rulesets"] = out
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
:root{--bd:rgba(120,140,180,.22);--t1:#eaf0fb;--t2:#9fb0cc;--t3:#7b8aa6;--pri:#4f8cff;--bad:#f97066}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;display:flex;align-items:center;justify-content:center;padding:24px;color:var(--t1);position:relative;
font:15px/1.65 system-ui,-apple-system,"Segoe UI","PingFang SC","HarmonyOS Sans SC","Microsoft YaHei",sans-serif;
background:
 radial-gradient(900px 500px at 12% 8%,rgba(79,140,255,.22),transparent 60%),
 radial-gradient(760px 460px at 92% 92%,rgba(139,92,246,.20),transparent 58%),
 linear-gradient(150deg,#0a1120,#141d33)}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
 background-image:linear-gradient(rgba(130,160,210,.07) 1px,transparent 1px),linear-gradient(90deg,rgba(130,160,210,.07) 1px,transparent 1px);
 background-size:46px 46px;
 -webkit-mask-image:radial-gradient(circle at 50% 38%,#000 5%,transparent 72%);
 mask-image:radial-gradient(circle at 50% 38%,#000 5%,transparent 72%)}
.blob{position:fixed;border-radius:50%;filter:blur(70px);pointer-events:none;z-index:0}
.b1{width:340px;height:340px;background:#2563eb;left:-80px;top:-90px;opacity:.5}
.b2{width:300px;height:300px;background:#7c3aed;right:-70px;bottom:-80px;opacity:.45}
.b3{width:220px;height:220px;background:#06b6d4;right:22%;top:6%;opacity:.3}
form.card{position:relative;z-index:1;width:396px;max-width:100%;padding:30px 28px 22px;border-radius:20px;
 background:rgba(22,31,52,.78);border:1px solid var(--bd);
 -webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);
 box-shadow:0 30px 70px -30px rgba(0,0,0,.8),0 2px 10px rgba(0,0,0,.3)}
form.card::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;border-radius:20px 20px 0 0;
 background:linear-gradient(90deg,#2563eb,#7c3aed,#06b6d4)}
.logo{width:56px;height:56px;border-radius:17px;display:flex;align-items:center;justify-content:center;margin-bottom:16px;
 background:linear-gradient(135deg,#2563eb,#7c3aed);box-shadow:0 14px 30px -14px rgba(79,140,255,.95)}
.logo svg{width:31px;height:31px}
h1{margin:0 0 6px;font-size:20px;font-weight:700;letter-spacing:.3px}
p.sub{margin:0 0 20px;color:var(--t2);font-size:13px}
.field{position:relative}
.field .fi{position:absolute;left:13px;top:50%;transform:translateY(-50%);font-size:14px;opacity:.65;pointer-events:none}
input{width:100%;padding:13px 14px 13px 40px;border-radius:12px;font:inherit;font-size:15px;
 border:1px solid var(--bd);background:rgba(9,15,28,.55);color:var(--t1);transition:border-color .16s,box-shadow .16s}
input:focus{outline:0;border-color:var(--pri);box-shadow:0 0 0 4px rgba(79,140,255,.18)}
input::placeholder{color:var(--t3)}
.rem{display:flex;align-items:center;gap:9px;margin:14px 0 0;color:var(--t2);font-size:13px;cursor:pointer}
.rem input{width:auto;padding:0;accent-color:var(--pri)}
button{width:100%;margin-top:18px;padding:13px;border:0;border-radius:12px;font:inherit;font-size:15px;font-weight:600;
 color:#fff;cursor:pointer;background:linear-gradient(135deg,#2563eb,#6d28d9);
 box-shadow:0 16px 30px -16px rgba(79,140,255,.95);transition:filter .16s,transform .16s}
button:hover{filter:brightness(1.08);transform:translateY(-1px)}
button:active{transform:none}
.err{color:var(--bad);margin-top:12px;min-height:20px;font-size:13px;font-weight:500}
p.tip{margin:16px 0 0;color:var(--t3);font-size:12.3px;text-align:center;line-height:1.6}
</style></head><body>
<span class="blob b1"></span><span class="blob b2"></span><span class="blob b3"></span>
<form class=card method=post action="/login" autocomplete="on">
<div class=logo><svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3.2v5.9c0 4.2-2.9 7.7-7 9-4.1-1.3-7-4.8-7-9V6.2z"/><path d="M8.8 12.2l2.1 2.1 4.3-4.6"/></svg></div>
<h1>mihomo 网页管理台</h1><p class=sub>群晖 NAS · 请输入管理台密码</p>
<input type=text name=username value="mihomo-admin" autocomplete="username" style="display:none">
<div class=field><span class=fi>🔒</span><input id=p name=pass type=password placeholder=管理台密码 autocomplete="current-password" autofocus></div>
<label class=rem><input type=checkbox name=remember value=1 checked> 记住我（30 天免登录）</label>
<button type=submit>登 录</button><div class=err id=e></div>
<p class=tip>登录后浏览器会提示保存密码，下次自动填入</p>
</form></body></html>"""

PAGE = """<!doctype html>
<html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>mihomo 网页管理台</title>
<script>try{var _th=localStorage.getItem("mh-theme");if(_th==="dark"||(!_th&&window.matchMedia&&window.matchMedia("(prefers-color-scheme:dark)").matches)){document.documentElement.className="dark";}}catch(e){}</script>
<style>
/* ===================== 设计变量 ===================== */
:root{
 --bg:#f2f5fb;--bg2:#e8eef8;
 --card:#ffffff;--card2:#f8fafd;--card3:#eff4fb;
 --bd:#e3e9f3;--bd2:#eef2f8;--bdh:#c9d4e6;--zeb:#fbfcfe;
 --t1:#0f172a;--t2:#4b5768;--t3:#8a95a8;
 --pri:#2563eb;--pri2:#7c3aed;
 --ring:rgba(37,99,235,.16);--hov:rgba(37,99,235,.07);--hov2:rgba(37,99,235,.16);
 --ok:#047857;--okbg:#ecfdf5;--okbd:#a7f3d0;
 --wn:#b45309;--wnbg:#fffbeb;--wnbd:#fde68a;
 --bad:#c81e1e;--badbg:#fef2f2;--badbd:#fecaca;--badbg2:#fee2e2;--bad2:#b91c1c;
 --sh:0 1px 2px rgba(16,24,40,.04),0 10px 28px -16px rgba(16,24,40,.20);
 --sh2:0 2px 6px rgba(16,24,40,.06),0 20px 44px -20px rgba(16,24,40,.30);
 --r:16px;--r2:12px;--r3:10px;
 --sans:system-ui,-apple-system,"Segoe UI","PingFang SC","HarmonyOS Sans SC","Noto Sans SC","Microsoft YaHei",sans-serif;
 --mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Consolas,"Liberation Mono",monospace;
}
html.dark{
 --bg:#0a1120;--bg2:#0d1526;
 --card:#111a2c;--card2:#141f34;--card3:#182540;
 --bd:#22304a;--bd2:#1b2740;--bdh:#33456a;--zeb:#131e33;
 --t1:#e8eefb;--t2:#aab8d0;--t3:#7d8ca6;
 --pri:#60a5fa;--pri2:#a78bfa;
 --ring:rgba(96,165,250,.25);--hov:rgba(96,165,250,.14);--hov2:rgba(96,165,250,.30);
 --ok:#34d399;--okbg:#0c2a22;--okbd:#1c5744;
 --wn:#fbbf24;--wnbg:#2a2211;--wnbd:#584418;
 --bad:#f87171;--badbg:#2b1416;--badbd:#5b2427;--badbg2:#3a1a1d;--bad2:#fca5a5;
 --sh:0 1px 2px rgba(0,0,0,.35),0 12px 30px -18px rgba(0,0,0,.70);
 --sh2:0 2px 8px rgba(0,0,0,.45),0 26px 52px -22px rgba(0,0,0,.80);
}

/* ===================== 基础 ===================== */
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font:14.5px/1.7 var(--sans);color:var(--t1);
 background:
  radial-gradient(1100px 460px at 12% -12%,rgba(37,99,235,.10),transparent 62%),
  radial-gradient(900px 400px at 100% -6%,rgba(124,58,237,.10),transparent 58%),
  linear-gradient(180deg,var(--bg),var(--bg2));
 background-attachment:fixed;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
 transition:background .3s,color .2s}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bd);border:3px solid transparent;background-clip:content-box;border-radius:99px}
::-webkit-scrollbar-thumb:hover{background:var(--t3);border:3px solid transparent;background-clip:content-box}
code,.mono{font-family:var(--mono)}

/* ===================== 顶部 ===================== */
.top{position:sticky;top:0;z-index:50}
header{position:relative;display:block;padding:12px 20px 11px;
 background:linear-gradient(180deg,rgba(255,255,255,.93),rgba(255,255,255,.80));
 -webkit-backdrop-filter:saturate(180%) blur(14px);backdrop-filter:saturate(180%) blur(14px);
 border-bottom:1px solid var(--bd)}
html.dark header{background:linear-gradient(180deg,rgba(17,26,44,.93),rgba(17,26,44,.80))}
header::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;
 background:linear-gradient(90deg,#2563eb 0%,#7c3aed 55%,#06b6d4 100%)}
.hw{position:relative;max-width:1140px;margin:0 auto;width:100%;display:flex;align-items:center;justify-content:center;gap:11px;flex-wrap:wrap}
.hmid{display:flex;align-items:center;justify-content:center;gap:11px;flex-wrap:wrap}
.hright{display:flex;align-items:center;gap:9px;padding-left:13px;border-left:1px solid var(--bd)}
nav .hw{gap:6px;justify-content:center}
.nodes{display:inline-flex;align-items:center;gap:6px;padding:6px 13px;border-radius:99px;
 border:1px solid var(--hov2);background:var(--hov);color:var(--pri);font-family:inherit;
 font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap;transition:.16s}
.nodes:hover{background:var(--hov2);transform:translateY(-1px)}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.mk{width:36px;height:36px;flex:0 0 auto;border-radius:11px;display:flex;align-items:center;justify-content:center;
 background:linear-gradient(135deg,#2563eb,#7c3aed);box-shadow:0 8px 20px -8px rgba(37,99,235,.75)}
.mk svg{width:21px;height:21px;display:block}
header h1{margin:0;font-size:16.5px;font-weight:700;letter-spacing:.2px;line-height:1.25}
header h1 small{display:block;font-size:11.5px;font-weight:500;color:var(--t3);letter-spacing:.3px;margin-top:1px}
header .pill{border:1px solid transparent;background:linear-gradient(135deg,#2563eb,#7c3aed);color:#fff;
 box-shadow:0 6px 14px -8px rgba(37,99,235,.9)}
header .pill.gray2{background:var(--card2);color:var(--t2);border-color:var(--bd);box-shadow:none}
.sp{flex:1}

/* ===================== 导航 ===================== */
nav{display:block;padding:8px 20px 10px;
 background:linear-gradient(180deg,rgba(255,255,255,.82),rgba(255,255,255,.55));
 border-bottom:1px solid var(--bd2)}
html.dark nav{background:linear-gradient(180deg,rgba(17,26,44,.82),rgba(17,26,44,.60))}
nav a{color:var(--t2);text-decoration:none;padding:7px 15px;font-size:13.8px;border-radius:99px;cursor:pointer;
 border:1px solid transparent;transition:background .16s,color .16s,transform .16s;user-select:none;white-space:nowrap}
nav a:hover{background:var(--hov);color:var(--pri)}
nav a.on{color:#fff;font-weight:600;background:linear-gradient(135deg,#2563eb,#6d28d9);
 box-shadow:0 8px 18px -10px rgba(37,99,235,.9)}

/* ===================== 布局 ===================== */
main{max-width:1140px;margin:20px auto 0;padding:0 18px 30px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:18px 20px;
 margin-bottom:18px;box-shadow:var(--sh);transition:box-shadow .2s,border-color .2s}
.card:hover{box-shadow:var(--sh2)}
.card h2{margin:0 0 14px;padding-bottom:12px;border-bottom:1px solid var(--bd2);
 font-size:15.5px;font-weight:700;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.card h2 .sp{flex:1}
.foot{display:flex;align-items:center;justify-content:center;gap:8px;flex-wrap:wrap;
 margin:26px 0 6px;padding-top:16px;border-top:1px dashed var(--bd);font-size:12px;color:var(--t3)}
.foot .dot{opacity:.45}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>div{flex:1;min-width:150px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}

/* ===================== 表单 ===================== */
label{display:block;font-size:12.8px;font-weight:500;color:var(--t2);margin:10px 0 5px}
input,select,textarea{width:100%;padding:9px 12px;border:1px solid var(--bd);border-radius:var(--r3);
 font:inherit;font-size:13.8px;background:var(--card);color:var(--t1);transition:border-color .15s,box-shadow .15s}
input::placeholder,textarea::placeholder{color:var(--t3)}
input:hover,select:hover,textarea:hover{border-color:var(--bdh)}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--pri);box-shadow:0 0 0 3px var(--ring)}
input[readonly]{background:var(--card3)!important;color:var(--t2);cursor:not-allowed}
textarea{min-height:104px;font-family:var(--mono);font-size:12.5px;line-height:1.65}
select{cursor:pointer}
input[type=checkbox]{width:auto;accent-color:var(--pri);cursor:pointer}
.chk{display:inline-flex;align-items:center;gap:7px;margin:4px 12px 4px 0;font-size:13.5px;color:var(--t1)}
.chk input{width:auto}

/* ===================== 按钮 ===================== */
.btn,.btn2,.iconbtn{font-family:inherit;cursor:pointer;transition:transform .16s,filter .16s,background .16s,border-color .16s,color .16s,box-shadow .16s;
 border-radius:var(--r3);font-weight:600;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;line-height:1.2}
.btn{padding:9px 16px;font-size:13.5px;color:#fff;border:0;
 background:linear-gradient(135deg,#2563eb,#4f46e5);box-shadow:0 8px 18px -10px rgba(37,99,235,.95)}
.btn:hover{filter:brightness(1.07);transform:translateY(-1px);box-shadow:0 12px 22px -12px rgba(37,99,235,1)}
.btn:active{transform:translateY(0)}
.btn.gray{background:linear-gradient(135deg,#64748b,#475569);box-shadow:0 8px 18px -12px rgba(71,85,105,.9)}
.btn.red{background:linear-gradient(135deg,#ef4444,#dc2626);box-shadow:0 8px 18px -12px rgba(220,38,38,.95)}
.btn2{padding:8px 14px;font-size:13px;color:var(--t1);background:var(--card);border:1px solid var(--bd)}
.btn2:hover{border-color:var(--pri);color:var(--pri);background:var(--hov)}
.btn2.red{color:var(--bad);border-color:var(--badbd);background:var(--badbg)}
.btn2.red:hover{color:var(--bad2);border-color:var(--bad);background:var(--badbg2)}
.iconbtn{width:36px;height:36px;padding:0;font-size:15px;border:1px solid var(--bd);background:var(--card);color:var(--t2)}
.iconbtn:hover{border-color:var(--pri);color:var(--pri);background:var(--hov);transform:translateY(-1px)}

/* ===================== 表格 ===================== */
table{width:100%;border-collapse:separate;border-spacing:0;font-size:13.5px}
th,td{text-align:left;padding:10px;border-bottom:1px solid var(--bd2);vertical-align:top}
th{color:var(--t2);font-weight:600;font-size:12.4px;letter-spacing:.3px;white-space:nowrap;
 background:var(--card2);border-bottom:1px solid var(--bd)}
tbody tr{transition:background .14s}
tbody tr:nth-child(even) td{background:var(--zeb)}
tbody tr:hover td{background:var(--hov)}
tbody tr:last-child td{border-bottom:0}
.card code{font-family:var(--mono);font-size:12.3px;background:var(--card3);padding:1px 6px;border-radius:6px;border:1px solid var(--bd2)}

/* ===================== 徽标 ===================== */
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:11.8px;font-weight:600;white-space:nowrap;vertical-align:middle;
 background:var(--okbg);color:var(--ok);border:1px solid var(--okbd)}
.pill.bad{background:var(--badbg);color:var(--bad);border-color:var(--badbd)}
.pill.warn{background:var(--wnbg);color:var(--wn);border-color:var(--wnbd)}
.pill.gray2{background:var(--card2);color:var(--t2);border-color:var(--bd)}
.vd{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;font-weight:600;white-space:nowrap;
 border:1px solid var(--bd);color:var(--t2);background:var(--card2)}
.vd.d{background:var(--okbg);border-color:var(--okbd);color:var(--ok)}
.vd.p{background:var(--hov);border-color:var(--hov2);color:var(--pri)}
.vd.o{background:var(--wnbg);border-color:var(--wnbd);color:var(--wn)}

/* ===================== 数据块 ===================== */
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.kv>div{position:relative;background:var(--card);border:1px solid var(--bd);border-radius:14px;
 padding:12px 14px 12px 17px;min-width:0;overflow:hidden;transition:border-color .18s,transform .18s}
.kv>div:hover{border-color:var(--hov2);transform:translateY(-1px)}
.kv>div::before{content:"";position:absolute;left:0;top:11px;bottom:11px;width:3px;border-radius:3px;
 background:linear-gradient(180deg,#2563eb,#7c3aed)}
.kv>div:nth-child(2)::before{background:linear-gradient(180deg,#06b6d4,#0ea5e9)}
.kv>div:nth-child(3)::before{background:linear-gradient(180deg,#10b981,#059669)}
.kv>div:nth-child(4)::before{background:linear-gradient(180deg,#f59e0b,#d97706)}
.kv>div:nth-child(5)::before{background:linear-gradient(180deg,#ec4899,#db2777)}
.kv>div:nth-child(6)::before{background:linear-gradient(180deg,#8b5cf6,#6d28d9)}
.kv>div:nth-child(7)::before{background:linear-gradient(180deg,#14b8a6,#0d9488)}
.kv b{display:block;font-size:17px;font-weight:700;letter-spacing:.2px;line-height:1.35}
.kv span{font-size:12px;color:var(--t3)}
.hint{font-size:12.5px;color:var(--t3);margin-top:8px;line-height:1.7}
.warnbox{position:relative;background:var(--wnbg);border:1px solid var(--wnbd);color:var(--wn);
 border-radius:var(--r2);padding:12px 16px;margin-bottom:16px;font-size:13.5px;font-weight:500;line-height:1.65}

/* ===================== 日志 ===================== */
#logs{white-space:pre-wrap;font-family:var(--mono);font-size:12px;line-height:1.75;max-height:360px;overflow:auto;
 background:#0b1220;color:#c7d2e0;padding:14px 16px;border-radius:var(--r2);border:1px solid #1e2a41}

/* ===================== 弹窗 ===================== */
#upbox2{display:none;position:fixed;inset:0;z-index:120;align-items:center;justify-content:center;padding:16px;
 background:rgba(10,16,30,.55);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
#upbox2 .box{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:24px;
 max-width:540px;width:100%;font-size:14px;box-shadow:var(--sh2);animation:pop .22s ease}
#upbox2 .box .err{color:var(--bad);font-weight:500}
#upbox2 .box .warn{color:var(--wn);font-weight:500}
#upbox2 .box .tip{color:var(--t3);font-size:12.5px}
@keyframes pop{from{opacity:0;transform:translateY(9px) scale(.985)}to{opacity:1;transform:none}}

/* ===================== 网络信息 ===================== */
.nig{display:grid;grid-template-columns:repeat(auto-fit,minmax(228px,1fr));gap:12px}
.nib{position:relative;border:1px solid var(--bd);border-radius:var(--r2);padding:12px 14px 12px 17px;
 background:var(--card2);overflow:hidden;transition:background .18s,border-color .18s,transform .18s}
.nib:hover{background:var(--card);border-color:var(--hov2);transform:translateY(-1px)}
.nib::before{content:"";position:absolute;left:0;top:12px;bottom:12px;width:3px;border-radius:3px;
 background:linear-gradient(180deg,#2563eb,#7c3aed)}
.nib .t{font-weight:700;font-size:13.2px;color:var(--t1);display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.nib .t span{font-weight:500;font-size:11px;color:var(--t3)}
.nib .v{font-family:var(--mono);font-size:15.5px;font-weight:600;color:var(--pri);margin:7px 0 3px;
 word-break:break-all;letter-spacing:.2px}
.nib .v i{cursor:pointer;font-style:normal;color:var(--t3);margin-left:6px;font-size:13px;transition:color .15s}
.nib .v i:hover{color:var(--pri)}
.nib .l{font-size:12px;color:var(--t2)}
.nib .l2{font-size:12px;color:var(--t3);margin-top:2px}
.nich{display:grid;grid-template-columns:repeat(auto-fit,minmax(162px,1fr));gap:9px;margin-top:12px}
.nic2{border:1px solid var(--bd);border-radius:var(--r3);padding:9px 11px;display:flex;align-items:center;gap:9px;
 font-size:12px;background:var(--card);transition:transform .16s,box-shadow .16s,border-color .16s}
.nic2:hover{transform:translateY(-1px);box-shadow:var(--sh);border-color:var(--hov2)}
.nic2 svg{width:20px;height:20px;flex:0 0 auto}
.nic2 .nm{flex:1 1 auto;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;color:var(--t2);font-weight:500}
.nic2 .rg{font-size:10px;color:var(--t3);margin-left:5px;font-weight:400}
.nic2 b{font-family:var(--mono);font-size:13px;margin-left:auto;font-weight:700}
.g2{color:#16a34a}.y2{color:#d97706}.r2{color:#dc2626}.w2{color:var(--t3)}
html.dark .g2{color:#4ade80}html.dark .y2{color:#fbbf24}html.dark .r2{color:#f87171}

/* ===================== IP 详情弹窗 ===================== */
#ipm{position:fixed;inset:0;z-index:120;display:none;align-items:center;justify-content:center;padding:14px;
 background:rgba(10,16,30,.55);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
#ipm.on{display:flex}
.ipmb{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);max-width:940px;width:100%;
 max-height:93vh;overflow:auto;box-shadow:var(--sh2);animation:pop .22s ease}
.ipmh{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:9px;padding:14px 16px;
 border-bottom:1px solid var(--bd2);font-weight:700;font-size:15px;background:var(--card)}
.ipmh .src{font-weight:500;font-size:12px;color:var(--t3)}
.ipmh button{margin-left:auto;border:1px solid var(--bd);background:var(--card2);color:var(--t2);
 border-radius:9px;width:32px;height:32px;cursor:pointer;font-size:16px;line-height:1;transition:.15s}
.ipmh button:hover{color:var(--bad);border-color:var(--badbd);background:var(--badbg)}
#ipmm{height:330px;background:var(--card3)}
.ipmi{display:grid;grid-template-columns:repeat(auto-fit,minmax(262px,1fr));gap:12px;padding:16px}
.ipmi h3{font-size:13.2px;margin:0 0 8px;color:var(--t1);font-weight:700}
.ipmi table{width:100%;font-size:13px}
.ipmi td{padding:6px 0;border-bottom:1px dashed var(--bd2);vertical-align:top}
.ipmi td:first-child{color:var(--t3);width:106px}
.ok2{color:var(--ok);font-weight:700}.no2{color:var(--t3)}

/* ===================== 卡片收起 / 展开 ===================== */
.card.collapsed .colbody{display:none}
.card.collapsed h2{padding-bottom:0;border-bottom:0;margin-bottom:0}

/* ===================== 彩色图标 ===================== */
.ic{width:28px;height:28px;border-radius:9px;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto}
.ic svg{width:16px;height:16px;display:block}
.ic.sm{width:23px;height:23px;border-radius:7px}
.ic.sm svg{width:13px;height:13px}
.ic-b{background:#e8f0ff;color:#2563eb}
.ic-c{background:#e0f7fb;color:#0891b2}
.ic-v{background:#f1eaff;color:#7c3aed}
.ic-r{background:#ffe8ee;color:#e11d48}
.ic-g{background:#e4f7ea;color:#059669}
.ic-a{background:#fff3e0;color:#d97706}
.ic-i{background:#e9ebff;color:#4f46e5}
.ic-t{background:#e0f5f2;color:#0d9488}
.ic-o{background:#ffefe4;color:#ea580c}
.ic-p{background:#ffe9f4;color:#db2777}
.ic-s{background:#eef1f6;color:#475569}
.ic-k{background:#e0f7fb;color:#0284c7}
html.dark .ic-b{background:rgba(37,99,235,.20);color:#8ab4ff}
html.dark .ic-c{background:rgba(8,145,178,.22);color:#67e8f9}
html.dark .ic-v{background:rgba(124,58,237,.22);color:#c4b5fd}
html.dark .ic-r{background:rgba(225,29,72,.20);color:#fda4af}
html.dark .ic-g{background:rgba(5,150,105,.22);color:#6ee7b7}
html.dark .ic-a{background:rgba(217,119,6,.22);color:#fcd34d}
html.dark .ic-i{background:rgba(79,70,229,.22);color:#a5b4fc}
html.dark .ic-t{background:rgba(13,148,136,.22);color:#5eead4}
html.dark .ic-o{background:rgba(234,88,12,.20);color:#fdba74}
html.dark .ic-p{background:rgba(219,39,119,.20);color:#f9a8d4}
html.dark .ic-s{background:rgba(71,85,105,.30);color:#cbd5e1}
html.dark .ic-k{background:rgba(2,132,199,.22);color:#7dd3fc}

/* 导航图标 */
nav a{display:inline-flex;align-items:center;gap:7px}
nav a .nv{width:15px;height:15px;flex:0 0 auto}
nav a.on .nv{color:#fff}
.nv-g{color:#059669}.nv-k{color:#0284c7}.nv-i{color:#4f46e5}
.nv-a{color:#d97706}.nv-v{color:#7c3aed}.nv-r{color:#e11d48}
html.dark .nv-g{color:#34d399}html.dark .nv-k{color:#38bdf8}html.dark .nv-i{color:#818cf8}
html.dark .nv-a{color:#fbbf24}html.dark .nv-v{color:#a78bfa}html.dark .nv-r{color:#fb7185}

/* 指标卡图标 */
.kv>div{display:flex;align-items:center;gap:11px}
.kv .kvi{width:34px;height:34px;border-radius:10px;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto}
.kv .kvi svg{width:17px;height:17px;display:block}
.kvt{min-width:0;flex:1 1 auto}
.kvt span{display:block;overflow-wrap:anywhere}

/* ===================== 深色兼容：覆盖 JS 写死的内联浅色 ===================== */
html.dark [style*="#e6ecf5"]{border-color:var(--bd2)!important}
html.dark [style*="color:#d92d20"]{color:var(--bad)!important}
html.dark [style*="color:#047857"]{color:var(--ok)!important}
html.dark [style*="color:#b45309"]{color:var(--wn)!important}
html.dark [style*="color:#94a3b8"]{color:var(--t3)!important}
html.dark [style*="color:#58a6ff"]{color:var(--pri)!important}
html.dark [style*="background:#f2f4f7"]{background:var(--card3)!important}
html.dark [style*="background:#98a2b3"]{background:#3a4a66!important}
/* ===================== 响应式 ===================== */
@media(max-width:720px){
 header{padding:10px 14px}
 .hw{gap:9px}
 .hright{padding-left:0;border-left:0}
 nav .hw{justify-content:flex-start}
 .nodes{padding:5px 10px;font-size:12px}
 header h1{font-size:15px}
 header h1 small{display:none}
 nav{padding:7px 14px 9px}
 nav .hw{gap:5px;flex-wrap:nowrap;overflow-x:auto;padding-bottom:2px}
 nav a{padding:6px 12px;font-size:13px}
 main{padding:0 12px 24px;margin-top:14px}
 .card{padding:14px;border-radius:14px}
 th,td{padding:8px 7px}
 .kv{grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}
 .grid2{grid-template-columns:1fr}
}
</style></head><body>
<div class=top>
<header>
 <div class=hw>
  <div class=hmid>
   <span class=mk><svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3.2v5.9c0 4.2-2.9 7.7-7 9-4.1-1.3-7-4.8-7-9V6.2z"/><path d="M8.8 12.2l2.1 2.1 4.3-4.6"/></svg></span>
   <h1>mihomo 网页管理台<small>群晖 NAS · 单端口旁路由</small></h1>
   <span class="pill" id=ver>v2.9.7</span>
   <span class="pill gray2" id=hd>群晖 NAS</span>
   <button id=mhuiBtn class=nodes onclick="(function(){var d=document.getElementById('mhui');var f=document.getElementById('mhuiF');if(d.style.display==='block'){d.style.display='none';}else{d.style.display='block';if(!f.getAttribute('src'))f.setAttribute('src','/ui/');}})()">节点面板（内嵌）</button>
  </div>
  <div class=hright>
   <button class=iconbtn id=thm onclick="thmTgl()" title="切换深色 / 浅色">🌙</button>
   <a class="btn gray" href="/logout">退出登录</a>
  </div>
 </div>
</header>
<nav>
 <div class=hw>
 <a data-t=status class=on><svg class="nv nv-g" viewBox="0 0 24 24"><path d="M2 12h4l3-7 4 14 3-7h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>状态</a><a data-t=airports><svg class="nv nv-k" viewBox="0 0 24 24"><path d="M6 18h11a4 4 0 0 0 .7-8 6 6 0 0 0-11.6 1.4A3.6 3.6 0 0 0 6 18z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>机场</a><a data-t=nodes><svg class="nv nv-i" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="7" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><rect x="3" y="13" width="18" height="7" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M7 7.5h.01M7 16.5h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>节点</a><a data-t=groups><svg class="nv nv-a" viewBox="0 0 24 24"><path d="M4 7h9M18 7h2M4 17h3M12 17h8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><circle cx="15.5" cy="7" r="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><circle cx="9.5" cy="17" r="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>策略</a><a data-t=sublinks><svg class="nv nv-v" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>订阅</a><a data-t=tools><svg class="nv nv-r" viewBox="0 0 24 24"><path d="M15 3a5 5 0 0 0-4.6 7L4 16.4V20h3.6l6.4-6.4A5 5 0 0 0 21 9l-3 3-3-3 3-3a5 5 0 0 0-3-3z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>工具</a>
 </div>
</nav>
</div>
<main>
 <div id=wp class=warnbox style="display:none">⚠️ 你还在用默认密码，请立刻到「工具」页改掉，否则同网段任何人都能打开管理台并看到你的节点。</div>
 <section id=tab-status>
  <div class=card><h2><span class="ic ic-b"><svg viewBox="0 0 24 24"><path d="M2 12h4l3-7 4 14 3-7h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 运行状态 <span class=sp></span>
    <button class="btn" onclick="gen()">立即更新配置</button></h2>
   <div class=kv id=kv></div>
   <div id=kupd></div></div>
  <div class=card><h2><span class="ic ic-c"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M3 12h18M12 3c2.6 3 2.6 15 0 18-2.6-3-2.6-15 0-18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 当前网络信息 <span class=sp></span>
    <button class=btn2 onclick="niLoad(1)">刷新</button>
    <label class=chk style="float:none;display:inline-block;margin-left:10px;font-weight:400;font-size:12px"><input type=checkbox id=niauto checked onchange="niAutoToggle()"> 自动刷新</label></h2>
   <div id=ni></div>
   <div id=nil></div>
   <div class=hint>国内测试走<b>直连</b>，国外 / 墙外走<b>代理</b>（都是面板所在群晖的真实出口）；延迟为群晖经 mihomo 实测（同一条连接预热后取最小值，所以比首次冷连接低）；<span id=nit></span></div></div>
  <div class=card><h2><span class="ic ic-v"><svg viewBox="0 0 24 24"><path d="M9 3h6M10 3v6L5 18a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M7.5 14h9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 分流测试 <span class=sp></span>
    <button class=btn2 data-body=stbody onclick="colToggle(this)">展开</button>
    <button class=btn2 onclick="stLoad(1)">开始测试</button></h2>
   <div class="colbody" id=stbody>
   <table><thead><tr><th>业务组</th><th>代表站点</th><th>命中规则</th><th>实际策略链</th><th>判定</th><th>延迟</th></tr></thead><tbody id=st></tbody></table>
   <div class=hint id=sth>点「开始测试」：每个业务组各发一个真实请求，同时读 mihomo 的连接记录，看它<b>命中了哪条规则</b>、<b>实际走了哪条策略链</b>（链尾就是最终业务组），再和该组应有的走向比对。</div>
   <div class=hint>延迟取该站点 <code>favicon</code> / <code>robots.txt</code> / <code>首页</code> 三者中最快的那个（小资源、只等响应头），所以不是页面加载时间；个别站点（如 OneDrive）本身落地线路慢，数值偏高是真实的。</div>
   </div>
  </div>

  <div class=card><h2><span class="ic ic-r"><svg viewBox="0 0 24 24"><path d="M12 20s-7-4.4-7-9a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 4.6-7 9-7 9z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M8 12h2l1.5-2 2 4 1.5-2h1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 健康自检<span id=hccount></span> <span class=sp></span>
    <button class=btn2 data-body=hcbody onclick="colToggle(this)">展开</button>
    <button class=btn2 id=hcbtn onclick="hcLoad(1)">重新自检</button></h2>
   <div class="colbody" id=hcbody><table><tbody id=hc></tbody></table>
   <div class=hint>健康自检会真实尝试：容器状态、配置校验、经代理出网、面板接口、磁盘、程序文件、定时任务。</div>
   </div>
  </div>
  <div class=card><h2><span class="ic ic-k"><svg viewBox="0 0 24 24"><path d="M12 3l8 4v10l-8 4-8-4V7z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M4 7l8 4 8-4M12 11v10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 代理提供者（机场） <span class=sp></span>
    <button class=btn2 data-body=pvbody onclick="colToggle(this)">展开</button></h2>
   <div class="colbody" id=pvbody><table>
   <thead><tr><th>提供者</th><th>节点数</th><th>更新时间</th></tr></thead><tbody id=pv></tbody></table></div></div>
 </section>

 <section id=tab-airports hidden>
  <div class=card><h2><span class="ic ic-t"><svg viewBox="0 0 24 24"><path d="M6 17h10a3.6 3.6 0 0 0 .6-7.2A5.4 5.4 0 0 0 6.6 11 3.2 3.2 0 0 0 6 17z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M11 21v-6M8.5 17.5 11 15l2.5 2.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 添加 / 修改机场订阅</h2>
   <div class=row><div><label>名称（英文/数字，作为提供者名）</label><input id=ap_n placeholder=airport></div>
   <div style="flex:2"><label>订阅链接（Clash / mihomo 格式）</label><input id=ap_u placeholder=https://...></div>
   <div><label>更新间隔（秒）</label><input id=ap_i value=86400></div></div>
   <div style="margin-top:12px"><button class=btn onclick="apSave()">保存并生成配置</button></div>
   <div class=hint>机场会生成独立的 <b>proxy-providers</b>，策略组用 <code>use</code> 引用；换机场只改这里。</div></div>
  <div class=card><h2><span class="ic ic-s"><svg viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 当前机场</h2><table>
   <thead><tr><th>名称</th><th>订阅链接</th><th>间隔</th><th>操作</th></tr></thead><tbody id=apt></tbody></table></div>
 </section>

 <section id=tab-nodes hidden>
  <div class=card><h2><span class="ic ic-i"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="7" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><rect x="3" y="13" width="18" height="7" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M7 7.5h.01M7 16.5h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 添加手动节点</h2>
   <label>批量粘贴分享链接，一行一个（支持 vmess / vless(含 reality) / ss / trojan / hysteria2 / hysteria / tuic / anytls / http / socks5，也可贴 Clash 风格单行）</label>
   <textarea id=nd_t placeholder="vless://...&#10;vmess://..."></textarea>
   <div style="margin-top:12px"><button class=btn onclick="ndAdd()">解析并添加</button>
     <button class="btn red" onclick="ndClear()">清空全部手动节点</button></div>
   <div class=hint id=nd_h>当前手动节点：0 个</div></div>
  <div class=card><h2><span class="ic ic-s"><svg viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 手动节点列表</h2><table>
   <thead><tr><th>名称</th><th>类型</th><th>服务器</th><th>端口</th><th>操作</th></tr></thead><tbody id=ndt></tbody></table></div>
 </section>

 <section id=tab-groups hidden>
  <div class=card><h2><span class="ic ic-c"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M3 12h18M12 3c2.6 3 2.6 15 0 18-2.6-3-2.6-15 0-18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 地区组（三件套）</h2><div id=rg></div>
   <div class=hint>勾选的地区会生成 手动(select) / 故转(fallback) / 自动(url-test) 三个策略组。</div></div>
  <div class=card><h2><span class="ic ic-a"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 业务组默认线路</h2><div class=grid2 id=bg></div>
   <div class=hint>TikTok 与 AI 默认走「狮城自动」（香港上不了 TikTok）；其余业务组默认走各自优先地区的「故转」，可自行改动。</div></div>
  <div class=card><h2><span class="ic ic-v"><svg viewBox="0 0 24 24"><path d="M12 3l9 5-9 5-9-5z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M3 13l9 5 9-5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 自定义策略组（自由增减）</h2>
   <div id=cg></div>
   <div style="margin-top:10px"><button class=btn onclick="cgAdd()">➕ 添加策略组</button>
     <span class=hint style="margin-left:8px">成员填名字、逗号分隔（如 <code>香港自动,日本故转</code>，也可直接写节点名）；成员留空则按「过滤规则」自动收录节点，过滤规则也留空 = 收录全部节点。</span></div>
   <div class=hint>支持类型：select（手动选）/ url-test（自动测速）/ fallback（故障转移）/ load-balance（负载均衡）。改完记得点下面「保存策略并生成配置」。</div>
  </div>
  <div class=card><h2><span class="ic ic-g"><svg viewBox="0 0 24 24"><path d="M12 4v16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M6 8h6M12 14h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M9 5.5 6 8l3 2.5M15 11.5 18 14l-3 2.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 自定义分流（按网站选直连/代理）</h2>
   <div id=cr></div>
   <div style="margin-top:10px"><button class=btn onclick="crAdd()">➕ 添加一行</button>
    <span class=hint style="margin-left:8px">域名写 <code>example.com</code> 就行（自动包含它自己和所有子域）；去向可选 <b>直连</b> / <b>代理（漏网之鱼）</b> / 任意地区组或自定义组。改完点下面「保存策略并生成配置」✓</span></div>
   <div class=hint>这条规则排在规则集<b>前面</b>，所以优先级最高：选了直连就一定直连，选了某个组就一定走那个组。</div></div>

  <div class=card><h2><span class="ic ic-o"><svg viewBox="0 0 24 24"><path d="M4 5h16M7 12h10M10 19h4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 规则集分流</h2>
   <div id=rs></div>
   <div style="margin-top:8px"><button class=btn2 onclick="rsAdd()">➕ 添加自定义规则集</button>
    <button class=btn2 onclick="rsReset()">全部恢复默认地址</button></div>
   <div class=hint style="margin-top:6px">默认地址来自 <code>MetaCubeX/meta-rules-dat</code>：<code>https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@meta/geo/geosite/&lt;名字&gt;.mrs</code>（AI=category-ai-!cn、GitHub=github、YouTube=youtube…）；<code>proxylite</code> 来自 <code>https://cdn.jsdelivr.net/gh/qichiyuhub/rule@main/rules/proxy.list</code>。想换镜像或换成自己的规则集，直接把地址改掉即可（留空＝用默认）；填错了点「全部恢复默认地址」。</div>
   <label class=chk><input type=checkbox id=rs_en onchange=rsToggle()> 启用规则集分流（七尺宇 qichiyuhub/rule + MetaCubeX meta-rules-dat）</label>
   <div class=hint>关闭后只保留基础规则（国内直连、其余走漏网之鱼）。</div></div>
  <div style="margin-top:8px"><button class=btn onclick="grSave()">保存策略并生成配置</button></div>
 </section>

 <section id=tab-tools hidden>
  <div class=card><h2><span class="ic ic-k"><svg viewBox="0 0 24 24"><path d="M9 3v6M15 3v6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M6 9h12v3a6 6 0 0 1-12 0z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M12 18v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 端口与地址</h2>
   <div class=row>
    <div><label>绑定 IP（本机地址）</label><input id=t_ip readonly style="background:#f2f4f7;color:#98a2b3;cursor:not-allowed"></div>
    <div><label>管理台绑定</label><input id=t_ab readonly style="background:#f2f4f7;color:#98a2b3;cursor:not-allowed"></div>
    <div><label>代理端口</label><input id=t_pp></div>
    <div><label>内核接口端口（仅本机）</label><input id=t_pa></div>
    <div><label>管理台端口（对外唯一）</label><input id=t_ad></div>
   </div>
   <div class=row><div><label>管理台密码（= 面板密钥）</label><input id=t_sc></div></div>
   <div class=row><div><label>直连域名（逗号分隔，填你自己的域名）</label><input id=t_dd placeholder="freet.cc"></div>
    <div style="align-self:flex-end"><button class=btn2 onclick="ddSave()">保存</button></div></div>
   <div class=hint>填你<b>自己的域名</b>（如 <code>freet.cc</code>）：访问面板/订阅/自建服务时<b>不走代理</b>，并且 DNS <b>返回真实 IP、不会被 fake-ip 拦住</b>（这样电脑上也能正常打开自己的域名 ✓）。改完点上面「保存」✓，再点「立即更新配置」生效 ✓。</div>
   <div style="margin-top:12px"><button class=btn onclick="tlSave()">保存</button>
    <button class="btn gray" onclick="applyAll()">应用并重建容器</button>
    <span class=hint style="margin-left:8px">保存密码</span></div>
   <div class=hint><b>单端口设计</b>：节点面板（<code>/ui/</code>）和管理台「共用一个端口」= 管理台端口，对外只需要放通这一个。
    <b>内核接口端口</b>（默认 9091）只绑 <code>127.0.0.1</code>，是管理台和内核之间的内部通道，不对外、也不用在路由器上放通。</div>
   <div class=hint>改<b>密码</b>：点「保存」即可 ✓；改<b>端口</b>：先点「保存」✓，再点「应用并重建容器」才算生效 ✓</div></div>

  <div class=card><h2><span class="ic ic-p"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M12 8v4l3 2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 定时自动更新</h2>
   <label class=chk><input type=checkbox id=t_sch> 启用</label>
   <div class=row><div><label>每天更新时间（HH:MM）</label><input id=t_st placeholder=04:00></div></div>
   <label class=chk style="margin-top:10px"><input type=checkbox id=t_au onchange="auToggle()">
    自动检查更新</label>
   <div style="margin-top:12px"><button class=btn onclick="tlSave()">保存</button></div></div>

  <div class=card><h2><span class="ic ic-s"><svg viewBox="0 0 24 24"><path d="M15 3a5 5 0 0 0-4.6 7L4 16.4V20h3.6l6.4-6.4A5 5 0 0 0 21 9l-3 3-3-3 3-3a5 5 0 0 0-3-3z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 维护工具</h2>
   <div class=row>
    <div><button class=btn onclick="gen()">立即更新配置</button><div class=hint>重新生成 + 校验 + 热重载</div></div>
    <div><button class=btn onclick="chkUpd()">检查更新</button><div class=hint>看仓库有没有新版 <span id=upTip></span></div></div>
    <div><button class=btn onclick="selfUpd()">更新管理台程序</button><div class=hint>拉取最新 + 校验 + 自动重启</div></div>
    <div><button class=btn onclick="apiPost('/api/backup',{},1)">立即备份配置</button><div class=hint>保留最近 10 份</div></div>
    <div><button class="btn red" onclick="kdRestart()">重启内核容器</button><div class=hint>热重载不生效时用</div></div>
   </div></div>

  <div class=card><h2><span class="ic ic-t"><svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="8" ry="3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 备份与回滚</h2><table>
   <thead><tr><th>备份文件</th><th>大小</th><th>时间</th><th>操作</th></tr></thead><tbody id=bk></tbody></table></div>

  <div class=card><h2><span class="ic ic-i"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M7 9l3 3-3 3M13 15h4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 运行日志</h2>
   <div style="margin-bottom:8px"><button class=btn onclick="loadLogs()">刷新日志</button></div>
   <div id=logs>点击「刷新日志」加载</div></div>
 </section>

 <section id=tab-sublinks hidden>
  <div class=card><h2><span class="ic ic-v"><svg viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 订阅生成 <span class=sp></span><span class=hint>把自己的订阅变成一个可分享的链接</span></h2>
   <div class=hint><b>自定义短链</b>：填了就得到 <code>/sub/你的名字</code>（也支持 <code>/c/你的名字</code> ✓）；不填自动给随机串 ✓。建好后列表里可点「改短链」✓ 改完旧链接立刻失效 ✓。<br><b>自适应链接</b>：同一个链接会按客户端自动返回 Clash / V2Ray / sing-box / Surge ✓ 朋友不用选格式 ✓。</div>
   <div class=hint>把<b>节点</b>（<code>vmess://</code> <code>vless://</code> <code>ss://</code> <code>trojan://</code> <code>hysteria2://</code> 等分享链接，每行一个，可多行）贴进下面 → 保存后就得到一条<b>属于你自己的订阅（机场）链接</b>：<b>Clash</b>（含你面板这套策略组）和 <b>V2Ray</b>（base64）。<br>也可以直接贴<b>订阅链接</b>（<code>http</code> 开头）→ 会当作上游机场合并进来。数据只存在你家 NAS（<code>/data/sublinks.json</code>），不经过任何第三方。</div>
   <div class=row style="margin-top:10px">
    <div style="flex:1 1 260px"><label>名称</label><input id=sb_name placeholder="例如 给朋友的香港节点"></div>
    <div style="flex:1 1 260px"><label>自定义短链（选填）</label><input id=sb_code placeholder="例如 abner → /sub/abner"></div>
    <div style="flex:0 0 150px"><label>&nbsp;</label><button class=btn onclick="sbSave()">保存 / 新增</button></div>
   </div>
   <div style="margin-top:10px"><label>节点 / 订阅来源（每行一条：分享链接或 http 订阅链接）</label>
    <textarea id=sb_urls rows=3 style="width:100%;padding:9px 10px;border:1px solid #d0d5dd;border-radius:8px;font-size:13px" placeholder="vmess://eyJhZGQiOiIxLjIuMy40Ii4uLg==&#10;vless://uuid@example.com:443?type=ws#示例节点&#10;ss://YWVzLTI1Ni1nY206cGFzcw==@1.2.3.4:8388#香港01"></textarea></div>
   <div style="margin-top:10px"><label>只保留这些地区（不勾 = 全部；对 Clash 输出生效）</label>
    <div id=sb_rg style="display:flex;gap:14px;flex-wrap:wrap;margin-top:6px"></div></div>
  </div>
  <div class=card><h2><span class="ic ic-a"><svg viewBox="0 0 24 24"><rect x="6" y="4" width="12" height="17" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M9 4V3h6v1M9 10h6M9 14h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 已有订阅</h2><div id=sb_list></div></div>
 </section>
<div class=foot><span>mihomo 网页管理台</span><span class=dot>·</span><span>群晖 NAS · 单端口旁路由</span><span class=dot>·</span><span>配置改动自动热重载</span></div>
</main>
<div id=upbox2><div class=box id=upmsg></div></div>
<script>
/* ==== QR Code Generator for JavaScript v1.4.4 ====
   Copyright (c) 2009 Kazuhiko Arase / MIT License
   https://github.com/kazuhikoarase/qrcode-generator */
//---------------------------------------------------------------------
//
// QR Code Generator for JavaScript
//
// Copyright (c) 2009 Kazuhiko Arase
//
// URL: http://www.d-project.com/
//
// Licensed under the MIT license:
//  http://www.opensource.org/licenses/mit-license.php
//
// The word 'QR Code' is registered trademark of
// DENSO WAVE INCORPORATED
//  http://www.denso-wave.com/qrcode/faqpatent-e.html
//
//---------------------------------------------------------------------

var qrcode = function() {

  //---------------------------------------------------------------------
  // qrcode
  //---------------------------------------------------------------------

  /**
   * qrcode
   * @param typeNumber 1 to 40
   * @param errorCorrectionLevel 'L','M','Q','H'
   */
  var qrcode = function(typeNumber, errorCorrectionLevel) {

    var PAD0 = 0xEC;
    var PAD1 = 0x11;

    var _typeNumber = typeNumber;
    var _errorCorrectionLevel = QRErrorCorrectionLevel[errorCorrectionLevel];
    var _modules = null;
    var _moduleCount = 0;
    var _dataCache = null;
    var _dataList = [];

    var _this = {};

    var makeImpl = function(test, maskPattern) {

      _moduleCount = _typeNumber * 4 + 17;
      _modules = function(moduleCount) {
        var modules = new Array(moduleCount);
        for (var row = 0; row < moduleCount; row += 1) {
          modules[row] = new Array(moduleCount);
          for (var col = 0; col < moduleCount; col += 1) {
            modules[row][col] = null;
          }
        }
        return modules;
      }(_moduleCount);

      setupPositionProbePattern(0, 0);
      setupPositionProbePattern(_moduleCount - 7, 0);
      setupPositionProbePattern(0, _moduleCount - 7);
      setupPositionAdjustPattern();
      setupTimingPattern();
      setupTypeInfo(test, maskPattern);

      if (_typeNumber >= 7) {
        setupTypeNumber(test);
      }

      if (_dataCache == null) {
        _dataCache = createData(_typeNumber, _errorCorrectionLevel, _dataList);
      }

      mapData(_dataCache, maskPattern);
    };

    var setupPositionProbePattern = function(row, col) {

      for (var r = -1; r <= 7; r += 1) {

        if (row + r <= -1 || _moduleCount <= row + r) continue;

        for (var c = -1; c <= 7; c += 1) {

          if (col + c <= -1 || _moduleCount <= col + c) continue;

          if ( (0 <= r && r <= 6 && (c == 0 || c == 6) )
              || (0 <= c && c <= 6 && (r == 0 || r == 6) )
              || (2 <= r && r <= 4 && 2 <= c && c <= 4) ) {
            _modules[row + r][col + c] = true;
          } else {
            _modules[row + r][col + c] = false;
          }
        }
      }
    };

    var getBestMaskPattern = function() {

      var minLostPoint = 0;
      var pattern = 0;

      for (var i = 0; i < 8; i += 1) {

        makeImpl(true, i);

        var lostPoint = QRUtil.getLostPoint(_this);

        if (i == 0 || minLostPoint > lostPoint) {
          minLostPoint = lostPoint;
          pattern = i;
        }
      }

      return pattern;
    };

    var setupTimingPattern = function() {

      for (var r = 8; r < _moduleCount - 8; r += 1) {
        if (_modules[r][6] != null) {
          continue;
        }
        _modules[r][6] = (r % 2 == 0);
      }

      for (var c = 8; c < _moduleCount - 8; c += 1) {
        if (_modules[6][c] != null) {
          continue;
        }
        _modules[6][c] = (c % 2 == 0);
      }
    };

    var setupPositionAdjustPattern = function() {

      var pos = QRUtil.getPatternPosition(_typeNumber);

      for (var i = 0; i < pos.length; i += 1) {

        for (var j = 0; j < pos.length; j += 1) {

          var row = pos[i];
          var col = pos[j];

          if (_modules[row][col] != null) {
            continue;
          }

          for (var r = -2; r <= 2; r += 1) {

            for (var c = -2; c <= 2; c += 1) {

              if (r == -2 || r == 2 || c == -2 || c == 2
                  || (r == 0 && c == 0) ) {
                _modules[row + r][col + c] = true;
              } else {
                _modules[row + r][col + c] = false;
              }
            }
          }
        }
      }
    };

    var setupTypeNumber = function(test) {

      var bits = QRUtil.getBCHTypeNumber(_typeNumber);

      for (var i = 0; i < 18; i += 1) {
        var mod = (!test && ( (bits >> i) & 1) == 1);
        _modules[Math.floor(i / 3)][i % 3 + _moduleCount - 8 - 3] = mod;
      }

      for (var i = 0; i < 18; i += 1) {
        var mod = (!test && ( (bits >> i) & 1) == 1);
        _modules[i % 3 + _moduleCount - 8 - 3][Math.floor(i / 3)] = mod;
      }
    };

    var setupTypeInfo = function(test, maskPattern) {

      var data = (_errorCorrectionLevel << 3) | maskPattern;
      var bits = QRUtil.getBCHTypeInfo(data);

      // vertical
      for (var i = 0; i < 15; i += 1) {

        var mod = (!test && ( (bits >> i) & 1) == 1);

        if (i < 6) {
          _modules[i][8] = mod;
        } else if (i < 8) {
          _modules[i + 1][8] = mod;
        } else {
          _modules[_moduleCount - 15 + i][8] = mod;
        }
      }

      // horizontal
      for (var i = 0; i < 15; i += 1) {

        var mod = (!test && ( (bits >> i) & 1) == 1);

        if (i < 8) {
          _modules[8][_moduleCount - i - 1] = mod;
        } else if (i < 9) {
          _modules[8][15 - i - 1 + 1] = mod;
        } else {
          _modules[8][15 - i - 1] = mod;
        }
      }

      // fixed module
      _modules[_moduleCount - 8][8] = (!test);
    };

    var mapData = function(data, maskPattern) {

      var inc = -1;
      var row = _moduleCount - 1;
      var bitIndex = 7;
      var byteIndex = 0;
      var maskFunc = QRUtil.getMaskFunction(maskPattern);

      for (var col = _moduleCount - 1; col > 0; col -= 2) {

        if (col == 6) col -= 1;

        while (true) {

          for (var c = 0; c < 2; c += 1) {

            if (_modules[row][col - c] == null) {

              var dark = false;

              if (byteIndex < data.length) {
                dark = ( ( (data[byteIndex] >>> bitIndex) & 1) == 1);
              }

              var mask = maskFunc(row, col - c);

              if (mask) {
                dark = !dark;
              }

              _modules[row][col - c] = dark;
              bitIndex -= 1;

              if (bitIndex == -1) {
                byteIndex += 1;
                bitIndex = 7;
              }
            }
          }

          row += inc;

          if (row < 0 || _moduleCount <= row) {
            row -= inc;
            inc = -inc;
            break;
          }
        }
      }
    };

    var createBytes = function(buffer, rsBlocks) {

      var offset = 0;

      var maxDcCount = 0;
      var maxEcCount = 0;

      var dcdata = new Array(rsBlocks.length);
      var ecdata = new Array(rsBlocks.length);

      for (var r = 0; r < rsBlocks.length; r += 1) {

        var dcCount = rsBlocks[r].dataCount;
        var ecCount = rsBlocks[r].totalCount - dcCount;

        maxDcCount = Math.max(maxDcCount, dcCount);
        maxEcCount = Math.max(maxEcCount, ecCount);

        dcdata[r] = new Array(dcCount);

        for (var i = 0; i < dcdata[r].length; i += 1) {
          dcdata[r][i] = 0xff & buffer.getBuffer()[i + offset];
        }
        offset += dcCount;

        var rsPoly = QRUtil.getErrorCorrectPolynomial(ecCount);
        var rawPoly = qrPolynomial(dcdata[r], rsPoly.getLength() - 1);

        var modPoly = rawPoly.mod(rsPoly);
        ecdata[r] = new Array(rsPoly.getLength() - 1);
        for (var i = 0; i < ecdata[r].length; i += 1) {
          var modIndex = i + modPoly.getLength() - ecdata[r].length;
          ecdata[r][i] = (modIndex >= 0)? modPoly.getAt(modIndex) : 0;
        }
      }

      var totalCodeCount = 0;
      for (var i = 0; i < rsBlocks.length; i += 1) {
        totalCodeCount += rsBlocks[i].totalCount;
      }

      var data = new Array(totalCodeCount);
      var index = 0;

      for (var i = 0; i < maxDcCount; i += 1) {
        for (var r = 0; r < rsBlocks.length; r += 1) {
          if (i < dcdata[r].length) {
            data[index] = dcdata[r][i];
            index += 1;
          }
        }
      }

      for (var i = 0; i < maxEcCount; i += 1) {
        for (var r = 0; r < rsBlocks.length; r += 1) {
          if (i < ecdata[r].length) {
            data[index] = ecdata[r][i];
            index += 1;
          }
        }
      }

      return data;
    };

    var createData = function(typeNumber, errorCorrectionLevel, dataList) {

      var rsBlocks = QRRSBlock.getRSBlocks(typeNumber, errorCorrectionLevel);

      var buffer = qrBitBuffer();

      for (var i = 0; i < dataList.length; i += 1) {
        var data = dataList[i];
        buffer.put(data.getMode(), 4);
        buffer.put(data.getLength(), QRUtil.getLengthInBits(data.getMode(), typeNumber) );
        data.write(buffer);
      }

      // calc num max data.
      var totalDataCount = 0;
      for (var i = 0; i < rsBlocks.length; i += 1) {
        totalDataCount += rsBlocks[i].dataCount;
      }

      if (buffer.getLengthInBits() > totalDataCount * 8) {
        throw 'code length overflow. ('
          + buffer.getLengthInBits()
          + '>'
          + totalDataCount * 8
          + ')';
      }

      // end code
      if (buffer.getLengthInBits() + 4 <= totalDataCount * 8) {
        buffer.put(0, 4);
      }

      // padding
      while (buffer.getLengthInBits() % 8 != 0) {
        buffer.putBit(false);
      }

      // padding
      while (true) {

        if (buffer.getLengthInBits() >= totalDataCount * 8) {
          break;
        }
        buffer.put(PAD0, 8);

        if (buffer.getLengthInBits() >= totalDataCount * 8) {
          break;
        }
        buffer.put(PAD1, 8);
      }

      return createBytes(buffer, rsBlocks);
    };

    _this.addData = function(data, mode) {

      mode = mode || 'Byte';

      var newData = null;

      switch(mode) {
      case 'Numeric' :
        newData = qrNumber(data);
        break;
      case 'Alphanumeric' :
        newData = qrAlphaNum(data);
        break;
      case 'Byte' :
        newData = qr8BitByte(data);
        break;
      case 'Kanji' :
        newData = qrKanji(data);
        break;
      default :
        throw 'mode:' + mode;
      }

      _dataList.push(newData);
      _dataCache = null;
    };

    _this.isDark = function(row, col) {
      if (row < 0 || _moduleCount <= row || col < 0 || _moduleCount <= col) {
        throw row + ',' + col;
      }
      return _modules[row][col];
    };

    _this.getModuleCount = function() {
      return _moduleCount;
    };

    _this.make = function() {
      if (_typeNumber < 1) {
        var typeNumber = 1;

        for (; typeNumber < 40; typeNumber++) {
          var rsBlocks = QRRSBlock.getRSBlocks(typeNumber, _errorCorrectionLevel);
          var buffer = qrBitBuffer();

          for (var i = 0; i < _dataList.length; i++) {
            var data = _dataList[i];
            buffer.put(data.getMode(), 4);
            buffer.put(data.getLength(), QRUtil.getLengthInBits(data.getMode(), typeNumber) );
            data.write(buffer);
          }

          var totalDataCount = 0;
          for (var i = 0; i < rsBlocks.length; i++) {
            totalDataCount += rsBlocks[i].dataCount;
          }

          if (buffer.getLengthInBits() <= totalDataCount * 8) {
            break;
          }
        }

        _typeNumber = typeNumber;
      }

      makeImpl(false, getBestMaskPattern() );
    };

    _this.createTableTag = function(cellSize, margin) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var qrHtml = '';

      qrHtml += '<table style="';
      qrHtml += ' border-width: 0px; border-style: none;';
      qrHtml += ' border-collapse: collapse;';
      qrHtml += ' padding: 0px; margin: ' + margin + 'px;';
      qrHtml += '">';
      qrHtml += '<tbody>';

      for (var r = 0; r < _this.getModuleCount(); r += 1) {

        qrHtml += '<tr>';

        for (var c = 0; c < _this.getModuleCount(); c += 1) {
          qrHtml += '<td style="';
          qrHtml += ' border-width: 0px; border-style: none;';
          qrHtml += ' border-collapse: collapse;';
          qrHtml += ' padding: 0px; margin: 0px;';
          qrHtml += ' width: ' + cellSize + 'px;';
          qrHtml += ' height: ' + cellSize + 'px;';
          qrHtml += ' background-color: ';
          qrHtml += _this.isDark(r, c)? '#000000' : '#ffffff';
          qrHtml += ';';
          qrHtml += '"/>';
        }

        qrHtml += '</tr>';
      }

      qrHtml += '</tbody>';
      qrHtml += '</table>';

      return qrHtml;
    };

    _this.createSvgTag = function(cellSize, margin, alt, title) {

      var opts = {};
      if (typeof arguments[0] == 'object') {
        // Called by options.
        opts = arguments[0];
        // overwrite cellSize and margin.
        cellSize = opts.cellSize;
        margin = opts.margin;
        alt = opts.alt;
        title = opts.title;
      }

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      // Compose alt property surrogate
      alt = (typeof alt === 'string') ? {text: alt} : alt || {};
      alt.text = alt.text || null;
      alt.id = (alt.text) ? alt.id || 'qrcode-description' : null;

      // Compose title property surrogate
      title = (typeof title === 'string') ? {text: title} : title || {};
      title.text = title.text || null;
      title.id = (title.text) ? title.id || 'qrcode-title' : null;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var c, mc, r, mr, qrSvg='', rect;

      rect = 'l' + cellSize + ',0 0,' + cellSize +
        ' -' + cellSize + ',0 0,-' + cellSize + 'z ';

      qrSvg += '<svg version="1.1" xmlns="http://www.w3.org/2000/svg"';
      qrSvg += !opts.scalable ? ' width="' + size + 'px" height="' + size + 'px"' : '';
      qrSvg += ' viewBox="0 0 ' + size + ' ' + size + '" ';
      qrSvg += ' preserveAspectRatio="xMinYMin meet"';
      qrSvg += (title.text || alt.text) ? ' role="img" aria-labelledby="' +
          escapeXml([title.id, alt.id].join(' ').trim() ) + '"' : '';
      qrSvg += '>';
      qrSvg += (title.text) ? '<title id="' + escapeXml(title.id) + '">' +
          escapeXml(title.text) + '</title>' : '';
      qrSvg += (alt.text) ? '<description id="' + escapeXml(alt.id) + '">' +
          escapeXml(alt.text) + '</description>' : '';
      qrSvg += '<rect width="100%" height="100%" fill="white" cx="0" cy="0"/>';
      qrSvg += '<path d="';

      for (r = 0; r < _this.getModuleCount(); r += 1) {
        mr = r * cellSize + margin;
        for (c = 0; c < _this.getModuleCount(); c += 1) {
          if (_this.isDark(r, c) ) {
            mc = c*cellSize+margin;
            qrSvg += 'M' + mc + ',' + mr + rect;
          }
        }
      }

      qrSvg += '" stroke="transparent" fill="black"/>';
      qrSvg += '</svg>';

      return qrSvg;
    };

    _this.createDataURL = function(cellSize, margin) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      return createDataURL(size, size, function(x, y) {
        if (min <= x && x < max && min <= y && y < max) {
          var c = Math.floor( (x - min) / cellSize);
          var r = Math.floor( (y - min) / cellSize);
          return _this.isDark(r, c)? 0 : 1;
        } else {
          return 1;
        }
      } );
    };

    _this.createImgTag = function(cellSize, margin, alt) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;

      var img = '';
      img += '<img';
      img += '\\u0020src="';
      img += _this.createDataURL(cellSize, margin);
      img += '"';
      img += '\\u0020width="';
      img += size;
      img += '"';
      img += '\\u0020height="';
      img += size;
      img += '"';
      if (alt) {
        img += '\\u0020alt="';
        img += escapeXml(alt);
        img += '"';
      }
      img += '/>';

      return img;
    };

    var escapeXml = function(s) {
      var escaped = '';
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charAt(i);
        switch(c) {
        case '<': escaped += '&lt;'; break;
        case '>': escaped += '&gt;'; break;
        case '&': escaped += '&amp;'; break;
        case '"': escaped += '&quot;'; break;
        default : escaped += c; break;
        }
      }
      return escaped;
    };

    var _createHalfASCII = function(margin) {
      var cellSize = 1;
      margin = (typeof margin == 'undefined')? cellSize * 2 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      var y, x, r1, r2, p;

      var blocks = {
        '██': '█',
        '█ ': '▀',
        ' █': '▄',
        '  ': ' '
      };

      var blocksLastLineNoMargin = {
        '██': '▀',
        '█ ': '▀',
        ' █': ' ',
        '  ': ' '
      };

      var ascii = '';
      for (y = 0; y < size; y += 2) {
        r1 = Math.floor((y - min) / cellSize);
        r2 = Math.floor((y + 1 - min) / cellSize);
        for (x = 0; x < size; x += 1) {
          p = '█';

          if (min <= x && x < max && min <= y && y < max && _this.isDark(r1, Math.floor((x - min) / cellSize))) {
            p = ' ';
          }

          if (min <= x && x < max && min <= y+1 && y+1 < max && _this.isDark(r2, Math.floor((x - min) / cellSize))) {
            p += ' ';
          }
          else {
            p += '█';
          }

          // Output 2 characters per pixel, to create full square. 1 character per pixels gives only half width of square.
          ascii += (margin < 1 && y+1 >= max) ? blocksLastLineNoMargin[p] : blocks[p];
        }

        ascii += '\\n';
      }

      if (size % 2 && margin > 0) {
        return ascii.substring(0, ascii.length - size - 1) + Array(size+1).join('▀');
      }

      return ascii.substring(0, ascii.length-1);
    };

    _this.createASCII = function(cellSize, margin) {
      cellSize = cellSize || 1;

      if (cellSize < 2) {
        return _createHalfASCII(margin);
      }

      cellSize -= 1;
      margin = (typeof margin == 'undefined')? cellSize * 2 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      var y, x, r, p;

      var white = Array(cellSize+1).join('██');
      var black = Array(cellSize+1).join('  ');

      var ascii = '';
      var line = '';
      for (y = 0; y < size; y += 1) {
        r = Math.floor( (y - min) / cellSize);
        line = '';
        for (x = 0; x < size; x += 1) {
          p = 1;

          if (min <= x && x < max && min <= y && y < max && _this.isDark(r, Math.floor((x - min) / cellSize))) {
            p = 0;
          }

          // Output 2 characters per pixel, to create full square. 1 character per pixels gives only half width of square.
          line += p ? white : black;
        }

        for (r = 0; r < cellSize; r += 1) {
          ascii += line + '\\n';
        }
      }

      return ascii.substring(0, ascii.length-1);
    };

    _this.renderTo2dContext = function(context, cellSize) {
      cellSize = cellSize || 2;
      var length = _this.getModuleCount();
      for (var row = 0; row < length; row++) {
        for (var col = 0; col < length; col++) {
          context.fillStyle = _this.isDark(row, col) ? 'black' : 'white';
          context.fillRect(row * cellSize, col * cellSize, cellSize, cellSize);
        }
      }
    }

    return _this;
  };

  //---------------------------------------------------------------------
  // qrcode.stringToBytes
  //---------------------------------------------------------------------

  qrcode.stringToBytesFuncs = {
    'default' : function(s) {
      var bytes = [];
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charCodeAt(i);
        bytes.push(c & 0xff);
      }
      return bytes;
    }
  };

  qrcode.stringToBytes = qrcode.stringToBytesFuncs['default'];

  //---------------------------------------------------------------------
  // qrcode.createStringToBytes
  //---------------------------------------------------------------------

  /**
   * @param unicodeData base64 string of byte array.
   * [16bit Unicode],[16bit Bytes], ...
   * @param numChars
   */
  qrcode.createStringToBytes = function(unicodeData, numChars) {

    // create conversion map.

    var unicodeMap = function() {

      var bin = base64DecodeInputStream(unicodeData);
      var read = function() {
        var b = bin.read();
        if (b == -1) throw 'eof';
        return b;
      };

      var count = 0;
      var unicodeMap = {};
      while (true) {
        var b0 = bin.read();
        if (b0 == -1) break;
        var b1 = read();
        var b2 = read();
        var b3 = read();
        var k = String.fromCharCode( (b0 << 8) | b1);
        var v = (b2 << 8) | b3;
        unicodeMap[k] = v;
        count += 1;
      }
      if (count != numChars) {
        throw count + ' != ' + numChars;
      }

      return unicodeMap;
    }();

    var unknownChar = '?'.charCodeAt(0);

    return function(s) {
      var bytes = [];
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charCodeAt(i);
        if (c < 128) {
          bytes.push(c);
        } else {
          var b = unicodeMap[s.charAt(i)];
          if (typeof b == 'number') {
            if ( (b & 0xff) == b) {
              // 1byte
              bytes.push(b);
            } else {
              // 2bytes
              bytes.push(b >>> 8);
              bytes.push(b & 0xff);
            }
          } else {
            bytes.push(unknownChar);
          }
        }
      }
      return bytes;
    };
  };

  //---------------------------------------------------------------------
  // QRMode
  //---------------------------------------------------------------------

  var QRMode = {
    MODE_NUMBER :    1 << 0,
    MODE_ALPHA_NUM : 1 << 1,
    MODE_8BIT_BYTE : 1 << 2,
    MODE_KANJI :     1 << 3
  };

  //---------------------------------------------------------------------
  // QRErrorCorrectionLevel
  //---------------------------------------------------------------------

  var QRErrorCorrectionLevel = {
    L : 1,
    M : 0,
    Q : 3,
    H : 2
  };

  //---------------------------------------------------------------------
  // QRMaskPattern
  //---------------------------------------------------------------------

  var QRMaskPattern = {
    PATTERN000 : 0,
    PATTERN001 : 1,
    PATTERN010 : 2,
    PATTERN011 : 3,
    PATTERN100 : 4,
    PATTERN101 : 5,
    PATTERN110 : 6,
    PATTERN111 : 7
  };

  //---------------------------------------------------------------------
  // QRUtil
  //---------------------------------------------------------------------

  var QRUtil = function() {

    var PATTERN_POSITION_TABLE = [
      [],
      [6, 18],
      [6, 22],
      [6, 26],
      [6, 30],
      [6, 34],
      [6, 22, 38],
      [6, 24, 42],
      [6, 26, 46],
      [6, 28, 50],
      [6, 30, 54],
      [6, 32, 58],
      [6, 34, 62],
      [6, 26, 46, 66],
      [6, 26, 48, 70],
      [6, 26, 50, 74],
      [6, 30, 54, 78],
      [6, 30, 56, 82],
      [6, 30, 58, 86],
      [6, 34, 62, 90],
      [6, 28, 50, 72, 94],
      [6, 26, 50, 74, 98],
      [6, 30, 54, 78, 102],
      [6, 28, 54, 80, 106],
      [6, 32, 58, 84, 110],
      [6, 30, 58, 86, 114],
      [6, 34, 62, 90, 118],
      [6, 26, 50, 74, 98, 122],
      [6, 30, 54, 78, 102, 126],
      [6, 26, 52, 78, 104, 130],
      [6, 30, 56, 82, 108, 134],
      [6, 34, 60, 86, 112, 138],
      [6, 30, 58, 86, 114, 142],
      [6, 34, 62, 90, 118, 146],
      [6, 30, 54, 78, 102, 126, 150],
      [6, 24, 50, 76, 102, 128, 154],
      [6, 28, 54, 80, 106, 132, 158],
      [6, 32, 58, 84, 110, 136, 162],
      [6, 26, 54, 82, 110, 138, 166],
      [6, 30, 58, 86, 114, 142, 170]
    ];
    var G15 = (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | (1 << 0);
    var G18 = (1 << 12) | (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8) | (1 << 5) | (1 << 2) | (1 << 0);
    var G15_MASK = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1);

    var _this = {};

    var getBCHDigit = function(data) {
      var digit = 0;
      while (data != 0) {
        digit += 1;
        data >>>= 1;
      }
      return digit;
    };

    _this.getBCHTypeInfo = function(data) {
      var d = data << 10;
      while (getBCHDigit(d) - getBCHDigit(G15) >= 0) {
        d ^= (G15 << (getBCHDigit(d) - getBCHDigit(G15) ) );
      }
      return ( (data << 10) | d) ^ G15_MASK;
    };

    _this.getBCHTypeNumber = function(data) {
      var d = data << 12;
      while (getBCHDigit(d) - getBCHDigit(G18) >= 0) {
        d ^= (G18 << (getBCHDigit(d) - getBCHDigit(G18) ) );
      }
      return (data << 12) | d;
    };

    _this.getPatternPosition = function(typeNumber) {
      return PATTERN_POSITION_TABLE[typeNumber - 1];
    };

    _this.getMaskFunction = function(maskPattern) {

      switch (maskPattern) {

      case QRMaskPattern.PATTERN000 :
        return function(i, j) { return (i + j) % 2 == 0; };
      case QRMaskPattern.PATTERN001 :
        return function(i, j) { return i % 2 == 0; };
      case QRMaskPattern.PATTERN010 :
        return function(i, j) { return j % 3 == 0; };
      case QRMaskPattern.PATTERN011 :
        return function(i, j) { return (i + j) % 3 == 0; };
      case QRMaskPattern.PATTERN100 :
        return function(i, j) { return (Math.floor(i / 2) + Math.floor(j / 3) ) % 2 == 0; };
      case QRMaskPattern.PATTERN101 :
        return function(i, j) { return (i * j) % 2 + (i * j) % 3 == 0; };
      case QRMaskPattern.PATTERN110 :
        return function(i, j) { return ( (i * j) % 2 + (i * j) % 3) % 2 == 0; };
      case QRMaskPattern.PATTERN111 :
        return function(i, j) { return ( (i * j) % 3 + (i + j) % 2) % 2 == 0; };

      default :
        throw 'bad maskPattern:' + maskPattern;
      }
    };

    _this.getErrorCorrectPolynomial = function(errorCorrectLength) {
      var a = qrPolynomial([1], 0);
      for (var i = 0; i < errorCorrectLength; i += 1) {
        a = a.multiply(qrPolynomial([1, QRMath.gexp(i)], 0) );
      }
      return a;
    };

    _this.getLengthInBits = function(mode, type) {

      if (1 <= type && type < 10) {

        // 1 - 9

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 10;
        case QRMode.MODE_ALPHA_NUM : return 9;
        case QRMode.MODE_8BIT_BYTE : return 8;
        case QRMode.MODE_KANJI     : return 8;
        default :
          throw 'mode:' + mode;
        }

      } else if (type < 27) {

        // 10 - 26

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 12;
        case QRMode.MODE_ALPHA_NUM : return 11;
        case QRMode.MODE_8BIT_BYTE : return 16;
        case QRMode.MODE_KANJI     : return 10;
        default :
          throw 'mode:' + mode;
        }

      } else if (type < 41) {

        // 27 - 40

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 14;
        case QRMode.MODE_ALPHA_NUM : return 13;
        case QRMode.MODE_8BIT_BYTE : return 16;
        case QRMode.MODE_KANJI     : return 12;
        default :
          throw 'mode:' + mode;
        }

      } else {
        throw 'type:' + type;
      }
    };

    _this.getLostPoint = function(qrcode) {

      var moduleCount = qrcode.getModuleCount();

      var lostPoint = 0;

      // LEVEL1

      for (var row = 0; row < moduleCount; row += 1) {
        for (var col = 0; col < moduleCount; col += 1) {

          var sameCount = 0;
          var dark = qrcode.isDark(row, col);

          for (var r = -1; r <= 1; r += 1) {

            if (row + r < 0 || moduleCount <= row + r) {
              continue;
            }

            for (var c = -1; c <= 1; c += 1) {

              if (col + c < 0 || moduleCount <= col + c) {
                continue;
              }

              if (r == 0 && c == 0) {
                continue;
              }

              if (dark == qrcode.isDark(row + r, col + c) ) {
                sameCount += 1;
              }
            }
          }

          if (sameCount > 5) {
            lostPoint += (3 + sameCount - 5);
          }
        }
      };

      // LEVEL2

      for (var row = 0; row < moduleCount - 1; row += 1) {
        for (var col = 0; col < moduleCount - 1; col += 1) {
          var count = 0;
          if (qrcode.isDark(row, col) ) count += 1;
          if (qrcode.isDark(row + 1, col) ) count += 1;
          if (qrcode.isDark(row, col + 1) ) count += 1;
          if (qrcode.isDark(row + 1, col + 1) ) count += 1;
          if (count == 0 || count == 4) {
            lostPoint += 3;
          }
        }
      }

      // LEVEL3

      for (var row = 0; row < moduleCount; row += 1) {
        for (var col = 0; col < moduleCount - 6; col += 1) {
          if (qrcode.isDark(row, col)
              && !qrcode.isDark(row, col + 1)
              &&  qrcode.isDark(row, col + 2)
              &&  qrcode.isDark(row, col + 3)
              &&  qrcode.isDark(row, col + 4)
              && !qrcode.isDark(row, col + 5)
              &&  qrcode.isDark(row, col + 6) ) {
            lostPoint += 40;
          }
        }
      }

      for (var col = 0; col < moduleCount; col += 1) {
        for (var row = 0; row < moduleCount - 6; row += 1) {
          if (qrcode.isDark(row, col)
              && !qrcode.isDark(row + 1, col)
              &&  qrcode.isDark(row + 2, col)
              &&  qrcode.isDark(row + 3, col)
              &&  qrcode.isDark(row + 4, col)
              && !qrcode.isDark(row + 5, col)
              &&  qrcode.isDark(row + 6, col) ) {
            lostPoint += 40;
          }
        }
      }

      // LEVEL4

      var darkCount = 0;

      for (var col = 0; col < moduleCount; col += 1) {
        for (var row = 0; row < moduleCount; row += 1) {
          if (qrcode.isDark(row, col) ) {
            darkCount += 1;
          }
        }
      }

      var ratio = Math.abs(100 * darkCount / moduleCount / moduleCount - 50) / 5;
      lostPoint += ratio * 10;

      return lostPoint;
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // QRMath
  //---------------------------------------------------------------------

  var QRMath = function() {

    var EXP_TABLE = new Array(256);
    var LOG_TABLE = new Array(256);

    // initialize tables
    for (var i = 0; i < 8; i += 1) {
      EXP_TABLE[i] = 1 << i;
    }
    for (var i = 8; i < 256; i += 1) {
      EXP_TABLE[i] = EXP_TABLE[i - 4]
        ^ EXP_TABLE[i - 5]
        ^ EXP_TABLE[i - 6]
        ^ EXP_TABLE[i - 8];
    }
    for (var i = 0; i < 255; i += 1) {
      LOG_TABLE[EXP_TABLE[i] ] = i;
    }

    var _this = {};

    _this.glog = function(n) {

      if (n < 1) {
        throw 'glog(' + n + ')';
      }

      return LOG_TABLE[n];
    };

    _this.gexp = function(n) {

      while (n < 0) {
        n += 255;
      }

      while (n >= 256) {
        n -= 255;
      }

      return EXP_TABLE[n];
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // qrPolynomial
  //---------------------------------------------------------------------

  function qrPolynomial(num, shift) {

    if (typeof num.length == 'undefined') {
      throw num.length + '/' + shift;
    }

    var _num = function() {
      var offset = 0;
      while (offset < num.length && num[offset] == 0) {
        offset += 1;
      }
      var _num = new Array(num.length - offset + shift);
      for (var i = 0; i < num.length - offset; i += 1) {
        _num[i] = num[i + offset];
      }
      return _num;
    }();

    var _this = {};

    _this.getAt = function(index) {
      return _num[index];
    };

    _this.getLength = function() {
      return _num.length;
    };

    _this.multiply = function(e) {

      var num = new Array(_this.getLength() + e.getLength() - 1);

      for (var i = 0; i < _this.getLength(); i += 1) {
        for (var j = 0; j < e.getLength(); j += 1) {
          num[i + j] ^= QRMath.gexp(QRMath.glog(_this.getAt(i) ) + QRMath.glog(e.getAt(j) ) );
        }
      }

      return qrPolynomial(num, 0);
    };

    _this.mod = function(e) {

      if (_this.getLength() - e.getLength() < 0) {
        return _this;
      }

      var ratio = QRMath.glog(_this.getAt(0) ) - QRMath.glog(e.getAt(0) );

      var num = new Array(_this.getLength() );
      for (var i = 0; i < _this.getLength(); i += 1) {
        num[i] = _this.getAt(i);
      }

      for (var i = 0; i < e.getLength(); i += 1) {
        num[i] ^= QRMath.gexp(QRMath.glog(e.getAt(i) ) + ratio);
      }

      // recursive call
      return qrPolynomial(num, 0).mod(e);
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // QRRSBlock
  //---------------------------------------------------------------------

  var QRRSBlock = function() {

    var RS_BLOCK_TABLE = [

      // L
      // M
      // Q
      // H

      // 1
      [1, 26, 19],
      [1, 26, 16],
      [1, 26, 13],
      [1, 26, 9],

      // 2
      [1, 44, 34],
      [1, 44, 28],
      [1, 44, 22],
      [1, 44, 16],

      // 3
      [1, 70, 55],
      [1, 70, 44],
      [2, 35, 17],
      [2, 35, 13],

      // 4
      [1, 100, 80],
      [2, 50, 32],
      [2, 50, 24],
      [4, 25, 9],

      // 5
      [1, 134, 108],
      [2, 67, 43],
      [2, 33, 15, 2, 34, 16],
      [2, 33, 11, 2, 34, 12],

      // 6
      [2, 86, 68],
      [4, 43, 27],
      [4, 43, 19],
      [4, 43, 15],

      // 7
      [2, 98, 78],
      [4, 49, 31],
      [2, 32, 14, 4, 33, 15],
      [4, 39, 13, 1, 40, 14],

      // 8
      [2, 121, 97],
      [2, 60, 38, 2, 61, 39],
      [4, 40, 18, 2, 41, 19],
      [4, 40, 14, 2, 41, 15],

      // 9
      [2, 146, 116],
      [3, 58, 36, 2, 59, 37],
      [4, 36, 16, 4, 37, 17],
      [4, 36, 12, 4, 37, 13],

      // 10
      [2, 86, 68, 2, 87, 69],
      [4, 69, 43, 1, 70, 44],
      [6, 43, 19, 2, 44, 20],
      [6, 43, 15, 2, 44, 16],

      // 11
      [4, 101, 81],
      [1, 80, 50, 4, 81, 51],
      [4, 50, 22, 4, 51, 23],
      [3, 36, 12, 8, 37, 13],

      // 12
      [2, 116, 92, 2, 117, 93],
      [6, 58, 36, 2, 59, 37],
      [4, 46, 20, 6, 47, 21],
      [7, 42, 14, 4, 43, 15],

      // 13
      [4, 133, 107],
      [8, 59, 37, 1, 60, 38],
      [8, 44, 20, 4, 45, 21],
      [12, 33, 11, 4, 34, 12],

      // 14
      [3, 145, 115, 1, 146, 116],
      [4, 64, 40, 5, 65, 41],
      [11, 36, 16, 5, 37, 17],
      [11, 36, 12, 5, 37, 13],

      // 15
      [5, 109, 87, 1, 110, 88],
      [5, 65, 41, 5, 66, 42],
      [5, 54, 24, 7, 55, 25],
      [11, 36, 12, 7, 37, 13],

      // 16
      [5, 122, 98, 1, 123, 99],
      [7, 73, 45, 3, 74, 46],
      [15, 43, 19, 2, 44, 20],
      [3, 45, 15, 13, 46, 16],

      // 17
      [1, 135, 107, 5, 136, 108],
      [10, 74, 46, 1, 75, 47],
      [1, 50, 22, 15, 51, 23],
      [2, 42, 14, 17, 43, 15],

      // 18
      [5, 150, 120, 1, 151, 121],
      [9, 69, 43, 4, 70, 44],
      [17, 50, 22, 1, 51, 23],
      [2, 42, 14, 19, 43, 15],

      // 19
      [3, 141, 113, 4, 142, 114],
      [3, 70, 44, 11, 71, 45],
      [17, 47, 21, 4, 48, 22],
      [9, 39, 13, 16, 40, 14],

      // 20
      [3, 135, 107, 5, 136, 108],
      [3, 67, 41, 13, 68, 42],
      [15, 54, 24, 5, 55, 25],
      [15, 43, 15, 10, 44, 16],

      // 21
      [4, 144, 116, 4, 145, 117],
      [17, 68, 42],
      [17, 50, 22, 6, 51, 23],
      [19, 46, 16, 6, 47, 17],

      // 22
      [2, 139, 111, 7, 140, 112],
      [17, 74, 46],
      [7, 54, 24, 16, 55, 25],
      [34, 37, 13],

      // 23
      [4, 151, 121, 5, 152, 122],
      [4, 75, 47, 14, 76, 48],
      [11, 54, 24, 14, 55, 25],
      [16, 45, 15, 14, 46, 16],

      // 24
      [6, 147, 117, 4, 148, 118],
      [6, 73, 45, 14, 74, 46],
      [11, 54, 24, 16, 55, 25],
      [30, 46, 16, 2, 47, 17],

      // 25
      [8, 132, 106, 4, 133, 107],
      [8, 75, 47, 13, 76, 48],
      [7, 54, 24, 22, 55, 25],
      [22, 45, 15, 13, 46, 16],

      // 26
      [10, 142, 114, 2, 143, 115],
      [19, 74, 46, 4, 75, 47],
      [28, 50, 22, 6, 51, 23],
      [33, 46, 16, 4, 47, 17],

      // 27
      [8, 152, 122, 4, 153, 123],
      [22, 73, 45, 3, 74, 46],
      [8, 53, 23, 26, 54, 24],
      [12, 45, 15, 28, 46, 16],

      // 28
      [3, 147, 117, 10, 148, 118],
      [3, 73, 45, 23, 74, 46],
      [4, 54, 24, 31, 55, 25],
      [11, 45, 15, 31, 46, 16],

      // 29
      [7, 146, 116, 7, 147, 117],
      [21, 73, 45, 7, 74, 46],
      [1, 53, 23, 37, 54, 24],
      [19, 45, 15, 26, 46, 16],

      // 30
      [5, 145, 115, 10, 146, 116],
      [19, 75, 47, 10, 76, 48],
      [15, 54, 24, 25, 55, 25],
      [23, 45, 15, 25, 46, 16],

      // 31
      [13, 145, 115, 3, 146, 116],
      [2, 74, 46, 29, 75, 47],
      [42, 54, 24, 1, 55, 25],
      [23, 45, 15, 28, 46, 16],

      // 32
      [17, 145, 115],
      [10, 74, 46, 23, 75, 47],
      [10, 54, 24, 35, 55, 25],
      [19, 45, 15, 35, 46, 16],

      // 33
      [17, 145, 115, 1, 146, 116],
      [14, 74, 46, 21, 75, 47],
      [29, 54, 24, 19, 55, 25],
      [11, 45, 15, 46, 46, 16],

      // 34
      [13, 145, 115, 6, 146, 116],
      [14, 74, 46, 23, 75, 47],
      [44, 54, 24, 7, 55, 25],
      [59, 46, 16, 1, 47, 17],

      // 35
      [12, 151, 121, 7, 152, 122],
      [12, 75, 47, 26, 76, 48],
      [39, 54, 24, 14, 55, 25],
      [22, 45, 15, 41, 46, 16],

      // 36
      [6, 151, 121, 14, 152, 122],
      [6, 75, 47, 34, 76, 48],
      [46, 54, 24, 10, 55, 25],
      [2, 45, 15, 64, 46, 16],

      // 37
      [17, 152, 122, 4, 153, 123],
      [29, 74, 46, 14, 75, 47],
      [49, 54, 24, 10, 55, 25],
      [24, 45, 15, 46, 46, 16],

      // 38
      [4, 152, 122, 18, 153, 123],
      [13, 74, 46, 32, 75, 47],
      [48, 54, 24, 14, 55, 25],
      [42, 45, 15, 32, 46, 16],

      // 39
      [20, 147, 117, 4, 148, 118],
      [40, 75, 47, 7, 76, 48],
      [43, 54, 24, 22, 55, 25],
      [10, 45, 15, 67, 46, 16],

      // 40
      [19, 148, 118, 6, 149, 119],
      [18, 75, 47, 31, 76, 48],
      [34, 54, 24, 34, 55, 25],
      [20, 45, 15, 61, 46, 16]
    ];

    var qrRSBlock = function(totalCount, dataCount) {
      var _this = {};
      _this.totalCount = totalCount;
      _this.dataCount = dataCount;
      return _this;
    };

    var _this = {};

    var getRsBlockTable = function(typeNumber, errorCorrectionLevel) {

      switch(errorCorrectionLevel) {
      case QRErrorCorrectionLevel.L :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 0];
      case QRErrorCorrectionLevel.M :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 1];
      case QRErrorCorrectionLevel.Q :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 2];
      case QRErrorCorrectionLevel.H :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 3];
      default :
        return undefined;
      }
    };

    _this.getRSBlocks = function(typeNumber, errorCorrectionLevel) {

      var rsBlock = getRsBlockTable(typeNumber, errorCorrectionLevel);

      if (typeof rsBlock == 'undefined') {
        throw 'bad rs block @ typeNumber:' + typeNumber +
            '/errorCorrectionLevel:' + errorCorrectionLevel;
      }

      var length = rsBlock.length / 3;

      var list = [];

      for (var i = 0; i < length; i += 1) {

        var count = rsBlock[i * 3 + 0];
        var totalCount = rsBlock[i * 3 + 1];
        var dataCount = rsBlock[i * 3 + 2];

        for (var j = 0; j < count; j += 1) {
          list.push(qrRSBlock(totalCount, dataCount) );
        }
      }

      return list;
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // qrBitBuffer
  //---------------------------------------------------------------------

  var qrBitBuffer = function() {

    var _buffer = [];
    var _length = 0;

    var _this = {};

    _this.getBuffer = function() {
      return _buffer;
    };

    _this.getAt = function(index) {
      var bufIndex = Math.floor(index / 8);
      return ( (_buffer[bufIndex] >>> (7 - index % 8) ) & 1) == 1;
    };

    _this.put = function(num, length) {
      for (var i = 0; i < length; i += 1) {
        _this.putBit( ( (num >>> (length - i - 1) ) & 1) == 1);
      }
    };

    _this.getLengthInBits = function() {
      return _length;
    };

    _this.putBit = function(bit) {

      var bufIndex = Math.floor(_length / 8);
      if (_buffer.length <= bufIndex) {
        _buffer.push(0);
      }

      if (bit) {
        _buffer[bufIndex] |= (0x80 >>> (_length % 8) );
      }

      _length += 1;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrNumber
  //---------------------------------------------------------------------

  var qrNumber = function(data) {

    var _mode = QRMode.MODE_NUMBER;
    var _data = data;

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _data.length;
    };

    _this.write = function(buffer) {

      var data = _data;

      var i = 0;

      while (i + 2 < data.length) {
        buffer.put(strToNum(data.substring(i, i + 3) ), 10);
        i += 3;
      }

      if (i < data.length) {
        if (data.length - i == 1) {
          buffer.put(strToNum(data.substring(i, i + 1) ), 4);
        } else if (data.length - i == 2) {
          buffer.put(strToNum(data.substring(i, i + 2) ), 7);
        }
      }
    };

    var strToNum = function(s) {
      var num = 0;
      for (var i = 0; i < s.length; i += 1) {
        num = num * 10 + chatToNum(s.charAt(i) );
      }
      return num;
    };

    var chatToNum = function(c) {
      if ('0' <= c && c <= '9') {
        return c.charCodeAt(0) - '0'.charCodeAt(0);
      }
      throw 'illegal char :' + c;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrAlphaNum
  //---------------------------------------------------------------------

  var qrAlphaNum = function(data) {

    var _mode = QRMode.MODE_ALPHA_NUM;
    var _data = data;

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _data.length;
    };

    _this.write = function(buffer) {

      var s = _data;

      var i = 0;

      while (i + 1 < s.length) {
        buffer.put(
          getCode(s.charAt(i) ) * 45 +
          getCode(s.charAt(i + 1) ), 11);
        i += 2;
      }

      if (i < s.length) {
        buffer.put(getCode(s.charAt(i) ), 6);
      }
    };

    var getCode = function(c) {

      if ('0' <= c && c <= '9') {
        return c.charCodeAt(0) - '0'.charCodeAt(0);
      } else if ('A' <= c && c <= 'Z') {
        return c.charCodeAt(0) - 'A'.charCodeAt(0) + 10;
      } else {
        switch (c) {
        case ' ' : return 36;
        case '$' : return 37;
        case '%' : return 38;
        case '*' : return 39;
        case '+' : return 40;
        case '-' : return 41;
        case '.' : return 42;
        case '/' : return 43;
        case ':' : return 44;
        default :
          throw 'illegal char :' + c;
        }
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qr8BitByte
  //---------------------------------------------------------------------

  var qr8BitByte = function(data) {

    var _mode = QRMode.MODE_8BIT_BYTE;
    var _data = data;
    var _bytes = qrcode.stringToBytes(data);

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _bytes.length;
    };

    _this.write = function(buffer) {
      for (var i = 0; i < _bytes.length; i += 1) {
        buffer.put(_bytes[i], 8);
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrKanji
  //---------------------------------------------------------------------

  var qrKanji = function(data) {

    var _mode = QRMode.MODE_KANJI;
    var _data = data;

    var stringToBytes = qrcode.stringToBytesFuncs['SJIS'];
    if (!stringToBytes) {
      throw 'sjis not supported.';
    }
    !function(c, code) {
      // self test for sjis support.
      var test = stringToBytes(c);
      if (test.length != 2 || ( (test[0] << 8) | test[1]) != code) {
        throw 'sjis not supported.';
      }
    }('\\u53cb', 0x9746);

    var _bytes = stringToBytes(data);

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return ~~(_bytes.length / 2);
    };

    _this.write = function(buffer) {

      var data = _bytes;

      var i = 0;

      while (i + 1 < data.length) {

        var c = ( (0xff & data[i]) << 8) | (0xff & data[i + 1]);

        if (0x8140 <= c && c <= 0x9FFC) {
          c -= 0x8140;
        } else if (0xE040 <= c && c <= 0xEBBF) {
          c -= 0xC140;
        } else {
          throw 'illegal char at ' + (i + 1) + '/' + c;
        }

        c = ( (c >>> 8) & 0xff) * 0xC0 + (c & 0xff);

        buffer.put(c, 13);

        i += 2;
      }

      if (i < data.length) {
        throw 'illegal char at ' + (i + 1);
      }
    };

    return _this;
  };

  //=====================================================================
  // GIF Support etc.
  //

  //---------------------------------------------------------------------
  // byteArrayOutputStream
  //---------------------------------------------------------------------

  var byteArrayOutputStream = function() {

    var _bytes = [];

    var _this = {};

    _this.writeByte = function(b) {
      _bytes.push(b & 0xff);
    };

    _this.writeShort = function(i) {
      _this.writeByte(i);
      _this.writeByte(i >>> 8);
    };

    _this.writeBytes = function(b, off, len) {
      off = off || 0;
      len = len || b.length;
      for (var i = 0; i < len; i += 1) {
        _this.writeByte(b[i + off]);
      }
    };

    _this.writeString = function(s) {
      for (var i = 0; i < s.length; i += 1) {
        _this.writeByte(s.charCodeAt(i) );
      }
    };

    _this.toByteArray = function() {
      return _bytes;
    };

    _this.toString = function() {
      var s = '';
      s += '[';
      for (var i = 0; i < _bytes.length; i += 1) {
        if (i > 0) {
          s += ',';
        }
        s += _bytes[i];
      }
      s += ']';
      return s;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // base64EncodeOutputStream
  //---------------------------------------------------------------------

  var base64EncodeOutputStream = function() {

    var _buffer = 0;
    var _buflen = 0;
    var _length = 0;
    var _base64 = '';

    var _this = {};

    var writeEncoded = function(b) {
      _base64 += String.fromCharCode(encode(b & 0x3f) );
    };

    var encode = function(n) {
      if (n < 0) {
        // error.
      } else if (n < 26) {
        return 0x41 + n;
      } else if (n < 52) {
        return 0x61 + (n - 26);
      } else if (n < 62) {
        return 0x30 + (n - 52);
      } else if (n == 62) {
        return 0x2b;
      } else if (n == 63) {
        return 0x2f;
      }
      throw 'n:' + n;
    };

    _this.writeByte = function(n) {

      _buffer = (_buffer << 8) | (n & 0xff);
      _buflen += 8;
      _length += 1;

      while (_buflen >= 6) {
        writeEncoded(_buffer >>> (_buflen - 6) );
        _buflen -= 6;
      }
    };

    _this.flush = function() {

      if (_buflen > 0) {
        writeEncoded(_buffer << (6 - _buflen) );
        _buffer = 0;
        _buflen = 0;
      }

      if (_length % 3 != 0) {
        // padding
        var padlen = 3 - _length % 3;
        for (var i = 0; i < padlen; i += 1) {
          _base64 += '=';
        }
      }
    };

    _this.toString = function() {
      return _base64;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // base64DecodeInputStream
  //---------------------------------------------------------------------

  var base64DecodeInputStream = function(str) {

    var _str = str;
    var _pos = 0;
    var _buffer = 0;
    var _buflen = 0;

    var _this = {};

    _this.read = function() {

      while (_buflen < 8) {

        if (_pos >= _str.length) {
          if (_buflen == 0) {
            return -1;
          }
          throw 'unexpected end of file./' + _buflen;
        }

        var c = _str.charAt(_pos);
        _pos += 1;

        if (c == '=') {
          _buflen = 0;
          return -1;
        } else if (c.match(/^\\s$/) ) {
          // ignore if whitespace.
          continue;
        }

        _buffer = (_buffer << 6) | decode(c.charCodeAt(0) );
        _buflen += 6;
      }

      var n = (_buffer >>> (_buflen - 8) ) & 0xff;
      _buflen -= 8;
      return n;
    };

    var decode = function(c) {
      if (0x41 <= c && c <= 0x5a) {
        return c - 0x41;
      } else if (0x61 <= c && c <= 0x7a) {
        return c - 0x61 + 26;
      } else if (0x30 <= c && c <= 0x39) {
        return c - 0x30 + 52;
      } else if (c == 0x2b) {
        return 62;
      } else if (c == 0x2f) {
        return 63;
      } else {
        throw 'c:' + c;
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // gifImage (B/W)
  //---------------------------------------------------------------------

  var gifImage = function(width, height) {

    var _width = width;
    var _height = height;
    var _data = new Array(width * height);

    var _this = {};

    _this.setPixel = function(x, y, pixel) {
      _data[y * _width + x] = pixel;
    };

    _this.write = function(out) {

      //---------------------------------
      // GIF Signature

      out.writeString('GIF87a');

      //---------------------------------
      // Screen Descriptor

      out.writeShort(_width);
      out.writeShort(_height);

      out.writeByte(0x80); // 2bit
      out.writeByte(0);
      out.writeByte(0);

      //---------------------------------
      // Global Color Map

      // black
      out.writeByte(0x00);
      out.writeByte(0x00);
      out.writeByte(0x00);

      // white
      out.writeByte(0xff);
      out.writeByte(0xff);
      out.writeByte(0xff);

      //---------------------------------
      // Image Descriptor

      out.writeString(',');
      out.writeShort(0);
      out.writeShort(0);
      out.writeShort(_width);
      out.writeShort(_height);
      out.writeByte(0);

      //---------------------------------
      // Local Color Map

      //---------------------------------
      // Raster Data

      var lzwMinCodeSize = 2;
      var raster = getLZWRaster(lzwMinCodeSize);

      out.writeByte(lzwMinCodeSize);

      var offset = 0;

      while (raster.length - offset > 255) {
        out.writeByte(255);
        out.writeBytes(raster, offset, 255);
        offset += 255;
      }

      out.writeByte(raster.length - offset);
      out.writeBytes(raster, offset, raster.length - offset);
      out.writeByte(0x00);

      //---------------------------------
      // GIF Terminator
      out.writeString(';');
    };

    var bitOutputStream = function(out) {

      var _out = out;
      var _bitLength = 0;
      var _bitBuffer = 0;

      var _this = {};

      _this.write = function(data, length) {

        if ( (data >>> length) != 0) {
          throw 'length over';
        }

        while (_bitLength + length >= 8) {
          _out.writeByte(0xff & ( (data << _bitLength) | _bitBuffer) );
          length -= (8 - _bitLength);
          data >>>= (8 - _bitLength);
          _bitBuffer = 0;
          _bitLength = 0;
        }

        _bitBuffer = (data << _bitLength) | _bitBuffer;
        _bitLength = _bitLength + length;
      };

      _this.flush = function() {
        if (_bitLength > 0) {
          _out.writeByte(_bitBuffer);
        }
      };

      return _this;
    };

    var getLZWRaster = function(lzwMinCodeSize) {

      var clearCode = 1 << lzwMinCodeSize;
      var endCode = (1 << lzwMinCodeSize) + 1;
      var bitLength = lzwMinCodeSize + 1;

      // Setup LZWTable
      var table = lzwTable();

      for (var i = 0; i < clearCode; i += 1) {
        table.add(String.fromCharCode(i) );
      }
      table.add(String.fromCharCode(clearCode) );
      table.add(String.fromCharCode(endCode) );

      var byteOut = byteArrayOutputStream();
      var bitOut = bitOutputStream(byteOut);

      // clear code
      bitOut.write(clearCode, bitLength);

      var dataIndex = 0;

      var s = String.fromCharCode(_data[dataIndex]);
      dataIndex += 1;

      while (dataIndex < _data.length) {

        var c = String.fromCharCode(_data[dataIndex]);
        dataIndex += 1;

        if (table.contains(s + c) ) {

          s = s + c;

        } else {

          bitOut.write(table.indexOf(s), bitLength);

          if (table.size() < 0xfff) {

            if (table.size() == (1 << bitLength) ) {
              bitLength += 1;
            }

            table.add(s + c);
          }

          s = c;
        }
      }

      bitOut.write(table.indexOf(s), bitLength);

      // end code
      bitOut.write(endCode, bitLength);

      bitOut.flush();

      return byteOut.toByteArray();
    };

    var lzwTable = function() {

      var _map = {};
      var _size = 0;

      var _this = {};

      _this.add = function(key) {
        if (_this.contains(key) ) {
          throw 'dup key:' + key;
        }
        _map[key] = _size;
        _size += 1;
      };

      _this.size = function() {
        return _size;
      };

      _this.indexOf = function(key) {
        return _map[key];
      };

      _this.contains = function(key) {
        return typeof _map[key] != 'undefined';
      };

      return _this;
    };

    return _this;
  };

  var createDataURL = function(width, height, getPixel) {
    var gif = gifImage(width, height);
    for (var y = 0; y < height; y += 1) {
      for (var x = 0; x < width; x += 1) {
        gif.setPixel(x, y, getPixel(x, y) );
      }
    }

    var b = byteArrayOutputStream();
    gif.write(b);

    var base64 = base64EncodeOutputStream();
    var bytes = b.toByteArray();
    for (var i = 0; i < bytes.length; i += 1) {
      base64.writeByte(bytes[i]);
    }
    base64.flush();

    return 'data:image/gif;base64,' + base64;
  };

  //---------------------------------------------------------------------
  // returns qrcode function.

  return qrcode;
}();

// multibyte support
!function() {

  qrcode.stringToBytesFuncs['UTF-8'] = function(s) {
    // http://stackoverflow.com/questions/18729405/how-to-convert-utf8-string-to-byte-array
    function toUTF8Array(str) {
      var utf8 = [];
      for (var i=0; i < str.length; i++) {
        var charcode = str.charCodeAt(i);
        if (charcode < 0x80) utf8.push(charcode);
        else if (charcode < 0x800) {
          utf8.push(0xc0 | (charcode >> 6),
              0x80 | (charcode & 0x3f));
        }
        else if (charcode < 0xd800 || charcode >= 0xe000) {
          utf8.push(0xe0 | (charcode >> 12),
              0x80 | ((charcode>>6) & 0x3f),
              0x80 | (charcode & 0x3f));
        }
        // surrogate pair
        else {
          i++;
          // UTF-16 encodes 0x10000-0x10FFFF by
          // subtracting 0x10000 and splitting the
          // 20 bits of 0x0-0xFFFFF into two halves
          charcode = 0x10000 + (((charcode & 0x3ff)<<10)
            | (str.charCodeAt(i) & 0x3ff));
          utf8.push(0xf0 | (charcode >>18),
              0x80 | ((charcode>>12) & 0x3f),
              0x80 | ((charcode>>6) & 0x3f),
              0x80 | (charcode & 0x3f));
        }
      }
      return utf8;
    }
    return toUTF8Array(s);
  };

}();

(function (factory) {
  if (typeof define === 'function' && define.amd) {
      define([], factory);
  } else if (typeof exports === 'object') {
      module.exports = factory();
  }
}(function () {
    return qrcode;
}));

;(function(){ if (typeof qrcode === 'undefined' && typeof window !== 'undefined') { window.qrcode = undefined; } })();
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
var KVIC={
 run:'<svg viewBox="0 0 24 24"><path d="M2 12h4l3-7 4 14 3-7h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
 ver:'<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
 ap:'<svg viewBox="0 0 24 24"><path d="M6 18h11a4 4 0 0 0 .7-8 6 6 0 0 0-11.6 1.4A3.6 3.6 0 0 0 6 18z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
 nd:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="7" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><rect x="3" y="13" width="18" height="7" rx="2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M7 7.5h.01M7 16.5h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
 ip:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M3 12h18M12 3c2.6 3 2.6 15 0 18-2.6-3-2.6-15 0-18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
 pa:'<svg viewBox="0 0 24 24"><path d="M9 3v6M15 3v6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M6 9h12v3a6 6 0 0 1-12 0z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M12 18v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
 ui:'<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
};
function hcRender(d){
  var h=(d.items||[]).map(function(i){
    return '<tr><td style="width:32px">'+(i.ok?'<span class=pill>通过</span>':'<span class="pill bad">异常</span>')+
           '</td><td style="width:150px">'+esc(i.name)+'</td><td>'+esc(i.detail)+'</td></tr>'}).join('');
  var b=document.getElementById('hc');
  if(b){ b.innerHTML=h+'<tr><td colspan=3><b>共 '+esc(d.passed)+'/'+esc(d.total)+' 项通过</b>（'+esc(d.time)+'）</td></tr>'; }
  var c=document.getElementById('hccount');
  if(c){ c.textContent='（'+esc(d.total)+' 项）'; }
}
function hcLoad(force){
  var btn=document.getElementById('hcbtn');
  if(force && btn){ btn.textContent='自检中…'; btn.disabled=true; }
  var back=function(){ if(force && btn){ btn.textContent='重新自检'; btn.disabled=false; } };
  fetch('/api/health?t='+Date.now()).then(function(r){ return r.json(); }).then(function(d){
    hcRender(d); back();
  }).catch(back);
}
function loadStatus(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    $('#wp').style.display = d.weak_pass ? 'block' : 'none';
    $('#kv').innerHTML=
      '<div><i class="kvi ic-g">'+KVIC.run+'</i><div class=kvt><b>'+esc(d.kernel.state)+'</b><span>内核容器 '+esc(d.kernel.name)+'</span></div></div>'+
      '<div><i class="kvi ic-b">'+KVIC.ver+'</i><div class=kvt><b>'+esc(d.kernel.version||'-')+((d.kernel_update&&d.kernel_update.has_update)?' <span class="pill bad">有新版 '+esc(d.kernel_update.latest)+'</span>':'')+'</b><span>mihomo 版本</span></div></div>'+
      '<div><i class="kvi ic-k">'+KVIC.ap+'</i><div class=kvt><b>'+esc(d.airports.length)+'</b><span>机场数量</span></div></div>'+
      '<div><i class="kvi ic-i">'+KVIC.nd+'</i><div class=kvt><b>'+esc(d.manual_count)+'</b><span>手动节点</span></div></div>'+
      '<div><i class="kvi ic-c">'+KVIC.ip+'</i><div class=kvt><b style="font-size:13px">'+esc(d.out_ip||'未测出')+'</b><span>代理出口 IP</span></div></div>'+
      '<div><i class="kvi ic-v">'+KVIC.pa+'</i><div class=kvt><b style="font-size:13px">'+esc(d.proxy_addr)+'</b><span>代理地址</span></div></div>'+
      '<div><i class="kvi ic-o">'+KVIC.ui+'</i><div class=kvt><b style="font-size:13px"><a href="'+esc(d.panel_url)+'" target="_blank" style="color:#58a6ff">打开节点面板 ↗</a></b><span>'+esc(d.panel_url_full||d.panel_url)+'（同一端口，内嵌）</span></div></div>';
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
  hcLoad(0);
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
    // 只列「真正的手动节点」：策略组、内核内置代理（PASS/REJECT/COMPATIBLE/GLOBAL…）一律不列，
    // 否则它们会被误当成节点显示出来，还带一个删不掉的删除按钮
    var SKIP={Direct:1,Dns:1,Selector:1,URLTest:1,Fallback:1,LoadBalance:1,Relay:1,
              Pass:1,PassRule:1,Reject:1,RejectDrop:1,Compatible:1};
    Object.keys(ps).forEach(function(k){var v=ps[k];
      if(SKIP[v.type]) return;
      if(v.provider!==undefined) return; // 机场节点不在这里列
      n++;h+='<tr><td>'+esc(k)+'</td><td>'+esc(v.type)+'</td><td>'+esc(v.server||'-')+'</td><td>'+esc(v.port||'-')+
        '</td><td><button class="btn red" onclick="ndDel(`'+esc(k)+'`)">删除</button></td></tr>'});
    $('#ndt').innerHTML=h||'<tr><td colspan=5>没有手动节点 —— 想加就在这里粘贴分享链接；机场节点请到页头的「节点面板」看，策略组请到「策略」页改</td></tr>';
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
      window.MEM=mem;
      CR=(s.custom_routes||[]).map(function(x){return {domain:x.domain||'',target:x.target||'漏网之鱼'};});
      crRender();
      RSDEF=d.ruleset_defaults||[];
      RSU=Object.assign({},s.ruleset_urls||{});
      RSC=(s.custom_rulesets||[]).map(function(x){return {key:x.key||'',url:x.url||'',behavior:x.behavior||'domain',target:x.target||'漏网之鱼'};});
      rsRender();
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
  apiPost('/api/groups',{regions:regs,groups:g,ruleset_enabled:$('#rs_en').checked,custom_groups:cg,custom_routes:CR,ruleset_urls:RSU,custom_rulesets:RSC},0);
}
function loadTools(){
  fetch('/api/settings').then(r=>r.json()).then(function(d){
    var s=d.settings||{};
    $('#t_ip').value=s.proxy_ip||'';$('#t_ab').value=s.admin_bind||'';
    $('#t_pp').value=s.proxy_port||'7890';$('#t_pa').value=s.panel_port||'9091';
    $('#t_ad').value=s.admin_port||'9090';$('#t_sc').value=s.secret||'';
    $('#t_sch').checked=(s.schedule_enabled!==false);$('#t_st').value=s.schedule_time||'04:00';
    $('#t_dd').value=s.direct_domains||'';
  });
  loadBackups();
}
function tlSave(){
  apiPost('/api/settings',{proxy_ip:$('#t_ip').value.trim(),admin_bind:$('#t_ab').value.trim(),
    proxy_port:$('#t_pp').value.trim(),panel_port:$('#t_pa').value.trim(),admin_port:$('#t_ad').value.trim(),
    secret:$('#t_sc').value, schedule_enabled:$('#t_sch').checked, schedule_time:$('#t_st').value.trim()},0);
}
function ddSave(){
  var el=$('#t_dd'), parts=(el.value||'').replace(/;/g,',').split(','), out=[];
  for(var i=0;i<parts.length;i++){ var x=parts[i].trim(); if(x){ out.push(x); } }
  el.value=out.join(',');
  apiPost('/api/settings',{direct_domains:el.value},1).then(function(d){
    if(d&&d.ok){ upModal('✅ 已保存直连域名<div style="margin-top:8px" class=hint>点「立即更新配置」后生效：DNS 返回真实 IP + 域名走直连</div><div style="margin-top:12px"><button class=btn onclick="upClose()">知道了</button></div>'); }
  });
}
var CR=[];
function crRender(){
  var box=document.getElementById('cr');
  if(!box){ return; }
  var mem=(window.MEM||[]).filter(function(x){ return x && x!=='直连' && x!=='全部手动' && x!=='全部自动'; });
  var h='', i, j, it;
  for(i=0;i<CR.length;i++){
    it=CR[i]||{};
    h+='<div class=row style="margin-bottom:8px">'
      +'<div style="flex:2 1 240px"><label>网站域名</label><input class=cr_d value="'+esc(it.domain||'')+'" placeholder="example.com"></div>'
      +'<div style="flex:1 1 170px"><label>去向</label><select class=cr_t>'
      +'<option value="直连"'+((it.target==='直连')?' selected':'')+'>直连（不走代理）</option>'
      +'<option value="漏网之鱼"'+((!it.target||it.target==='漏网之鱼')?' selected':'')+'>代理（漏网之鱼）</option>';
    for(j=0;j<mem.length;j++){
      h+='<option value="'+esc(mem[j])+'"'+((it.target===mem[j])?' selected':'')+'>'+esc(mem[j])+'</option>';
    }
    h+='</select></div>'
      +'<div style="flex:0 0 auto;align-self:flex-end"><button class="btn2 red" onclick="crDel('+i+')">删除</button></div>'
      +'</div>';
  }
  box.innerHTML=h||'<div class=hint>还没有规则 —— 点下面「➕ 添加一行」，比如把某个网站设成直连或走某个地区组。</div>';
  var ds=box.querySelectorAll('.cr_d'), ts=box.querySelectorAll('.cr_t'), k;
  for(k=0;k<ds.length;k++){ (function(k){ ds[k].oninput=function(){ if(CR[k]){CR[k].domain=ds[k].value;} }; })(k); }
  for(k=0;k<ts.length;k++){ (function(k){ ts[k].onchange=function(){ if(CR[k]){CR[k].target=ts[k].value;} }; })(k); }
}
function crAdd(){ CR.push({domain:'',target:'漏网之鱼'}); crRender(); }
function crDel(i){ CR.splice(i,1); crRender(); }
var RSDEF=[],RSU={},RSC=[];
function rsRender(){
  var box=document.getElementById('rs');
  if(!box){ return; }
  var mem=(window.MEM||[]).filter(function(x){ return x && x!=='直连'; });
  var h='', i, j, it, k;
  h+='<div style="font-size:13px;font-weight:600;margin:12px 0 6px">默认规则集的地址（留空＝用默认，地址可直接改）</div>';
  for(i=0;i<RSDEF.length;i++){
    it=RSDEF[i];
    h+='<div class=row style="margin-bottom:6px">'
      +'<div style="flex:0 0 140px;align-self:center"><code>'+esc(it.key)+'</code></div>'
      +'<div style="flex:1 1 300px"><input class=rs_u data-k="'+esc(it.key)+'" value="'+esc(RSU[it.key]||'')+'" placeholder="'+esc(it.url)+'"></div>'
      +'</div>';
  }
  h+='<div style="font-size:13px;font-weight:600;margin:16px 0 6px">自定义规则集（在默认规则之后、漏网之鱼之前匹配）</div>';
  if(!RSC.length){ h+='<div class=hint>还没有自定义规则集。</div>'; }
  for(i=0;i<RSC.length;i++){
    it=RSC[i]||{};
    h+='<div class=row style="margin-bottom:6px">'
      +'<div style="flex:0 0 150px"><input class=rc_k value="'+esc(it.key||'')+'" placeholder="名称，如 myrule"></div>'
      +'<div style="flex:1 1 260px"><input class=rc_u value="'+esc(it.url||'')+'" placeholder="https://example.com/my.mrs"></div>'
      +'<div style="flex:0 0 150px"><select class=rc_t>'
      +'<option value="漏网之鱼"'+((!it.target||it.target==='漏网之鱼')?' selected':'')+'>漏网之鱼</option>'
      +'<option value="直连"'+((it.target==='直连')?' selected':'')+'>直连</option>';
    for(j=0;j<mem.length;j++){
      h+='<option value="'+esc(mem[j])+'"'+((it.target===mem[j])?' selected':'')+'>'+esc(mem[j])+'</option>';
    }
    h+='</select></div>'
      +'<div style="flex:0 0 auto;align-self:flex-end"><button class="btn2 red" onclick="rsDel('+i+')">删除</button></div>'
      +'</div>';
  }
  box.innerHTML=h;
  var us=box.querySelectorAll('.rs_u'), ks=box.querySelectorAll('.rc_k'),
      cu=box.querySelectorAll('.rc_u'), ct=box.querySelectorAll('.rc_t');
  for(k=0;k<us.length;k++){ (function(k){ us[k].oninput=function(){ RSU[us[k].getAttribute('data-k')]=us[k].value.trim(); }; })(k); }
  for(k=0;k<ks.length;k++){ (function(k){ ks[k].oninput=function(){ if(RSC[k]){RSC[k].key=ks[k].value.trim();} }; })(k); }
  for(k=0;k<cu.length;k++){ (function(k){ cu[k].oninput=function(){ if(RSC[k]){RSC[k].url=cu[k].value.trim();} }; })(k); }
  for(k=0;k<ct.length;k++){ (function(k){ ct[k].onchange=function(){ if(RSC[k]){RSC[k].target=ct[k].value;} }; })(k); }
}
function rsAdd(){ RSC.push({key:'',url:'',behavior:'domain',target:'漏网之鱼'}); rsRender(); }
function rsDel(i){ RSC.splice(i,1); rsRender(); }
function rsReset(){ RSU={}; rsRender(); }

var ICI={'字节抖音':'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#1677FF" d="m19.9 1.5 4.1 1v19l-4.1 1zM6.5 10.9l4.1 1v9l-4 1.1zM0 2.6l4.1 1v16.8l-4.1 1zm17.5 5.6v11.1l-4.2-1v-9z"></path></svg>','Bilibili':'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#FB7299" d="M17.813 4.653h.854q2.266.08 3.773 1.574Q23.946 7.72 24 9.987v7.36q-.054 2.266-1.56 3.773c-1.506 1.507-2.262 1.524-3.773 1.56H5.333q-2.266-.054-3.773-1.56C.053 19.614.036 18.858 0 17.347v-7.36q.054-2.267 1.56-3.76t3.773-1.574h.774l-1.174-1.12a1.23 1.23 0 0 1-.373-.906q0-.534.373-.907l.027-.027q.4-.373.92-.373t.92.373L9.653 4.44q.107.106.187.213h4.267a.8.8 0 0 1 .16-.213l2.853-2.747q.4-.373.92-.373c.347 0 .662.151.929.4s.391.551.391.907q0 .532-.373.906zM5.333 7.24q-1.12.027-1.88.773q-.76.748-.786 1.894v7.52q.026 1.146.786 1.893t1.88.773h13.334q1.12-.026 1.88-.773t.786-1.893v-7.52q-.026-1.147-.786-1.894t-1.88-.773zM8 11.107q.56 0 .933.373q.375.374.4.96v1.173q-.025.586-.4.96q-.373.375-.933.374c-.56-.001-.684-.125-.933-.374q-.375-.373-.4-.96V12.44q0-.56.386-.947q.387-.386.947-.386m8 0q.56 0 .933.373q.375.374.4.96v1.173q-.025.586-.4.96q-.373.375-.933.374c-.56-.001-.684-.125-.933-.374q-.375-.373-.4-.96V12.44q.025-.586.4-.96q.373-.373.933-.373"></path></svg>','腾讯微信':'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#09B83E" d="M8.7 2.19C3.9 2.19 0 5.48 0 9.53c0 2.21 1.17 4.2 3 5.55a.6.6 0 0 1 .21.66l-.39 1.48q-.03.11-.04.22c0 .16.13.3.29.3a.3.3 0 0 0 .16-.06l1.9-1.11a.9.9 0 0 1 .72-.1 10 10 0 0 0 2.84.4q.41-.01.81-.05a5.85 5.85 0 0 1 1.93-6.45 8.3 8.3 0 0 1 5.86-1.83c-.58-3.59-4.2-6.35-8.6-6.35m-2.9 3.8c.64 0 1.16.53 1.16 1.18a1.17 1.17 0 0 1-1.16 1.18 1.17 1.17 0 0 1-1.17-1.18c0-.65.52-1.18 1.17-1.18m5.8 0c.65 0 1.17.53 1.17 1.18a1.17 1.17 0 0 1-1.16 1.18 1.17 1.17 0 0 1-1.16-1.18c0-.65.52-1.18 1.16-1.18m5.34 2.87a8 8 0 0 0-5.28 1.78 5.5 5.5 0 0 0-1.78 6.22c.94 2.46 3.66 4.23 6.88 4.23q1.25 0 2.36-.33a.7.7 0 0 1 .6.08l1.59.93.14.04c.13 0 .24-.1.24-.24q-.01-.09-.04-.18l-.33-1.23-.02-.16a.5.5 0 0 1 .2-.4 5.8 5.8 0 0 0 2.5-4.62c0-3.21-2.93-5.84-6.66-6.09zm-2.53 3.27c.53 0 .97.44.97.98a1 1 0 0 1-.97.99 1 1 0 0 1-.97-.99c0-.54.43-.98.97-.98zm4.84 0c.54 0 .97.44.97.98a1 1 0 0 1-.97.99 1 1 0 0 1-.97-.99c0-.54.44-.98.97-.98"></path></svg>','阿里淘宝':'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#E16322" d="M21.31 9.9a3 3 0 1 1 0 1.92.96.96 0 0 1 0-1.92m2.39 3.05H13.3v-.96h4.14V9.76h-2.89v-.77h2.9v-.92h-2.52v.2H13.3v-2.9h1.64v.35l2.52-.3V4.6h1.85v.64c.93-.08 1.76-.13 2.21-.1 1.5.06 2.45.27 2.49 1.26.03 1-1.43 1.9-1.43 1.9l-.45-.43v.2h-2.8v.92h3.22v.77h-3.23v2.23h4.39zM21.53 7.3l-.02-.01s1.38-.76.35-1.27c-.87-.43-5.54.3-6.93.62v.66zM1.88 6.42a1 1 0 0 0 0-2 1 1 0 0 0-1 1 1 1 0 0 0 1 1m3.41-.86a7 7 0 0 0 .37-.72L4.2 4.42s-.6 1.93-1.65 2.83c0 0 1.02.6 1.01.58a10 10 0 0 0 .78-.88l.68-.3a9 9 0 0 1-1.14 1.69l.61.54s.42-.4.88-.9h.53v.9H3.86v.73H5.9v1.72h-.08c-.23-.01-.58-.05-.71-.27-.17-.26-.05-.75-.04-1.04h-1.4l-.06.02s-.52 2.32 1.5 2.27a5.3 5.3 0 0 0 3.46-.92l.2.76 1.17-.48-.79-1.92-.94.3.18.65a3 3 0 0 1-.82.42V9.6h2v-.72h-2v-.9h2v-.72H6.01c.26-.31.46-.6.51-.78l-.62-.17c2.67-.95 4.15-.79 4.13.77v4.12s.16 1.4-1.46 1.3l-.87-.18-.21.83s3.78 1.08 4.1-1.82-.09-4.76-.09-4.76-.34-2.68-6.2-1.02zm-5.23 6.6 1.58.98c1.1-2.38 1.03-2.06 1.3-2.92.29-.87.35-1.54-.13-2.02a10 10 0 0 0-1.6-1.36L.55 7.86l1.21.75s.82.42.43 1.2c-.36.73-2.13 2.34-2.13 2.34M20 19s-.02.53-.67.53c-.6 0-.64-.42-.64-.42q-.39.45-1.07.46c-.76 0-1.3-.51-1.3-1.3 0-.78.56-1.27 1.4-1.27.39 0 .73.15.93.4l.01-.2c0-.56-.3-.8-1-.8q-.51 0-1.02.13.16-.33.3-.45.18-.14.94-.14c1.27 0 1.74.42 1.74 1.42v1.2c0 .32.03.44.38.44m-1.33-.74c0-.48-.25-.75-.64-.75-.4 0-.66.28-.66.76 0 .47.27.76.65.76.39 0 .65-.27.65-.77m5.27-.5c0 1.15-.7 1.82-1.78 1.82-1.1 0-1.77-.67-1.77-1.82 0-1.16.68-1.83 1.77-1.83s1.78.67 1.78 1.83m-1.08 0q0-1.3-.7-1.3t-.69 1.3.7 1.3.69-1.3m-7.14-.05c0 1.17-.65 1.86-1.57 1.86q-.66-.01-1.05-.47s-.1.42-.66.42c-.69 0-.67-.52-.67-.52.4.02.38-.21.38-.43v-2.89c0-.36-.07-.48-.43-.49.02-.1.08-.53.68-.53.82 0 .76.91.76.91v.79q.34-.4 1-.4c.96 0 1.56.65 1.56 1.75m-1.09.08q-.01-1.35-.76-1.35c-.44 0-.74.4-.74 1.1v.36c0 .72.31 1.12.76 1.12q.73 0 .74-1.23m-3.24-.03c0 1.15-.7 1.82-1.78 1.82-1.1 0-1.78-.67-1.78-1.82 0-1.16.68-1.83 1.78-1.83s1.78.67 1.78 1.83m-1.09 0q0-1.3-.7-1.3-.69 0-.68 1.3t.69 1.3q.7 0 .7-1.3m-6-2.72q-.4.11-1.55.1-1.38-.02-1.85-.04c-.52 0-.73.13-.91.66q.45-.13 1.13-.11c.36 0 .42.04.42.3v2.9c0 .28.11.67.72.67.71 0 .84-.52.84-.52-.36 0-.43-.13-.43-.49v-2.56c0-.27.1-.28.47-.28h.26c.55 0 .7-.1.9-.63M7.46 19s-.02.52-.67.52c-.56 0-.64-.4-.64-.4q-.39.45-1.07.45c-.76 0-1.3-.52-1.3-1.3S4.33 17 5.17 17c.39 0 .73.14.93.4v-.2c0-.56-.3-.8-1-.8q-.5 0-1.01.13.15-.33.3-.46.17-.14.94-.14c1.26 0 1.74.43 1.74 1.43v1.2c0 .32.03.44.38.44m-1.33-.75c0-.48-.26-.74-.64-.74-.4 0-.67.28-.67.76 0 .46.28.76.66.76.39 0 .65-.28.65-.78"></path></svg>','GitHub':'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#181717" d="M12 .3a12 12 0 0 0-3.8 23.38c.6.12.83-.26.83-.57L9 21.07c-3.34.72-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.08-.74.09-.73.09-.73 1.2.09 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.31.76-1.61-2.66-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.14-.3-.54-1.52.1-3.18 0 0 1-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.28-1.55 3.29-1.23 3.29-1.23.64 1.66.24 2.88.12 3.18a4.7 4.7 0 0 1 1.23 3.22c0 4.61-2.8 5.63-5.48 5.92.42.36.81 1.1.81 2.22l-.01 3.29c0 .31.2.69.82.57A12 12 0 0 0 12 .3"></path></svg>','Telegram.DC5':'<svg width="24px" height="24px" viewBox="0 0 16 16" version="1.1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><defs><linearGradient x1="50%" y1="0%" x2="50%" y2="100%" id="linearGradient-1"><stop stop-color="#38AEEB" offset="0%"></stop><stop stop-color="#279AD1" offset="100%"></stop></linearGradient></defs><g id="Artboard" stroke="none" stroke-width="1" fill="none" fill-rule="evenodd"><circle id="Oval" fill="url(#linearGradient-1)" cx="8" cy="8" r="8"></circle><path d="M3.17026167,7.83635602 C5.78750201,6.74265999 7.53273882,6.02162863 8.40597211,5.67326193 C10.8992306,4.67860423 11.2454541,4.53439191 11.5831299,4.52864956 C11.6573986,4.52743168 11.8385417,4.55776042 11.9798438,4.67645833 C12.1211458,4.79515625 12.1635786,4.87206678 12.1755371,4.93908691 C12.1874957,5.00610705 12.1862759,5.21456762 12.1744385,5.3338623 C12.0393279,6.69547283 11.5259342,9.83829771 11.2285121,11.3633248 C11.1026617,12.008621 10.8548582,12.2249854 10.6149558,12.2461596 C10.0935924,12.2921758 9.69769267,11.9156852 9.19272668,11.5981993 C8.40255458,11.1013965 8.13911734,10.9180161 7.3721185,10.4332283 C6.48571864,9.87297217 6.85080034,9.6784879 7.35595703,9.17524981 C7.48815894,9.04355001 9.67825076,7.04590073 9.71077046,6.86250183 C9.7391276,6.70257812 9.7494847,6.68189389 9.67664063,6.60973958 C9.60379655,6.53758527 9.51674192,6.54658941 9.46083149,6.55876051 C9.38158015,6.57601267 8.17521836,7.33962686 5.84174612,8.84960308 C5.48344358,9.08558775 5.15890428,9.20056741 4.86812819,9.19454205 C4.54757089,9.18789957 3.93094724,9.02070014 3.47255094,8.87778221 C2.91030922,8.70248755 2.46345069,8.609808 2.50236203,8.31210343 C2.52262946,8.15704047 2.74526267,7.998458 3.17026167,7.83635602 Z" id="Path-3" fill="#FFFFFF"></path></g></svg>','X.com':'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#1DA1F2" d="M23.643 4.937c-.835.37-1.732.62-2.675.733.962-.576 1.7-1.49 2.048-2.578-.9.534-1.897.922-2.958 1.13-.85-.904-2.06-1.47-3.4-1.47-2.572 0-4.658 2.086-4.658 4.66 0 .364.042.718.12 1.06-3.873-.195-7.304-2.05-9.602-4.868-.4.69-.63 1.49-.63 2.342 0 1.616.823 3.043 2.072 3.878-.764-.025-1.482-.234-2.11-.583v.06c0 2.257 1.605 4.14 3.737 4.568-.392.106-.803.162-1.227.162-.3 0-.593-.028-.877-.082.594 1.85 2.323 3.196 4.368 3.233-1.595 1.25-3.604 1.995-5.786 1.995-.376 0-.747-.022-1.112-.065 2.072 1.328 4.532 2.104 7.172 2.104 8.607 0 13.3-7.132 13.3-13.3 0-.202-.005-.403-.014-.602.913-.66 1.706-1.477 2.332-2.41z"></path></svg>','YouTube':'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#FF0000" d="M23.5 6.19a3 3 0 0 0-2.12-2.14c-1.87-.5-9.38-.5-9.38-.5s-7.5 0-9.38.5A3 3 0 0 0 .5 6.19C0 8.07 0 12 0 12s0 3.93.5 5.81a3 3 0 0 0 2.12 2.14c1.87.5 9.38.5 9.38.5s7.5 0 9.38-.5a3 3 0 0 0 2.12-2.14C24 15.93 24 12 24 12s0-3.93-.5-5.81M9.55 15.57V8.43L15.82 12z"></path></svg>'};
var NI=null,NI_ONCE=0,ST=null,IPMAP=null;
function niBox(tit,sub,ip,loc,line){
  var h='<div class=nib><div class=t>'+esc(tit)+'<span>'+esc(sub||'')+'</span></div>';
  h+='<div class=v>'+esc(ip||'—');
  if(ip){ h+='<i data-ip="'+esc(ip)+'" title="查询这个 IP 的详细信息">🔍</i>'; }
  h+='</div><div class=l>'+esc(loc||'—')+'</div>';
  if(line){ h+='<div class=l2>'+line+'</div>'; }
  return h+'</div>';
}
function niLoad(force){
  var b=document.getElementById('ni'), l=document.getElementById('nil');
  if(b){ b.innerHTML='<div class=hint>检测中…（国内走直连、国外走代理，约几秒）</div>'; }
  fetch('/api/netinfo'+(force?'?force=1':'')).then(function(r){return r.json()}).then(function(d){
    NI=d.data||{}; niRender();
  }).catch(function(){ if(b){ b.innerHTML='<div class=hint>检测失败（网络不通？）</div>'; } });
  if(l){ l.innerHTML='<div class=hint>测延迟中…</div>'; }
  fetch('/api/latency'+(force?'?force=1':'')).then(function(r){return r.json()}).then(function(d){
    nilRender(d.items||[]);
  }).catch(function(){ if(l){ l.innerHTML=''; } });
}
function niRender(){
  var b=document.getElementById('ni');
  if(!b){ return; }
  var d=NI||{}, cn=d.cn||{}, os=d.os||{}, w=d.wall||{}, k;
  var wh='';
  for(k in w){ wh+=(wh?'　':'')+esc(k)+(w[k]?' ✓':' ✗'); }
  var h='<div class=nig>';
  h+=niBox('国内测试', cn.src||'CN-Test', cn.ip, cn.loc, '· 你访问国内站点使用的 IP');
  h+=niBox('国外测试', os.src||'漏网之鱼', os.ip, ((os.loc||'')+(os.asn?('　'+os.asn):'')), '· 你访问国外站点使用的 IP');
  h+=niBox('墙外测试', 'Google / 推特 / YouTube', os.ip, (os.loc||''), '· '+wh);
  h+='</div>';
  b.innerHTML=h;
  var ms=b.querySelectorAll('i[data-ip]'), i;
  for(i=0;i<ms.length;i++){ (function(el){ el.onclick=function(){ ipShow(el.getAttribute('data-ip')); }; })(ms[i]); }
}
function nilRender(items){
  var l=document.getElementById('nil');
  if(!l){ return; }
  var h='<div class=nich>', i, it, ms, cls;
  for(i=0;i<items.length;i++){
    it=items[i]; ms=it.ms||0;
    cls = ms===0 ? 'w2' : (ms<120 ? 'g2' : (ms<400 ? 'y2' : 'r2'));
    h+='<div class=nic2>'+(ICI[it.name]||'')+'<span class=nm>'+esc(it.name)+'<span class=rg>'+esc(it.region||'')+'</span></span>'
      +'<b class='+cls+'>'+(ms?(ms+'ms'):'✗')+'</b></div>';
  }
  h+='</div>';
  l.innerHTML=h;
  var tn=document.getElementById('nit');
  if(tn){ tn.textContent=' · 更新于 '+new Date().toLocaleTimeString(); }
}
function colToggle(b){
  var id=b.getAttribute('data-body');
  var box=document.getElementById(id); if(!box){ return; }
  var on=box.parentNode.classList.toggle('collapsed');
  b.textContent=(on?'展开':'收起');
  try{ localStorage.setItem('mh-col-'+id, on?'1':'0'); }catch(e){}
}
(function(){
  var bs=document.querySelectorAll('button[data-body]'), i;
  for(i=0;i<bs.length;i++){
    var b=bs[i], id=b.getAttribute('data-body');
    var box=document.getElementById(id); if(!box){ continue; }
    var v='';
    try{ v=localStorage.getItem('mh-col-'+id)||''; }catch(e){}
    var on=(v==='0')?false:true;   // 默认收起，点过「展开」才展开
    if(on){ box.parentNode.classList.add('collapsed'); }
    b.textContent=(on?'展开':'收起');
  }
})();
function stLoad(force){
  var b=document.getElementById('st');
  if(b){ b.innerHTML='<tr><td colspan=6>测试中…（每个业务组发一个请求，一般 2~4 秒出表）</td></tr>'; }
  fetch('/api/splittest'+(force?'?force=1':'')).then(function(r){return r.json()}).then(function(d){
    ST=d.data||{}; stRender();
    stLat(0);
  }).catch(function(){ if(b){ b.innerHTML='<tr><td colspan=6>测试失败</td></tr>'; } });
}
function stLat(n){
  fetch('/api/splittest?lat=1').then(function(r){return r.json()}).then(function(d){
    if(!d || !d.data){ return; }
    ST=d.data; stRender();
    var rows=ST.rows||[], i, miss=0;
    for(i=0;i<rows.length;i++){ if(!rows[i].ms){ miss++; } }
    if(miss && n < 8){ setTimeout(function(){ stLat(n+1); }, 800); }
  }).catch(function(){});
}
function stRender(){
  var b=document.getElementById('st');
  if(!b){ return; }
  var rows=(ST||{}).rows||[], h='', i, r, v;
  for(i=0;i<rows.length;i++){
    r=rows[i];
    v = (r.ok===1) ? '<span class="vd d">✓ 正确</span>'
      : ((r.ok===0) ? '<span class="vd o">✗ 不符</span>' : '<span class="vd o">未抓到</span>');
    h+='<tr><td>'+esc(r.group)+'</td><td>'+esc(r.host||'')+'</td>'
      +'<td>'+(r.rule ? esc(r.rule)+'<div class=l2>'+esc(r.payload||'')+'</div>' : '—')+'</td>'
      +'<td style="font-size:12px">'+esc(r.chain||'—')+'</td>'
      +'<td>'+v+'</td>'
      +'<td>'+(r.ms ? r.ms+'ms' : '<span class=hint>测…</span>')+'</td></tr>';
  }
  b.innerHTML=h||'<tr><td colspan=6>没有结果</td></tr>';
  var sth=document.getElementById('sth');
  if(sth){
    var cnt={1:0,0:0,"-1":0}, k;
    for(k=0;k<rows.length;k++){ cnt[rows[k].ok]=(cnt[rows[k].ok]||0)+1; }
    sth.innerHTML='结果：<b style="color:#047857">正确 '+cnt[1]+'</b> ／ <b style="color:#b45309">不符 '+cnt[0]+'</b> ／ <b style="color:#94a3b8">未抓到 '+cnt[-1]+'</b>　'
      +'（每个业务组各测一个代表站点，链尾应等于该组名）';
  }
  var ms=b.querySelectorAll('i[data-ip]'), j;
  for(j=0;j<ms.length;j++){ (function(el){ el.onclick=function(){ ipShow(el.getAttribute('data-ip')); }; })(ms[j]); }
}
function ipShow(ip){
  var m=document.getElementById('ipm');
  if(!m||!ip){ return; }
  m.classList.add('on');
  var s=document.getElementById('ipm-src'), mp=document.getElementById('ipmm'), inf=document.getElementById('ipmi');
  if(s){ s.textContent='查询中…'; }
  if(mp){ mp.innerHTML=''; }
  if(inf){ inf.innerHTML='<div class=hint>正在查询…</div>'; }
  fetch('/api/ipinfo?ip='+encodeURIComponent(ip)).then(function(r){return r.json()}).then(function(d){
    ipFill(ip,d.data||{});
  }).catch(function(){ if(inf){ inf.innerHTML='<div class=hint>查询失败</div>'; } });
}
function ipHide(){
  var m=document.getElementById('ipm');
  if(m){ m.classList.remove('on'); }
  if(IPMAP){ try{ IPMAP.remove(); }catch(e){} IPMAP=null; }
}
function ipNum(v){ var n=parseFloat(v); return isNaN(n)?0:n; }
function ipFill(ip,data){
  var r=(data||{}).raw||{}, src=(data||{}).src||'';
  var s=document.getElementById('ipm-src');
  if(s){ s.textContent = src ? ('数据来源：'+src) : '未取到数据'; }
  var country=r.country||r.country_code||'', city=r.city||'', region=r.region||'';
  var org=r.company||r.isp||r.org||r.organization||r.as_name||'';
  var asn=r.asn||r.asn_num||'', tz=r.timezone||'';
  var lat=ipNum(r.lat||r.latitude), lon=ipNum(r.lon||r.longitude);
  var geo=[country,region,city].filter(function(x){return !!x;}).join(' ');
  var h='<div><h3><span class="ic sm ic-b"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M12 11v6M12 8h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 基本信息</h3><table>'
    +'<tr><td>IP 地址</td><td>'+esc(ip)+'</td></tr>'
    +'<tr><td>地理位置</td><td>'+esc(geo||'未知')+'</td></tr>'
    +'<tr><td>时区</td><td>'+esc(tz||'未知')+'</td></tr>'
    +'<tr><td>运营商</td><td>'+esc(org||'未知')+'</td></tr>'
    +'<tr><td>ASN</td><td>'+esc(String(asn)||'未知')+'</td></tr>'
    +'</table></div>';
  var sec=[['数据中心','is_datacenter'],['代理服务器','is_proxy'],['VPN 连线','is_vpn'],
           ['Tor 网络','is_tor'],['网络爬虫','is_crawler'],['移动网络','is_mobile'],
           ['卫星网络','is_satellite'],['已知滥用','is_abuser'],['保留地址','is_bogon']];
  var rows='', n=0, i, v;
  for(i=0;i<sec.length;i++){
    v=r[sec[i][1]];
    if(v===undefined||v===null){ continue; }
    n++;
    rows+='<tr><td>'+esc(sec[i][0])+'</td><td class="'+(v?'no2':'ok2')+'">'+(v?'是':'否')+'</td></tr>';
  }
  var risk=r.risk;
  if(risk!==undefined&&risk!==null&&risk!==''){
    n++;
    var rv=ipNum(risk);
    rows+='<tr><td>风控评级</td><td>'+esc(String(rv))+'</td></tr>';
  }
  if(n){ h+='<div><h3><span class="ic sm ic-g"><svg viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4-3 7.5-7 9-4-1.5-7-5-7-9V6z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M9 12l2 2 4-4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> 安全检测</h3><table>'+rows+'</table>'
    +'<div class=hint style="margin-top:6px">'+(src==='ipapi.is'?'免费档不一定返回全部项，缺失的不会显示':'')+'</div></div>'; }
  var inf=document.getElementById('ipmi');
  if(inf){ inf.innerHTML=h; }
  var tip='<b>'+esc(city||region||country||ip)+'</b><br>落地IP: '+esc(ip)+(asn?('<br>ASN: '+esc(String(asn))):'')+(org?('<br>'+esc(org)):'');
  ipMap(lat,lon,tip);
}
function ipMap(lat,lon,tip){
  var mp=document.getElementById('ipmm');
  if(!mp){ return; }
  if(!window.L){
    mp.innerHTML='<div class=hint style="padding:16px">地图库加载中…</div>';
    var css=document.createElement('link');
    css.rel='stylesheet';
    css.href='https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css';
    document.head.appendChild(css);
    var sc=document.createElement('script');
    sc.src='https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js';
    sc.onload=function(){ ipMap(lat,lon,tip); };
    sc.onerror=function(){
      var t2=document.createElement('script');
      t2.src='https://fastly.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js';
      t2.onload=function(){ ipMap(lat,lon,tip); };
      t2.onerror=function(){ mp.innerHTML='<div class=hint style="padding:16px">地图库加载失败（无外网或被拦）</div>'; };
      document.head.appendChild(t2);
    };
    document.head.appendChild(sc);
    return;
  }
  if(!lat||!lon){ mp.innerHTML='<div class=hint style="padding:16px">这个 IP 没有经纬度，无法画地图</div>'; return; }
  if(IPMAP){ try{ IPMAP.remove(); }catch(e){} IPMAP=null; }
  mp.innerHTML='';
  IPMAP=L.map(mp).setView([lat,lon],10);
  L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    {subdomains:'1234',maxZoom:18,attribution:'高德地图'}).addTo(IPMAP);
  L.marker([lat,lon]).addTo(IPMAP).bindPopup(tip).openPopup();
  setTimeout(function(){ try{ IPMAP.invalidateSize(); }catch(e){} },150);
}

var NI_AUTO=1,NI_BUSY=0,NI_LAST=0;
function niVisible(){ var s=document.getElementById('tab-status'); return !!s && !s.hidden; }
function niAutoToggle(){ var c=document.getElementById('niauto'); NI_AUTO = c && c.checked ? 1 : 0; }
function niLatTick(){
  if(!NI_AUTO || document.hidden || !niVisible() || !NI || NI_BUSY){ return; }
  NI_BUSY=1;
  fetch('/api/latency?force=1').then(function(r){return r.json()}).then(function(d){
    nilRender(d.items||[]); NI_BUSY=0;
  }).catch(function(){ NI_BUSY=0; });
}
function niNetTick(){
  if(!NI_AUTO || document.hidden || !niVisible() || !NI){ return; }
  fetch('/api/netinfo').then(function(r){return r.json()}).then(function(d){
    if(d&&d.data){ NI=d.data; niRender(); }
  }).catch(function(){});
}
setInterval(niLatTick, 6000);
setInterval(niNetTick, 60000);
setTimeout(function(){ if(!NI){ niLoad(0); } },900);
setTimeout(function(){ if(!ST){ stLoad(0); } },3200);
function thmSet(t){
  document.documentElement.className=(t==='dark'?'dark':'');
  var b=document.getElementById('thm');
  if(b){ b.textContent=(t==='dark'?'☀️':'🌙'); b.title=(t==='dark'?'切换到浅色':'切换到深色'); }
}
function thmTgl(){
  var cur=(document.documentElement.className==='dark')?'light':'dark';
  try{ localStorage.setItem('mh-theme',cur); }catch(e){}
  thmSet(cur);
}
(function(){
  var th='';
  try{ th=localStorage.getItem('mh-theme')||''; }catch(e){}
  if(!th){ th=(window.matchMedia&&window.matchMedia('(prefers-color-scheme:dark)').matches)?'dark':'light'; }
  thmSet(th);
})();
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
function sbLink(t,target){ if(!target||target==='auto'){ return location.origin+'/sub/'+t; } return location.origin+'/sub/'+t+'?target='+target; }
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
  var h='', i, it, la, l1, l2, l3, l4;
  for(i=0;i<SB.length;i++){
    it=SB[i];
    la=sbLink(it.token,'auto'); l1=sbLink(it.token,'clash'); l2=sbLink(it.token,'v2ray');
    l3=sbLink(it.token,'singbox'); l4=sbLink(it.token,'surge');
    h+='<div style="padding:12px 0;border-bottom:1px solid #e6ecf5">'
      +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
      +'<b>'+esc(it.name)+'</b>'
      +(it.enable?'<span class="pill" style="background:#12b76a">启用</span>':'<span class="pill" style="background:#98a2b3">已停用</span>')
      +'<span class=hint>'+esc(it.n_nodes||0)+' 个节点 · '+esc(it.n_urls||0)+' 个来源订阅'+((it.regions&&it.regions.length)?' · 地区：'+esc(it.regions.join('/')):'')+'</span>'
      +'<span class=hint>短链 <code>/sub/'+esc(it.token)+'</code></span>'
      +'<span class=sp></span>'
      +'<button class=btn2 data-act=rename data-tok="'+esc(it.token)+'">改短链</button>'
      +'<button class=btn2 data-act=toggle data-tok="'+esc(it.token)+'" data-en="'+(it.enable?1:0)+'">'+(it.enable?'停用':'启用')+'</button>'
      +'<button class="btn2 red" data-act=del data-tok="'+esc(it.token)+'">删除</button>'
      +'</div>'
      +'<div class=hint style="margin-top:8px;word-break:break-all">⭐ 自适应（一个链接通吃所有客户端）：<code>'+esc(la)+'</code> <button class=btn2 data-act=copy data-txt="'+esc(la)+'">复制</button> <button class=btn2 data-act=qr data-txt="'+esc(la)+'">二维码</button></div>'
      +'<div class=hint style="word-break:break-all">Clash：<code>'+esc(l1)+'</code> <button class=btn2 data-act=copy data-txt="'+esc(l1)+'">复制</button> <button class=btn2 data-act=qr data-txt="'+esc(l1)+'">二维码</button></div>'
      +'<div class=hint style="word-break:break-all">V2Ray：<code>'+esc(l2)+'</code> <button class=btn2 data-act=copy data-txt="'+esc(l2)+'">复制</button> <button class=btn2 data-act=qr data-txt="'+esc(l2)+'">二维码</button></div>'
      +'<div class=hint style="word-break:break-all">sing-box：<code>'+esc(l3)+'</code> <button class=btn2 data-act=copy data-txt="'+esc(l3)+'">复制</button></div>'
      +'<div class=hint style="word-break:break-all">Surge：<code>'+esc(l4)+'</code> <button class=btn2 data-act=copy data-txt="'+esc(l4)+'">复制</button></div>'
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
    else if(a==='qr'){ sbQR(b.getAttribute('data-txt')||''); }
    else if(a==='rename'){ sbRename(b.getAttribute('data-tok')||''); }
  };
}
function sbQR(txt){
  try{
    var q=qrcode(0,'M'); q.addData(txt); q.make();
    var svg='';
    try{ svg=q.createSvgTag({cellSize:4,margin:2,scalable:true}); }catch(e1){ svg=q.createSvgTag(4,2); }
    upModal('<div style="text-align:center"><div style="margin-bottom:8px;font-weight:600">扫码导入订阅</div>'
      +'<div style="background:#fff;padding:10px;display:inline-block;border-radius:10px;border:1px solid #e6ecf5">'+svg+'</div>'
      +'<div class=hint style="margin-top:10px;word-break:break-all">'+esc(txt)+'</div>'
      +'<div style="margin-top:12px"><button class=btn onclick="upClose()">关闭</button></div></div>');
  }catch(e){ prompt('复制链接：', txt); }
}
function sbRename(t){
  var c=prompt('设置自定义短链（字母/数字/下划线/短横线，1~40 位）', t);
  if(c===null){ return; }
  c=(c||'').trim(); if(!c){ return; }
  apiPost('/api/sublinks',{action:'rename',token:t,code:c},1).then(function(d){ if(d&&d.ok){ sbLoad(); } });
}
function sbCopy(txt){
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(function(){ upModal('✅ 已复制到剪贴板<div style="margin-top:12px"><button class=btn onclick="upClose()">知道了</button></div>'); },
      function(){ prompt('手动复制：',txt); });
  } else { prompt('手动复制：',txt); }
}
function sbSave(){
  var raw=((document.getElementById('sb_urls')||{}).value||'').split(String.fromCharCode(10)), lines=[], i, u;
  for(i=0;i<raw.length;i++){ u=raw[i].trim(); if(u){ lines.push(u); } }
  var regions=[];
  document.querySelectorAll('.sbrg').forEach(function(c){ if(c.checked){ regions.push(c.value); } });
  apiPost('/api/sublinks',{action:'save',name:((document.getElementById('sb_name')||{}).value||''),code:((document.getElementById('sb_code')||{}).value||''),urls:lines,regions:regions},1)
   .then(function(d){
     if(d && d.ok){
       var ta=document.getElementById('sb_urls'); if(ta){ ta.value=''; }
       var nm=document.getElementById('sb_name'); if(nm){ nm.value=''; }
       var cd=document.getElementById('sb_code'); if(cd){ cd.value=''; }
       sbLoad();
     }
   });
}
function sbDel(t){ if(!confirm('删除这条订阅？链接会立刻失效，朋友那边就用不了了。')){ return; } apiPost('/api/sublinks',{action:'del',token:t},1).then(function(){ sbLoad(); }); }
function sbToggle(t,e){ apiPost('/api/sublinks',{action:'toggle',token:t,enable:!!e},1).then(function(){ sbLoad(); }); }

loadStatus();
</script>

<div id=mhui style="display:none;position:fixed;inset:0;z-index:9998;background:#0d1117">
 <div style="height:38px;line-height:38px;padding:0 12px;background:#161b22;color:#c9d1d9;font-size:13px">
   节点面板 metacubexd —— 地址已自动带出（<b>http://当前地址/clash</b>），密钥可留空（管理台登录过就自动带上）
   <button onclick="document.getElementById('mhui').style.display='none'" style="float:right;margin-top:8px;padding:2px 10px;border:1px solid #30363d;border-radius:6px;background:#21262d;color:#c9d1d9;cursor:pointer">关闭 ✕</button>
 </div>
 <iframe id=mhuiF style="width:100%;height:calc(100% - 38px);border:0"></iframe>
</div><div id=ipm><div class=ipmb>
  <div class=ipmh><span class="ic ic-b"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M16 16l4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></span> IP 详细信息 <span class=src id=ipm-src></span><button onclick="ipHide()">×</button></div>
  <div id=ipmm></div>
  <div class=ipmi id=ipmi></div>
</div></div>
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
