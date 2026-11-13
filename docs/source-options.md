# 音源调查与实测

当前正式服务仍接入网易云、QQ、酷我、酷狗、咪咕。下面是额外音源的独立调查与验收，尚未加入正式服务配置。

## 已测通的链路（2026-11-13）

本机和服务器各验证两首不同曲目。服务器使用独立容器，内存硬上限180 MB、无 swap，未挂载正式曲库或账号。验证包含完整音频、实际文件长度、元数据时长、FFmpeg全曲解码、两首内容不同、无效编号不能返回默认音频，以及本机/服务器的源数据哈希一致。

| 音源 | 已测通的发现方式 | 样本 | 结果 |
| --- | --- | --- | --- |
| Bilibili AU 音频区 | AU编号、AM歌单；已读取歌单10624 | AU1003142《YELLOW》183秒、AU2445151《双星伴生》197秒 | 两首完整AAC/M4A文件均通过，本机与服务器文件SHA-256相同 |
| 街声中国站 | 关键词搜索；`流浪`返回真实歌曲 | 579807《流浪》245秒、830458《流浪台北》252秒 | 两首公开、非试听HLS音轨的全部25/26个分段通过；无转码封装MP3后全曲解码成功，两端源分段数据SHA-256相同 |

[完整验收数据](source-qualification-20261113.json)包含两端曲目、哈希、长度、时长、无效编号对照及隔离容器内存观测。内存观测属于独立源验收，不能代替将适配器接入正式服务后的并发压力测试。

### 接入时必须保留的区别

- **B站AU与视频音轨是不同链路。** 本次AU歌单和音频通过，视频信息及音频关键词搜索接口仍出现HTTP 412，不能将结果推广为“B站全站搜索/视频播放全部可用”。AU可先按编号或歌单接入。
- **AU的`size`字段不总是字节数。** 本次AU2445151返回168，实际完整文件为4,768,839字节。应以CDN Content-Length、实际落盘大小、时长和完整解码共同校验；不能直接据此字段判定文件截断。
- **街声验证的是公开播放链路。** 元数据须为公开、启用、非试听，播放列表须完整且无加密。本轮没有证明原文件下载接口的权限；普通原文件请求曾返回403，不能宣传为原文件/无损下载已测通。
- 街声样本为96 kbps公开播放音轨。封装使用`-c:a copy`，不做重新编码；HLS一首歌包含多个分段，单首任务间隔不等于HTTP请求间隔。本次分段串行获取，并保持至少3秒的请求间隔。

### 复跑

需要Python 3.12+、Requests，以及PATH中可用的FFmpeg/ffprobe；不需要安装整个第三方音乐下载器。

```bash
python tools/qualify_bilibili_audio.py --output .local/source-qualification/bilibili-au
python tools/qualify_streetvoice_audio.py --output .local/source-qualification/streetvoice
```

验证失败退出码为1，结果写入输出目录中的`*-qualification.json`。B站脚本可复用已有测试文件，但仍核对当前CDN长度并重新完整解码。街声脚本会重新读取公开播放分段。音频文件保留在本地输出目录，不纳入Git。

原始实现参考：

- [yt-dlp BilibiliAudio](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)
- [Listen 1 Bilibili](https://github.com/listen1/listen1_chrome_extension/blob/master/js/provider/bilibili.js)
- [musicdl StreetVoice](https://github.com/CharlesPikachu/musicdl/blob/master/musicdl/modules/sources/streetvoice.py)

## 其他已探测的候选

| 候选 | 本次证据 | 结论 |
| --- | --- | --- |
| [ccMixter / musicdl](https://github.com/CharlesPikachu/musicdl/blob/master/musicdl/modules/sources/ccmixter.py) | 关键词搜索成功；一首280秒CC-BY 4.0 MP3在两端完整下载、平台SHA-1、文件长度和全曲解码均通过，下载较慢 | 有单曲成功证据，但本轮未完成双曲资格验收，不与上表两路等同 |
| [Audius](https://docs.audius.co/api/) | 两端搜索成功；抽样曲目存在下载关闭或下载门槛 | 尚未证明适合本项目的离线缓存；不能据此判定整个平台不可用 |
| [Jamendo](https://developer.jamendo.com/v3.0) | 网站接口返回403；官方API需要开发者Client ID，并需检查`audiodownload_allowed` | 未测通完整链路，不使用其他人的凭据 |
| [Internet Archive](https://github.com/jjjake/internetarchive) | 本机连接被拒绝，服务器连接失败 | 本轮环境未测通，不代表全球服务不可用 |

## 当前酷狗、咪咕适配器来源

`providers/kugou.py`、`providers/migu.py` 随本仓库维护，调用平台公开接口；没有加载或执行落雪的自定义音源脚本。取链方式可以在所附的[0xHJK/music-dl](https://github.com/0xHJK/music-dl)旧适配器中追溯，咪咕榜单编号参考了LX Music的榜单实现。

[LX Music数据来源说明](https://github.com/lyswhut/lx-music-desktop#readme)区分平台元数据与自定义音频链接来源。替换一个搜索或榜单适配器，不保证音频取链就能恢复。上述实测均为时间点证据，不承诺接口永久可用。
