# 音源调查

当前服务接入网易云、QQ、酷我、酷狗、咪咕。添加音源需要验证搜索、完整音频、权限、格式和资源占用，不能只根据项目的支持列表判断可用性。

## 当前酷狗、咪咕适配器

`providers/kugou.py`、`providers/migu.py` 随本仓库维护，调用平台公开接口；没有加载或执行落雪的自定义音源脚本。取链方式可以在所附的 `0xHJK/music-dl` 旧适配器中追溯，咪咕榜单编号参考了 LX Music 的榜单实现。

- [music-dl](https://github.com/0xHJK/music-dl)：本项目保留了适配器快照和 MIT 声明。
- [LX Music 数据来源说明](https://github.com/lyswhut/lx-music-desktop#readme)：平台元数据与自定义音频链接来源是两回事。替换一个搜索或榜单适配器，并不保证音频取链就能恢复。

## 候选

| 音源 | 可参考的原始实现 / 官方文档 | 接入要点 |
| --- | --- | --- |
| Bilibili | [Listen 1](https://github.com/listen1/listen1_chrome_extension)、[yt-dlp 的 Bilibili 提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py) | 视频音轨与传统音乐曲库不同：标题、UP 主不能直接当成歌曲、歌手；需处理分 P、独立音轨、短期链接及访问限制。优先作为独立搜索源评估，不直接并入自动歌单或同曲匹配。 |
| Jamendo | [官方 API](https://developer.jamendo.com/v3.0)、[下载接口](https://developer.jamendo.com/v3.0/tracks/file) | 需要开发者 Client ID；下载必须检查 `audiodownload_allowed`，不能把可播放当作允许下载。 |
| Audius | [官方 API 文档](https://docs.audius.co/api/) | 有搜索、播放和下载接口；需按当前 API 认证与曲目权限接入，曲库与现有五平台不同。 |

Bilibili 可以只取音频，不必缓存视频。若后续接入，应使用单任务、流式写盘和现有 FFmpeg 预算，继续在 180 MB 容器限制下验证。以上候选尚未加入 MusicHub 的正式五源配置。
