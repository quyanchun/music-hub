<div align="center">

# 🎵 MusicHub · 音乐汇

**多音源音乐服务，用你喜欢的播放器听歌。**

网易云 · QQ 音乐 · 酷我 · 酷狗 · 咪咕 · B站音频区

[Docker 一键部署](#docker-一键部署) · [连接播放器](#连接播放器) · [功能与限制](#功能与限制) · [日常管理](#日常管理)

![Docker](https://github.com/quyanchun/music-hub/actions/workflows/docker.yml/badge.svg)
![Subsonic](https://img.shields.io/badge/Subsonic-1.16.1-3975EA)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

MusicHub 是一个自托管音乐服务，通过 **Subsonic 1.16.1 兼容 API**，把多个音源的榜单、公开歌单和搜索结果提供给音流等播放器。服务运行在 NAS、服务器或电脑的 Docker 中，手机只需填写地址和服务账号。

**仅支持平台允许免费收听的完整歌曲，不支持会员及付费歌曲，不解锁或绕过会员权限。** 使用匿名公开接口，不需要配置平台 Cookie。歌曲是否可播仍受地区、平台权限、网络和接口状态影响。

## 使用效果

<p align="center">
  <img src="docs/screenshots/subsonic-client.jpg" alt="音流通过 Subsonic 连接 MusicHub，浏览多音源歌单并播放音乐" width="360" />
</p>

截图为客户端连接后的效果。MusicHub 本身提供 API，没有独立网页播放器；歌曲数量和公开歌单会随后台采集、权限检查与平台状态变化。

## 工作方式

```text
网易云 / QQ / 酷我 / 酷狗 / 咪咕 / B站AU
                ↓
  MusicHub：采集、搜索、权限过滤、按需缓存
                ↓  Subsonic API
       音流等兼容 Subsonic 的客户端
```

| 项目 | 说明 |
| --- | --- |
| 客户端服务类型 | **Subsonic**，不要选择 Navidrome |
| 协议 | Subsonic API 1.16.1；支持 JSON / XML、GET / 表单 POST |
| 认证 | 服务独立账号；支持密码和 `md5(password + salt)` token |
| 默认端口 | `8766` |
| API 路径 | `/rest/`，通常由客户端自动补全 |
| OpenSubsonic 扩展 | 仅声明已实现的 `songLyrics` 第 1 版 |
| 持久化 | SQLite、账号、收藏和播放记录保存在 Docker 数据卷 |

## Docker 一键部署

需要已安装 Docker。预构建镜像支持 **Linux amd64 / arm64**，适用于常见 x86 NAS、云服务器和 ARM64 设备。macOS / Windows 可使用 Docker Desktop。

复制下面整段命令执行：

```bash
docker run -d \
  --name music-hub \
  --restart unless-stopped \
  --init \
  -p 8766:8766 \
  -v music-hub-data:/experiment/data \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m,mode=1777 \
  --memory 180000000 --memory-swap 180000000 \
  --pids-limit 128 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --log-opt max-size=5m --log-opt max-file=2 \
  -e TZ=Asia/Shanghai \
  ghcr.io/quyanchun/music-hub:latest
```

首次运行会自动拉取镜像、创建数据卷并生成服务账号。查看账号：

```bash
docker exec music-hub cat /experiment/data/credentials.json
```

输出中的 `username` 和 `password` 填到客户端中。这是 MusicHub 的账号，与 NAS 系统账号、音乐平台账号无关。请保存好这份凭据，不要上传到 GitHub。

查看启动状态：

```bash
docker ps --filter name=music-hub
curl http://127.0.0.1:8766/health
```

健康接口返回 `{"service":"music-hub","status":"ok"}` 即说明 API 已启动。首次采集在后台进行，健康检查通过时曲库可能仍为空，稍后在客户端刷新即可。

### Docker Compose

```bash
git clone https://github.com/quyanchun/music-hub.git
cd music-hub
docker compose up -d
docker compose exec music-hub cat /experiment/data/credentials.json
```

Compose 会使用相同的预构建镜像，自动创建持久化数据卷。可在 `compose.yaml` 同目录新建 `.env` 调整绑定地址或端口：

```dotenv
# 本机反向代理使用 127.0.0.1；默认 0.0.0.0 允许局域网访问。
MUSICHUB_BIND=0.0.0.0
MUSICHUB_PORT=8766
# 自动缓存下载间隔，单位秒，最低60秒。
MUSICHUB_CACHE_INTERVAL=60
```

例如端口已被占用，将 `MUSICHUB_PORT` 改为 `18766` 后重新执行 `docker compose up -d`，客户端地址也改用 `18766`。

### 从源码构建

不方便拉取 GHCR 镜像时，可下载源码本地构建。Dockerfile 直接使用官方 Python 基础镜像，无需其他私有镜像或本项目早期的音乐站服务。

```bash
git clone https://github.com/quyanchun/music-hub.git
cd music-hub
docker compose up -d --build
```

构建过程需要能访问 Docker Hub、Debian 软件源和 PyPI。

## 连接播放器

以 [音流](https://music.aqzscn.cn/docs/services/) 为例：

1. 添加音乐服务，类型选择 **Subsonic**。
2. 服务器地址填写 `http://你的服务器IP:8766`。只填到端口，不追加 `/rest/`。
3. 用户名、密码填写上面 `credentials.json` 中的值。
4. 保存并登录，在歌曲、专辑、歌手、歌单或搜索页浏览音乐。

| 访问场景 | 地址示例 |
| --- | --- |
| 与客户端在同一台电脑 | `http://127.0.0.1:8766` |
| 手机访问同一局域网的 NAS / 电脑 | `http://你的NAS或电脑IP:8766` |
| 域名与 HTTPS 反向代理 | `https://music.example.com` |

手机上的 `127.0.0.1` 指手机自身，请使用运行 Docker 的设备 IP。公网使用建议配置 HTTPS，并按需开放端口；MusicHub 不会自动修改防火墙或配置路由器转发。反向代理应把完整路径转发到 `http://127.0.0.1:8766`，在线搜索可能需要约 25 秒，建议请求超时至少 60 秒。

也可使用其他支持 Subsonic 的客户端，兼容性以实际接口为准。不要选 Navidrome 专有服务类型，MusicHub 没有实现它的登录 API。

## 功能与限制

| 功能 | 当前行为 |
| --- | --- |
| 搜索 | 合并本地曲库和五个平台在线结果；B站支持AU编号取歌、已采集AU曲库的本地关键词搜索；支持分页与按音源搜索 |
| 榜单 | 原五个平台共15张榜单，保留原排名；没有可展示曲目的榜单隐藏 |
| 公开歌单 | 包含B站AU推荐歌单；显示已缓存和未尝试曲目，隐藏会员、试听及确认失败的曲目 |
| 每日推荐 | 每天以北京时间03:00为日界线，从已校验缓存中选50首；批次存SQLite，同一天稳定，重启后保留 |
| 缓存歌曲歌单 | 在服务返回的歌单列表中固定排第一；只列出已完整校验的本地音频，Range / HEAD / 下载直接读服务器文件 |
| 浏览 | 歌曲、歌手、专辑、流派、音源文件夹 |
| 播放与下载 | 按需缓存，支持 HEAD、Range、拖动和断点读取 |
| 歌词与封面 | 同步歌词、封面、相似歌曲和热门歌曲 |
| 使用记录 | 收藏、评分和播放次数持久化，重启后保留 |

- 公开歌单每源最多选取 12 张，每张最多读取 300 首；匿名接口可能只返回部分歌曲。真正为空或所有曲目均确认失败的歌单不显示；新歌单尚未缓存时仍可浏览、点播。显示数量与详情中的实际曲目数一致。
- 榜单和公开歌单约每 6 小时刷新；发现池约每 2 小时补充。首次初始化需要一段时间，不会预下载全库音频。
- 新关键词搜索最长约 25 秒；某音源失败时返回其他音源与已有库内结果。云服务器可能被平台限流，接口适配不代表所有源始终可播。
- QQ 音频取链目前使用适配器中的第三方解析通道，最终音频地址限定为 QQ 自有域名；该通道变化也会影响可用性。
- 搜索结果缓存约 5 分钟；空关键词、`*` 和 `""` 浏览已收录曲库，不发起全网搜索。
- 歌单为只读，不支持创建 / 编辑个人歌单；当前为单账号服务，不支持多用户、服务端播放队列、转码和完整 Navidrome API。
- 每日推荐与下载任务独立：客户端首次访问当天推荐时，只从已完整校验的缓存中选50首，不触发下载或在线搜索。优先选近7天未推荐的歌曲，数量不足时允许复用近期歌曲；新批次凑齐50首后切换。缓存少于50首且从未有完整批次时只返回实际数量。
- 下载任务全天单线程运行，默认每1分钟安排一首，理想情况下约每小时60首、每天1440首；不再限制凌晨时段或每天100首。依次优先缓存公开歌单、榜单、其他已收录歌曲；多个歌单共用同一份音频。实际完成量受音源、播放和资源压力影响，不补追停机期间的欠量。
- 失败先尝试其他音源的同版本，仍失败则等下一轮选择其他歌曲；失败和重复内容也遵守配置的任务间隔，换源下载之间至少间隔5秒。下次任务时间和进度存SQLite，重启不会突发下载。单曲失败指数退避；遇403、429或平台频繁提示时，该音源自动下载先冷却30分钟。
- 自动下载按音源记录连续失败次数：连续失败3次后禁用到北京时间次日00:00；如原有限流冷却更晚结束，以较晚时间为准。次日再试时重新累计，成功新增缓存会清零该音源的失败次数，内存、播放及磁盘资源暂停不计入失败。状态存SQLite，重启不解除禁用；所有音源都不可用时，自动下载暂停等待恢复，不反复探测。
- 上述禁用仅针对自动下载（包括内部换源搜索），不限制客户端主动搜索、手动播放和其他曲库采集任务；不能保证平台永远不限制服务器IP。
- 缓存文件按「歌曲名 - 歌手名.扩展名」命名（如 `精卫 - 王悦辰.mp3`），可直接从数据卷的 `audio/` 目录拷贝出去使用。非法字符替换为 `·`，文件名按 UTF-8 截断到 120 字节，撞名时追加 `(2)` 序号。去重按规范化的歌曲名 + 歌手集合，同名同歌手跨音源只保留一份、不再比较时长；另以 SHA-256 检查重复内容。被容量淘汰的歌曲后台冷却7天。服务启动时会把历史 32 位 ID 文件名自动迁移为可读名，内部 ID、歌单和每日推荐批次不变。
- 缓存歌曲歌单使用专用本地播放ID；文件被淘汰或外部删除后，旧ID返回不可用，不会回源下载。普通搜索同时合并本地曲库与五个平台的在线结果，本地已缓存结果优先，关键词最多取500个本地候选，空关键词可分页浏览曲库。
- 手动点播与后台下载共用缓存、去重索引和20GB额度；下载并校验成功后，所有包含该曲目的歌单都会使用本地播放ID。未尝试的曲目继续显示；例如100首中75首成功、25首确认失败，就显示75首。失败曲目在后台遵守退避和音源禁用规则重试，恢复成功后重新显示。播放及内存压力导致的暂停不算音源失败。
- 推荐与相似歌曲基于公开歌单、元数据和库内记录，不是各平台登录后的私人每日推荐。

### 存储与资源

默认容器内存上限 **180 MB（约 171.7 MiB）**，不使用 swap；Docker 引擎本身另占资源。下载分块写盘，缓存索引和待办进度在SQLite中；后台在工作内存接近上限的75%时暂停，并为前台请求及ffprobe预留空间。一次只有一个音频下载和一个ffprobe，缓存歌单响应按条从数据库读并写入临时磁盘文件，不构造全库内存列表。忙时可能需要等待。

音频缓存总上限默认 **20 GB（20,000,000,000 字节）**，推荐、榜单、歌单和按需播放共用额度，临时下载预留空间。音频不再按24小时过期；容量不足时按缓存完成时间从旧到新淘汰，保护正在播放的文件、当日准备批次及仍在使用的上一批推荐。没有可安全删除的文件时暂停下载。新增音频需保留约3 GiB磁盘空间。封面仍按约64 MiB / 24小时维护，日志按5 MiB × 2轮转。镜像、数据库、封面和日志不计入音频额度，部署前请另留空间。

SQLite 主库上限 128 MiB；收藏、评分和播放记录保留。超过 30 天未使用且不属于本地缓存、榜单、发现池或用户记录的临时索引会清理，旧队列可能需要重新搜索。数据卷含账号和使用记录，应定期备份。

### 可选缓存配置

在 Compose 服务的 `environment` 中设置（`docker run` 使用 `-e`）：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `MUSICHUB_AUDIO_CACHE_BYTES` | `20000000000` | 所有音频文件的总缓存额度 |
| `MUSICHUB_PREFETCH_INTERVAL` | `10` | 后台循环间隔，单位秒，最低5秒 |
| `MUSICHUB_CACHE_INTERVAL` | `60` | 后台安排下一首歌曲的间隔秒数，最低60秒；可设为120、180等降低请求频率，修改后重建容器生效 |

`getScanStatus` 额外返回 `cachedSongCount` 和 `cachedBytes`，可用于检查任务进度。服务器启动及每30分钟分批核对缓存目录、索引和残留临时文件。

音频文件位于数据卷的 `audio/` 子目录（本机 compose 部署时示例为 `/disk1/音乐/subsonic/audio/`）。文件名即歌曲名加歌手名，可整目录拷贝到其它设备离线播放；直接改名或删除文件会导致对应曲目的本地 ID 失效，服务端会在下次核对时清理索引，播放时重新取流。客户端「下载」端点也使用同样的可读文件名。

B站AU已接入公开歌单、AU编号取歌、缓存和播放；普通关键词只搜索已采集的AU曲库，不调用受限的视频搜索接口。B站元数据请求至少间隔3秒，自动下载共用默认每1分钟一首的可配置队列及失败熔断。街声仍仅完成隔离实测，未接入正式服务。结果、限制和复跑方式见[音源调查](docs/source-options.md)。

## 日常管理

以下命令适用于上述 `docker run` 部署；Compose 部署可在源码目录使用 `docker compose logs`、`restart`、`stop` 等对应命令。

```bash
# 查看日志与资源
docker logs --tail 100 music-hub
docker stats --no-stream music-hub

# 重启 / 停止
docker restart music-hub
docker stop music-hub
```

### 更新

Compose 部署：

```bash
docker compose pull
docker compose up -d
```

`docker run` 部署：

```bash
docker pull ghcr.io/quyanchun/music-hub:latest
docker stop music-hub
docker rm music-hub
# 然后重新执行上面的 docker run 命令，继续使用 music-hub-data 数据卷。
```

更新前建议备份数据。删除容器不会删除命名数据卷；**不要执行 `docker compose down -v` 或删除数据卷**，否则账号、收藏和曲库会丢失。源码构建用户用 `git pull` 后执行 `docker compose up -d --build`。

本项目直接更新主分支及 `latest` 镜像，不依赖 GitHub Releases。镜像同时附带 `sha-提交号` 标签，便于固定构建。

### 备份

`docker run` 部署可以先停止服务，再复制完整数据目录，确保 SQLite 与 WAL 处于一致状态：

```bash
docker stop music-hub
docker cp music-hub:/experiment/data ./music-hub-backup
docker start music-hub
```

备份目录含密码，请妥善保存。恢复时停止服务，把备份内容放回原数据卷，并确保目录可由容器用户 `10001:10001` 读写。

## 开发与验证

需要 Python 3.12+ 和 FFmpeg（包括 `ffprobe`）：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -p 'test_*.py'
python service.py --host 127.0.0.1 --port 8766 --data ./data
```

单元测试使用隔离临时目录和模拟平台响应。真实部署可在容器内运行：

```bash
docker exec music-hub python /experiment/verify_live.py
# 额外验证各源真实播放（受上游网络、地区和平台权限影响）
docker exec music-hub python /experiment/verify_live.py --playback
```

验证脚本从私有数据目录读取账号，不打印密码；首次采集尚未完成或后台正在改变曲库时，可等待稳定后再验证。GitHub Actions 在 amd64 / arm64 上运行测试，并在主分支检查通过后构建和推送双架构镜像。

## 项目来源与许可证

MusicHub 从早期独立音乐服务实验整理而来，服务端不依赖其他项目源码。音乐平台适配器包含 [music-dl](https://github.com/0xHJK/music-dl) 的代码快照，保留上游作者信息和 MIT 许可证，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。[Folia 音乐站](https://github.com/quyanchun/folia-free) 是同一音源方向的桌面播放器项目，两者可以分别运行。

项目代码采用 [MIT](LICENSE) 许可证。音乐内容版权属于各平台及权利人，请遵守相关服务条款和内容使用要求。
