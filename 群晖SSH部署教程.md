# 群晖 SSH 部署教程（mihomo + 中文网页管理台）

一条命令搞定，全程约 3 分钟。


---

## 一、前提

1. **DSM 7.2 或更高**
2. **套件中心**里装好 **Container Manager**（旧名 Docker）

   > 套件中心 → 搜索 `Container Manager` → 安装 → 打开一次确认能启动

3. 知道**群晖的 IP**（就是你现在访问 DSM 用的地址，例如 `192.168.1.100`）
4. （可选）一个**机场订阅链接**（Clash 格式），装好后在管理台里加也行

---

## 二、开启 SSH

控制面板 → **终端机和 SNMP** → 勾选 **启动 SSH 功能** → 端口保持 `22` → 应用

然后用电脑连上（Windows 用 PowerShell / MobaXterm，Mac 用终端）：

```bash
ssh 你的用户名@群晖IP
```

输入密码进去后，**切换到 root**：

```bash
sudo -i
```

> 密码就是你登录 DSM 的密码。

> 📷 截图占位：控制面板「终端机和 SNMP」勾选 SSH

---

## 三、一键部署

```bash
curl -fsSL https://raw.githubusercontent.com/amw1933/synology-mihomo/main/mihomo-install.sh | bash
```

想顺便把订阅一起导进去、并指定管理台密码：

```bash
curl -fsSL https://raw.githubusercontent.com/amw1933/synology-mihomo/main/mihomo-install.sh | \
  SUB_URL='你的订阅链接' ADMIN_PASS='你的密码' bash
```

脚本会依次做这些事（**10 个步骤，每一步都有提示**）：

| 步骤 | 做什么 |
|---|---|
| 1 | **检查环境** → 找到 `docker` / `docker compose` |
| 2 | **识别本机 IP** → 自动填进配置，不用手改 |
| 3 | **管理台密码** → 不填则随机生成并打印 |
| 4 | **下载程序文件** → `settings-apply.sh` / `dsm-fix.sh` / 管理台 / `compose.yaml` |
| 5 | **生成配置** → 端口、IP、密码按你的参数填好，写 `settings.json` |
| 6 | **构建并启动** → 构建管理台镜像（约 1-2 分钟）→ 启动容器 |
| 7 | **绑定 IP** → 若绑定地址不是本机地址，自动加到网卡并写开机自启 |
| 8 | **定期清理 DSM 记录** → 写 `/etc/cron.d/mh-dsmfix`（每 10 分钟） |
| 9 | **生成节点配置** → 登录管理台调 `/api/gen` |
| 10 | **节点面板 metacubexd** —— 首次启动时后台自动安装，无需操作 |

> **注意**：脚本默认**自动识别 IP**。想专门用一个内网地址，加参数 `MH_IP=192.168.1.250`，
> 脚本会把地址加到网卡上，并写开机自启 `/usr/local/etc/rc.d/mhip.sh`。

### 可用的参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `SUB_URL` | 空 | 订阅链接（Clash 格式），不填就装好后在网页里加 |
| `ADMIN_PASS` | 随机生成 | 管理台密码 / 面板密钥，脚本会打印出来 |
| `MH_IP` | 自动识别 | 绑定 IP |
| `PROXY_PORT` | `7890` | 代理端口（HTTP / SOCKS5） |
| `PANEL_PORT` | `9091` | **内核接口**端口（只绑 `127.0.0.1`，不对外，不用放通；面板已和管理台同端口） |
| `ADMIN_PORT` | `9090` | 管理台端口 |
| `BASE_DIR` | `/volume1/docker/mihomo` | 安装目录 |
| `CT_PREFIX` | 空 | 容器名前缀（同机装第二套时用，例如 `CT_PREFIX=t-`） |

---

## 四、看结果

脚本最后会打印这样一段：

```
============================================================
  部署完成
============================================================
  代理地址     192.168.1.100:7890   （HTTP / SOCKS5）
  节点面板     http://192.168.1.100:9090/ui/
               面板密钥 = 管理台密码
  网页管理台   http://192.168.1.100:9090
  管理台密码   xxxxxxxxxx
============================================================
```

> 🔑 **记下管理台密码**！没填 `ADMIN_PASS` 时是**随机生成**的，只在安装输出里出现这一次。
> 忘了可以改：编辑 `/volume1/docker/mihomo/compose.yaml` 里的 `- ADMIN_PASS=新密码`，
> 再 `cd /volume1/docker/mihomo && docker compose -p mihomo up -d`。

顺手确认一下（在 SSH 里）：

```bash
docker ps --filter name=mihomo --format '{{.Names}} {{.Status}} {{.Ports}}'
docker exec mihomo /mihomo -t -d /root/.config/mihomo -f /root/.config/mihomo/config.yaml
```

能看到 `mihomo Up ...`，配置校验输出 `test is successful` 就说明内核和配置都好了。

---

## 五、还没订阅？进管理台加

浏览器打开 `http://群晖IP:9090`，输入密码，然后：

1. **机场** → 粘贴订阅链接（Clash 格式）→ **添加机场**
   - 保存后**自动更新配置**，弹窗提示「正在更新配置…」→「✅ 更新完毕」，不用再点状态页
   - 有多个机场就一个一个加，**第 1 个是主订阅**（决定 DNS / 规则 / 入口）
   - 每个机场是独立的 `proxy-providers`，换机场只动一处，策略组自动跟着变
2. 节点面板**已自动装好**：直接开 `http://群晖IP:9090/ui/`（不弹设置页、不用填密钥）。想重装走 **工具 → 节点面板界面**
3. 点右上角 **打开节点面板** → 就能看到所有节点、测速、切换

> ⚠️ mihomo 只在启动时注册 `external-ui`，安装面板成功后管理台会**自动重启一次内核容器**才生效，稍等十几秒。

> 📷 截图占位：管理台「机场」页添加订阅、策略组列表

---

## 六、客户端怎么填

| 类型 | 地址 | 端口 |
|---|---|---|
| HTTP 代理 | 群晖IP | **7890** |
| SOCKS5 代理 | 群晖IP | **7890** |

手机（Shadowrocket / v2rayNG / sing-box）里选「HTTP」或「SOCKS5」填这个地址就行。

> mihomo 这边是**混合入口**，同一个端口同时支持 HTTP 和 SOCKS5。

---

## 七、日常维护（都在网页里）

| 想做的事 | 去哪 |
|---|---|
| 换订阅、加机场 | **机场** |
| 手动加节点 | **节点**（粘贴 `vmess` / `vless`(含 reality) / `ss` / `trojan` / `hysteria2` / `tuic` / `anytls` 等分享链接） |
| 某网站走直连 / 走代理 | **规则**（下拉选 DIRECT 或策略组） |
| 换代理 / 面板 / 管理台端口 | **工具 → 端口设置**（改完点「应用并重建容器」） |
| 给代理单独一个 IP | **工具 → 地址设置** |
| 每天自动更新 | **工具 → 定时自动更新**（默认 04:00） |
| 按规则集精细分流 | **工具 → 规则集分流** |
| 更新脚本到最新版 | **工具 → 程序文件** |
| 更新面板网页 | **工具 → 节点面板界面** |
| 升级 mihomo 内核 | 管理台「**运行状态**」→ 有新版本会显示「有新版 vX.Y.Z」+「**一键更新内核**」（自动拉最新镜像并重建内核容器，约 1~2 分钟，配置/线路不丢） |
| 备份 / 回滚 | **工具 → 备份** |
| 出问题先自检 | **状态 → 健康自检**（7 项） |

**TikTok / AI**：`TikTok`、`AI` 两个业务组默认走 `狮城自动`（香港上不了 TikTok），都能手动改。

---

## 八、验证清单（真实实测）

以下是在 **DS920+ / DSM 7.4.1-90080** 上跑通后的实测输出（IP 已脱敏为 `192.168.1.100`）。
照着自己机器上的结果对一遍，全对就说明部署成功：

```bash
$ docker ps --format '{{.Names}} | {{.Status}} | {{.Ports}}' | grep mihomo
mihomo       | Up 8 minutes | (host 网络；7890 代理 / 9090 面板 直听)
mihomo-admin | Up 32 seconds |

$ docker exec mihomo /mihomo -t -d /root/.config/mihomo -f /root/.config/mihomo/config.yaml
configuration file /root/.config/mihomo/config.yaml test is successful

$ curl -s -x http://192.168.1.100:7890 https://api.ipify.org        # 代理真的出网
47.238.134.22

$ curl -s -H 'Authorization: Bearer <管理台密码>' http://192.168.1.100:9090/version
{"meta":true,"version":"v1.19.34"}

$ curl -s -o /dev/null -w '%{http_code}' http://192.168.1.100:9090/ui/
200

$ curl -s -c /tmp/ck -d 'pass=<管理台密码>' http://192.168.1.100:9090/login
{"ok": true, "msg": "登录成功"}
$ curl -s -b /tmp/ck http://192.168.1.100:9090/api/health
{"ok": true, "passed": 7, "total": 7, ...}      # 7 项自检全过
```

管理台里的实际状态：

| 项 | 实测值 |
|---|---|
| 代理提供者 | 机场 \| 类型 HTTP \| 节点数 **31** |
| 策略组 | Selector **20**（含内置 GLOBAL，即 19 个自建）、URLTest **6**、Fallback **3** |
| TikTok 当前选择 | 狮城自动 |
| AI 当前选择 | 狮城自动 |

---

## 九、端口对照表

| 用途 | 宿主端口 | 容器端口 | 地址 | 备注 |
|---|---|---|---|---|
| 代理 | **7890** | — | `群晖IP:7890` | host 网络，HTTP / SOCKS5 同一个口 |
| 节点面板 | **9090** | — | `http://群晖IP:9090/ui/` | host 网络，密钥 = 管理台密码 |
| 网页管理台 | **9090** | host 网络 | `http://群晖IP:9090` | 登录密码 = 管理台密码 |


---

## 十、常见问题

**1. 提示找不到 docker**

先去套件中心把 **Container Manager** 装上再跑脚本。

**2. 下载脚本失败 / 卡住**

GitHub 直连不稳时多试两次；也可以把仓库文件下到本地，用离线方式：

```bash
# 把文件放到同一个目录后
SRC_DIR=/volume1/docker/mihomo-src bash mihomo-install.sh
```

**3. 面板打开要填「密钥」，填什么？**

地址填 `http://群晖IP:9090`，**密钥填管理台密码**（就是 `ADMIN_PASS`）。

**4. 「代理提供者」显示 0？**

还没加机场，或管理台没生成过配置。去 **机场** 页添加订阅并**保存**（会自动更新配置）。

**5. 容器一直重启？**

多半是配置为空：新装第一次管理台会自动写引导配置，**等 10 秒**左右就稳定。
一直不停就看日志，并确认 `mihomo-admin` 在运行、`etc` 目录不是一直空的。

**6. 改了端口，进不去了**

用**新端口**访问。建议管理台端口保持 `9090`（**节点面板和它同一个端口**），一般只需要改代理端口；内核接口端口（默认 9091）保持默认即可，它只绑本机。
若管理台容器里没有 docker 命令，按页面给出的 SSH 命令执行：

```bash
sudo sh /volume1/docker/mihomo/settings-apply.sh
```

**7. Container Manager 里项目/容器点不动，报「No such container」**

容器被重建后 DSM 自己的记录会变乱，跑一下：

```bash
sudo sh /volume1/docker/mihomo/dsm-fix.sh
```

然后**重新打开** Container Manager 页面。SSH 安装时脚本已建 `/etc/cron.d/mh-dsmfix`
**每 10 分钟自动清理**，也可以手动建一个任务计划每天跑。

**8. 镜像拉不动怎么办？**

**一键脚本默认从 Docker Hub 拉内核（`metacubex/mihomo:latest`），国内可直接拉；脚本还带兜底：Docker Hub 不通时自动改用 `ghcr.io` 并打标签。**

如果你手工把 compose 改成了 `ghcr.io/metacubex/mihomo:latest`，可能报：

```
Head "https://ghcr.io/v2/metacubex/mihomo/manifests/latest": denied
```

这是 **上游 `ghcr.io/metacubex/mihomo` 这个包拒绝匿名拉取**（与你的网络无关）。换回 Docker Hub 即可：

```bash
sudo /usr/local/bin/docker pull metacubex/mihomo:latest
```

如果连 Docker Hub 也不通（少见），在能上网的机器上拉好再搬过来：

```bash
docker pull metacubex/mihomo:latest && docker save metacubex/mihomo:latest -o mihomo.tar
# 拷到群晖后
sudo /usr/local/bin/docker load -i mihomo.tar
```

**9. 手工放了面板文件，面板却不生效**

面板由**管理台**自己提供（`/ui/`），不再依赖内核的 `external-ui`，所以重装面板不用重启内核；
若是**手工**把面板文件丢进 `etc/ui/`，要自己重启内核：

```bash
sudo /usr/local/bin/docker restart mihomo
```

**10. 加了机场但没节点？**

先看「状态 → 健康自检」，再点「立即更新配置」看弹窗里的中文报错。
常见是订阅链接过期，或机场给的是网页地址而不是订阅地址。
规则集来自七尺宇 `qichiyuhub/rule` 与 MetaCubeX `meta-rules-dat`，**只按 URL 引用**。

**11. 会不会影响群晖本身和别的设备？**

本仓库**默认就是旁路由**：会开 TUN、接管「网关指向群晖」的设备的流量。想回到「只开一个代理端口、不改 DNS、不改路由」的模式，把 `settings.json` 里的 `tun_enable` 改成 `false` 后重新生成配置即可（见 [旁路由模式.md](旁路由模式.md)）。
群晖自己的上网方式和其他设备都不受影响。要用代理的设备自己填地址。

**12. 换了群晖的 IP（或搬到另一台机器），会不会起不来？**

**不会。** 这套**没有把 IP 写进配置**：

- 用的是 **host 网络**，端口由配置决定（7890 / 9090），不存在「把 IP 写进端口映射」这回事；
- 管理台的 `admin_bind` / `proxy_ip` **留空**，它会自己识别宿主地址；
- 所以**改 IP、加第二个 IP、换机器，都不用改任何文件**。

> NAS 一改 IP，容器就要去绑一个**已经不存在的地址**，于是起不来。
> 如果你遇到这种情况：**不指定 `SB_IP` 重跑一次它的安装脚本**（脚本会自动识别新 IP 并重写端口），
> 或者干脆把端口那两行的 IP 前缀删掉、改成 `- "2180:2080"`。

---

## 十一、卸载

```bash
sudo -i
cd /volume1/docker/mihomo
docker compose -p mihomo down
cd / && rm -rf /volume1/docker/mihomo
rm -f /usr/local/etc/rc.d/mhip.sh /etc/cron.d/mh-dsmfix
```

---

## 致谢

| 项目 | 用途 |
|---|---|
| [MetaCubeX/mihomo](https://github.com/MetaCubeX/mihomo) | 代理内核（Clash.Meta） |
| [MetaCubeX/metacubexd](https://github.com/MetaCubeX/metacubexd) | 节点面板界面 |
| [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) | 规则集与 geo 数据（mrs） |
| **七尺宇 · [qichiyuhub/rule](https://github.com/qichiyuhub/rule)** | 规则集分流（`rules/proxy.list`），**只按 URL 引用，不搬运、不再分发** |

---

## 免责声明

- 本项目**仅供个人学习、研究与自用**，请遵守你所在地区的法律法规以及所用服务的条款。
- 请勿用于任何非法用途；因使用本项目产生的任何后果由使用者自行承担。
- 本项目**不提供、不内置、不分发任何订阅、节点或机场信息**。
- 规则集、面板等第三方资源均来自上游开源项目，其可用性与合法性以对应项目为准。

[MIT](LICENSE) © 2026 amw1933


---

## 附：旁路由模式（设备只填网关就全局走代理）

本仓库**默认就是旁路由版**：内核走 host 网络 + TUN。在路由器里把设备的**网关**指向群晖 IP，
设备就自动全局走代理 —— 电视盒子、游戏机这类不能填代理的设备也能用（DNS 不用改，会被内核接管）。

| 项目 | 说明 |
|---|---|
| 端口 | **7890** 代理、**9090** 唯一 Web 端口（管理台 + 面板 + 内核反代）、**9091** 内核接口（只绑 127.0.0.1）（host 网络下由配置决定，不再是端口映射） |
| 开关 | `settings.json` 的 `tun_enable`：`true` = 旁路由（默认），`false` = 只开代理端口 |
| 直连例外 | `direct_domains`（逗号分隔，例如 `example.com,myvps.net`），让订阅/自建服务别绕代理 |
| 生效 | 改完在管理台点「**立即更新配置**」（或重启内核容器） |
| 回退 | `tun_enable` 改回 `false` → 立刻变回「只提供代理端口」的普通模式 |

**路由器侧**（以 iKuai 为例）：网络设置 → DHCP 设置 → DHCP 静态分配 → 编辑设备 →
**网关**填群晖 IP（例如 `192.168.1.100`），**DNS 不用动**。

⚠️ 三个要注意的：

1. **设备会依赖群晖在线**：群晖关机/重启，这些设备就上不了网（旁路由的固有代价）
2. **群晖自身的流量也会走代理**（TUN 的正常表现）；不想让某些服务走代理就加进 `direct_domains`
3. **一个 IP 只能一套做旁路由**：两套都开 TUN 会互相抢路由和 DNS

完整原理、配置片段、常见坑见 **[旁路由模式.md](旁路由模式.md)**。


### 单端口架构（对外只有 `9090`）

| 路径 | 内容 |
|---|---|
| `/` | 网页管理台（改密码 / 加机场 / 加节点 / 更新配置 / 健康自检） |
| `/ui/` | 节点面板 metacubexd —— **开箱即用**：装好直接打开就是面板主页，不用填地址/密钥 |
| `/clash/` | 内核接口反代（面板自动调用，同样受密钥保护） |

- **`9091`** 是内核控制接口，只绑 `127.0.0.1`，**不对外**；所以路由器/防火墙只需要放通**一个 9090**
- **零配置**：打开 `/ui/` 直接进面板（管理台会按登录会话自动代填密钥）。万一出现设置页：地址填 `http://群晖IP:9090/clash`、**密钥留空**
- `7890` 仍是代理端口（App 里手动填代理时才用；旁路由模式下设备不需要它）
