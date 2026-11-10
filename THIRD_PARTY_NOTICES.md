# 第三方代码与服务

MusicHub 的 Subsonic 服务、曲库、搜索、公开歌单和发现池代码采用根目录的 MIT 许可证。

`providers/vendor/` 包含 [0xHJK/music-dl](https://github.com/0xHJK/music-dl) 的适配器快照，保留源文件中的作者信息及
[`providers/vendor/LICENSE`](providers/vendor/LICENSE) 的完整 MIT 版权声明。
快照清单保存在 [`providers/snapshot.json`](providers/snapshot.json)；后续对初始化、超时和权限判断的修改以 Git 历史为准。
`providers/` 的五音源适配器来自本项目早期音乐站代码，随本仓库一起维护。

运行依赖包括 Python、Requests、Click、PyCryptodome、PrettyTable、Mutagen 及 FFmpeg。
各依赖仍适用其自身许可证，Docker 镜像通过官方 Python 基础镜像、Debian 软件源及 PyPI 安装这些依赖。

网易云音乐、QQ 音乐、酷我音乐、酷狗音乐和咪咕音乐的名称、元数据、封面、歌词与音频
属于相应平台或权利人。本仓库不分发音乐文件，不代表这些平台，也不授予任何音乐内容的使用权。
