# 群晖 Container Manager 部署教程（mihomo + 中文网页管理台）

全程**不碰命令行**，只用 File Station + Container Manager 点鼠标。
（只有第 5 步「加机场 / 更新配置」开始才进网页管理台）


---

## 一、前提

1. **DSM 7.2 或更高**
2. 套件中心安装 **Container Manager**（旧名 Docker），打开一次确认能启动
3. 知道**群晖的 IP**（就是你访问 DSM 用的地址，例如 `192.168.1.100`）
4. 有一个**机场订阅链接**（Clash 格式，没有也行，装好后在管理台里加）

---

## 二、建文件夹（这一步要做）

打开 **File Station**，进到 `docker` 共享文件夹（没有就先新建一个共享文件夹叫 `docker`），
在里面新建文件夹 **`mihomo`**，再在 `mihomo` 里建一个子文件夹 **`etc`**：

```
docker/
└── mihomo/
    └── etc/        ← mihomo 配置目录（管理台自动往里写配置，不要手改）
```

> **为什么必须建 `etc`**：compose 里 `./etc` 是内核配置目录的挂载点。
> DSM 上的 Docker **不会**自动创建不存在的挂载目录，缺了它启动时报
> `Bind mount failed: '…/mihomo/etc' does not exist`。

> 最终路径是 `/volume1/docker/mihomo`。若 `docker` 共享文件夹不在 volume1，选项目路径时对应改。

> ⚠️ 别"为了省掉建文件夹"改成挂载项目文件夹本身（挂 `./` + `-d …/etc`）——
> 那样会让内核的 `external-ui` / SAFE_PATHS 检查报错、启动即崩（踩过这个坑）。就用上面这种写法 ✓

> 除了 `etc`，其余东西都不用准备：`settings.json` / `config.yaml` / GeoIP 数据 / 节点面板
> 都由管理台**首次启动自动生成或下载** ✓

> 最终路径：`/volume1/docker/mihomo`。若你的 `docker` 共享文件夹不在 volume1，选项目路径时对应改。

---

## 三、镜像地址 ✅

管理台不用你上传任何文件——它已经打包成镜像发在 GHCR 上，compose 直接拉现成的。

| 组件 | 镜像 | 容器名 |
|---|---|---|
| mihomo 内核 | `metacubex/mihomo:latest`（上游在 Docker Hub 的官方发布） | `mihomo` |
| 网页管理台 | `ghcr.io/amw1933/synology-mihomo-web:latest` | `mihomo-admin` |

> 内核走 **Docker Hub**、管理台走 **GHCR**，两个都是公开镜像，项目第一次启动会自动拉取。
> ⚠️ 注意：`ghcr.io/metacubex/mihomo` 这个地址**匿名拉取会被上游拒绝**（报 `denied`），**别用那个**，详见常见问题第 8 条。

---

## 四、新建项目（Container Manager）

1. 打开 **Container Manager** → 左边选 **项目** → 点 **新增**
2. 项目名称：`mihomo`
3. 路径：选 `/docker/mihomo`（就是刚才建的文件夹）
4. 来源：选 **创建 docker-compose.yml**（或「直接输入」）
5. 把 **本仓库根目录的 [docker-compose.yml](docker-compose.yml)** 内容**整段粘进去** —— **一个字都不用改**：

```yaml
services:
  mihomo:
    image: metacubex/mihomo:latest          # Docker Hub 官方发布，国内可直接拉
    container_name: ${CT_MH:-mihomo}
    restart: unless-stopped
    network_mode: host                      # 旁路由必需：TUN 要在宿主网络里接管流量
    cap_add:
      - NET_ADMIN                           # 建 TUN / 改路由需要
    devices:
      - /dev/net/tun:/dev/net/tun
    volumes:
      - ./etc:/root/.config/mihomo          # 相对路径，跟着项目文件夹走
    command: ["-d", "/root/.config/mihomo"]

  mihomo-admin:
    image: ghcr.io/amw1933/synology-mihomo-web:latest
    container_name: ${CT_ADMIN:-mihomo-admin}
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./:/data                            # 整个项目文件夹 = 管理台的数据目录
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - ADMIN_PASS=${ADMIN_PASS:-admin}     # 默认 admin，登录后请立刻改掉
      - ADMIN_PORT=${ADMIN_PORT:-9090}
      - ADMIN_BIND=${ADMIN_BIND:-0.0.0.0}
      - PROXY_PORT=${PROXY_PORT:-7890}      # host 网络下 = 配置里的代理端口
      - PANEL_PORT=${PANEL_PORT:-9090}
      - TUN_ENABLE=1                        # 生成带 TUN 的旁路由配置
```

> **为什么一个字都不用改**：端口绑 `0.0.0.0`（不是某个具体 IP），管理台会自己识别宿主地址。
> 想改端口 / 密码 / 容器名：在同目录放一个 `.env`（照 [.env.example](.env.example) 写），compose 文件本身不用动。
> **换台群晖（别的机器）**：同一份文件直接粘，什么都不用改。

> **为什么用 `./` 相对路径**：项目文件夹叫什么都行，compose 里不用出现 `/volume1/docker/xxx`，避免「项目建在 `mihomo-1`、容器却挂在 `mihomo` 目录」。

6. 点 **下一步** → **完成**

Container Manager 会自动**拉取两个镜像并启动** `mihomo` 和 `mihomo-admin` 两个容器。
**等两个容器都显示「运行中」**，再往下走。

> ⚠️ `mihomo-admin` 头一分钟可能会**重启一两次**：它在自动生成引导配置，属正常现象。
> 如果**一直**重启不停，看常见问题第 4 条。

> 📷 截图占位：Container Manager 项目页里两个容器「运行中」

---

## 五、打开管理台，把功能补齐

浏览器打开：

```
http://群晖IP:9090
```

输入密码（**默认 `admin`**，页面顶部会红字提醒你改，请登录后立刻到「工具」页改掉），然后做这几件事：

### 1. 添加机场（**会自动更新配置**）

**机场** → 粘贴订阅链接（Clash 格式）→ **添加机场**

保存后**自动开始更新配置**，页面会弹窗提示「正在更新配置…」→
十几秒后变成「✅ 更新完毕」，不用再去「状态」页点按钮。

有多个机场就一个一个加，**第 1 个是主订阅**（决定 DNS / 规则 / 入口）。
机场会生成独立的 `proxy-providers`，换机场只动一处，所有策略组自动跟着变。

> **「代理提供者」显示 0？** 见常见问题第 2 条。

### 2. 安装节点面板网页

节点面板**自动装好**：直接开 `http://群晖IP:9090/ui/`（零配置，不用填地址/密钥）。想重装走 **工具 → 节点面板界面**。

> ⚠️ mihomo 只在启动时注册 `external-ui`，所以安装成功后管理台会**自动重启一次内核容器**才生效，稍等十几秒。
> 若你是手工把面板文件丢进 `etc/ui/`，要自己重启内核（见常见问题第 9 条）。

### 3. 建议：开启定时自动更新与规则集分流

**工具** → **定时自动更新** → 开启（默认每天 04:00 自动同步机场订阅）。

**工具** → **规则集分流** → 勾上 → **保存**，再回「状态」点一次「立即更新配置」。

开启后会自动按规则集分流（规则集只按 URL 引用上游，不搬运）：AI / 油管 / 谷歌 / GitHub /
电报 / TikTok / Netflix / 微软 / 苹果 各走对应策略组，淘宝 / 百度 / 国内镜像走**直连**。
其中 `TikTok`、`AI` 两个业务组默认走 `狮城自动`（香港上不了 TikTok），其余业务组默认走各自优先地区的「故转」。

地区三件套：香港 / 日本 / 新加坡 / 美国 / 台湾 / 韩国，每个地区自动生成
**手动(select) + 故转(fallback) + 自动(url-test)** 三个策略组。

> 📷 截图占位：管理台「状态」页自检全过、策略组列表

---

## 六、客户端怎么填

| 类型 | 地址 | 端口 |
|---|---|---|
| HTTP 代理 | 群晖IP | **7890** |
| SOCKS5 代理 | 群晖IP | **7890** |

同一个端口同时支持 HTTP 和 SOCKS5，手机电脑填这个就行。

---

## 七、日常维护（都在网页里）

| 想做的事 | 去哪 |
|---|---|
| 换订阅 / 加机场 | **机场** |
| 手动加节点 | **节点**（粘贴 `vmess`/`vless`/`ss`/`trojan`/`hysteria2` 等分享链接） |
| 某网站走直连或走代理 | **规则**（下拉选 DIRECT 或策略组） |
| 换端口 / 地址 | **工具 → 端口设置 / 地址设置**（改完点「应用并重建容器」） |
| 更新程序文件 / 面板 / 备份 | **工具 → 程序文件 / 节点面板界面 / 备份** |
| 升级 mihomo 内核 | 管理台「**运行状态**」→ 有新版本会显示「有新版 vX.Y.Z」+「**一键更新内核**」（自动拉最新镜像并重建内核容器，约 1~2 分钟，配置/线路不丢） |
| 出问题先自检 | **状态 → 健康自检**（7 项） |

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

对照检查：

1. **两个容器都 Up**（host 网络：7890 代理 / 9090 面板 / 9090 管理台）
2. **配置校验**输出 `test is successful`
3. **代理出网**返回一个公网 IP（不是你家宽带的 IP）
4. **面板 `/ui/`** 返回 `200`；**管理台自检** `passed` = `total` = 7

---

## 九、端口对照表

| 用途 | 宿主端口 | 容器端口 | 地址 | 备注 |
|---|---|---|---|---|
| 代理 | **7890** | — | `群晖IP:7890` | host 网络，HTTP / SOCKS5 同一个口 |
| 节点面板 | **9090** | — | `http://群晖IP:9090/ui/` | host 网络，密钥 = 管理台密码 |
| 网页管理台 | **9090** | host 网络 | `http://群晖IP:9090` | 登录密码 = 管理台密码 |


---

## 十、常见问题

### 1. 面板打开要填「连接到后端 / 密钥」，填什么？

- 地址填 `http://群晖IP:9090`
- **密钥填管理台密码**（就是 compose 里的 `ADMIN_PASS`）

### 2. 「代理提供者」显示 0？

说明还没加机场，或管理台没生成过配置。去 **机场** 页添加订阅并**保存**即可（会自动更新配置）。

### 3. 容器一直重启？

通常是配置为空：新装第一次管理台会自动写一份引导配置，**等 10 秒**左右 `mihomo` 就会稳定。
若一直不停，看 `mihomo-admin` 是否在运行、`etc` 目录是不是一直空的，再到日志里找原因。

### 4. 改端口后连不上？

在 **工具 → 端口设置** 改完，要等它**「应用并重建容器」**，然后用**新端口**访问。
若管理台容器里没有 docker 命令，按页面给出的 SSH 命令执行：

```bash
sudo sh /volume1/docker/mihomo/settings-apply.sh
```

### 5. Container Manager 里点容器报「No such container」？

这是 DSM 自己的记录被弄脏了：容器被**重建**（改端口、更新镜像等）之后，
DSM 会多出一条带十二位十六进制前缀的记录，把正常记录顶掉，界面就点不动了。

**一次修好：**

```bash
sudo sh /volume1/docker/mihomo/dsm-fix.sh
```

然后**关掉 Container Manager 页面重新打开**。SSH 安装时脚本已经建好
`/etc/cron.d/mh-dsmfix`，**每 10 分钟自动清理一次**，一般不用手动跑。

### 6. 节点面板里节点不显示？

机场订阅拉取失败（网络原因）。到管理台看该机场的「节点数 / 更新时间」，
或点「状态 → 立即更新配置」看弹窗里的中文报错。

### 7. 会不会影响群晖和别的设备？

本仓库**默认就是旁路由**：会开 TUN、接管「网关指向群晖」的设备的流量。想回到「只开一个代理端口、不改 DNS、不改路由」的模式，把 `settings.json` 里的 `tun_enable` 改成 `false` 后重新生成配置即可（见 [旁路由模式.md](旁路由模式.md)）。
群晖自己的上网方式和其他设备都不受影响。

### 8. 镜像拉不动怎么办？

**内核镜像默认就是 Docker Hub（`metacubex/mihomo:latest`），国内可以直接拉，正常不会有问题。**

如果你手工把它改成了 ghcr.io，可能报：

```
Head "https://ghcr.io/v2/metacubex/mihomo/manifests/latest": denied
```

这是 **上游 `ghcr.io/metacubex/mihomo` 这个包拒绝匿名拉取**（和你的网络无关，换 IP 也一样）。
换回 Docker Hub 即可：

```bash
sudo /usr/local/bin/docker pull metacubex/mihomo:latest
```

如果连 Docker Hub 也不通（少见），可以在**能上网的机器**上拉好再搬过来，或配置群晖的镜像加速：

```bash
# 在能拉通的机器上导出 / 导入
docker pull metacubex/mihomo:latest
docker save metacubex/mihomo:latest -o mihomo.tar
# 拷到群晖后
sudo /usr/local/bin/docker load -i mihomo.tar
```

> 管理台镜像 `ghcr.io/amw1933/synology-mihomo-web:latest` 是公开可拉的，不用管。

### 9. 手工放了面板文件，面板却不生效？

面板由**管理台**自己提供（`/ui/`），不再依赖内核的 `external-ui`，所以重装面板不用重启内核；
如果**手工**把面板文件丢进 `etc/ui/`，要自己重启内核才生效：

```bash
sudo /usr/local/bin/docker restart mihomo
```

### 10. 换了群晖 IP / 搬到另一台机器，要改什么吗？

**什么都不用改。** `docker-compose.yml` 里：

- 端口由**配置**决定（host 网络，7890 / 9090），不存在「把 IP 写进端口映射」这回事；
- 管理台 `ADMIN_BIND=${ADMIN_BIND:-0.0.0.0}`，地址由它自己识别；
- 所以**改 IP、加第二个 IP、换台群晖直接粘，都能跑**。

> NAS 一改 IP 就会因为绑不上地址而起不来 —— 重跑一次它的脚本让它重新识别 IP 即可。

---

## 十一、卸载

1. Container Manager → 项目 → 选中 `mihomo` → **停止** → **删除**（勾选删除镜像）
2. File Station 里删掉 `docker/mihomo` 文件夹
3. 如果建过任务计划 / cron，把 `dsm-fix` 相关任务删掉

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
