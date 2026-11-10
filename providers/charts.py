"""Paginated public charts. Preserve each platform's order and playback limits."""
import datetime
import re

import requests

PAGE_SIZE = 30
BOARDS = {
    'netease': {'hot': 3778678, 'new': 3779629, 'rising': 19723756},
    'kugou': {'hot': 8888, 'new': 74534, 'rising': 6666},
    'kuwo': {'hot': 16, 'new': 17, 'rising': 93},
    # LX Music desktop uses the same QQ Music topids (26/27/62).
    'qq': {'hot': 26, 'new': 27, 'rising': 62},
    # Migu bangids from LX Music musicSdk/mg/leaderboard.js. Migu has no
    # "soaring" board; the rising tab uses its 音乐风向榜 instead.
    'migu': {'hot': 27186466, 'new': 27553319, 'rising': 75959118},
}
QQ_CHART_LIMIT = 300


def duration(seconds):
    return str(datetime.timedelta(seconds=int(seconds or 0)))


def get_json(url, params):
    response = requests.get(url, params=params, timeout=8)
    response.raise_for_status()
    return response.json()


def get_chart(source, chart, page):
    start = (page - 1) * PAGE_SIZE
    board = BOARDS[source][chart]
    if source == 'netease':
        from music_dl.addons.netease import NeteaseApi
        response = NeteaseApi.request('https://music.163.com/api/playlist/detail',
                                     method='GET', data={'id': board})
        playlist = response.get('result') or response.get('playlist')
        if response.get('code') != 200 or not playlist or not playlist.get('tracks'):
            raise ValueError('榜单暂时不可用')
        total = int(playlist.get('trackCount') or len(playlist['tracks']))
        tracks = playlist['tracks'][start:start + PAGE_SIZE]
        if start < total and not tracks:
            raise ValueError('平台暂未提供后续曲目')
        urls = {}
        if tracks:
            rights = NeteaseApi.request('http://music.163.com/weapi/song/enhance/player/url',
                                       data=NeteaseApi.encrypted_request(
                                           {'ids': [s['id'] for s in tracks], 'br': 128000}))
            if rights.get('code') != 200 or not rights.get('data'):
                raise ValueError('暂时无法确认榜单播放权限')
            urls = {item['id']: item for item in rights['data']}
        songs = []
        for rank, track in enumerate(tracks, start + 1):
            audio = urls.get(track['id'], {})
            playable = bool(audio.get('url'))
            trial = bool(audio.get('freeTrialInfo'))
            availability = '' if playable else ('需会员' if track.get('fee') in (1, 4) else '暂不可用')
            seconds = int((track.get('duration') or track.get('dt') or 0) / 1000)
            songs.append({'source': source, 'id': track['id'], 'title': track['name'],
                          'singer': '、'.join(a['name'] for a in track.get('artists', track.get('ar', []))),
                          'album': (track.get('album') or track.get('al') or {}).get('name', ''),
                          'duration': duration(seconds), 'duration_seconds': seconds,
                          'hash': '', 'rank': rank, 'playable': playable,
                          'downloadable': playable and not trial,
                          'availability': '试听片段' if trial else availability})
        updated = playlist.get('updateTime')
        date = datetime.datetime.fromtimestamp(updated / 1000, datetime.timezone(datetime.timedelta(hours=8))).strftime('%m/%d') if updated else ''
        name = playlist['name']
    elif source == 'kugou':
        response = get_json('http://mobilecdnbj.kugou.com/api/v3/rank/song',
                            {'version': 9108, 'ranktype': 1, 'plat': 0, 'pagesize': PAGE_SIZE,
                             'area_code': 1, 'page': page, 'rankid': board,
                             'with_res_tag': 0, 'show_portrait_mv': 1})
        if response.get('errcode') != 0 or not response.get('data'):
            raise ValueError('榜单暂时不可用')
        listing = response['data']
        tracks = listing['info']
        total = int(listing['total'])
        name = {'hot': 'TOP500', 'new': '新歌榜', 'rising': '飙升榜'}[chart]
        published = (tracks[0].get('rank_id_publish_date') or tracks[0].get('addtime') or '') if tracks else ''
        date = published[5:10].replace('-', '/')
        songs = []
        for rank, track in enumerate(tracks, start + 1):
            singer = '、'.join(a.get('author_name', '') for a in track.get('authors', []))
            if not singer:
                singer = track.get('filename', '').partition(' - ')[0]
            # Keep the file hash as the id too: restored playback queues do not
            # carry the hash field, and kugou.download() needs it.
            file_hash = track['hash']
            songs.append({'source': source, 'id': file_hash,
                          'title': track.get('songname') or track.get('filename', '').partition(' - ')[2],
                          'singer': singer, 'album': '', 'hash': file_hash,
                          'duration': duration(track.get('duration')),
                          'duration_seconds': int(track.get('duration') or 0), 'rank': rank})
    elif source == 'qq':
        # QQ Music returns the whole board in one response (max 300 songs), so
        # pagination is applied locally. Metadata and audio availability use the
        # same rules as qq.search(); audio itself is resolved at download time.
        from qq import clean_text
        body = {
            'toplist': {
                'module': 'musicToplist.ToplistInfoServer',
                'method': 'GetDetail',
                'param': {'topid': board, 'num': QQ_CHART_LIMIT, 'period': ''},
            },
            'comm': {'uin': 0, 'format': 'json', 'ct': 20, 'cv': 1859},
        }
        response = requests.post('https://u.y.qq.com/cgi-bin/musicu.fcg', json=body,
                                 headers={'User-Agent': 'Mozilla/5.0',
                                          'Referer': 'https://y.qq.com/'}, timeout=12)
        response.raise_for_status()
        if len(response.content) > 4 * 1024 * 1024:
            raise ValueError('榜单响应异常')
        data = response.json()
        if data.get('code') != 0 or (data.get('toplist') or {}).get('code') != 0:
            raise ValueError('榜单暂时不可用')
        info = data['toplist']['data']
        tracks = info.get('songInfoList') or []
        meta = info.get('data') or {}
        total = min(int(meta.get('totalNum') or len(tracks)), len(tracks))
        names = {'hot': '热歌榜', 'new': '新歌榜', 'rising': '飙升榜'}
        name = str(meta.get('title') or names[chart])
        published = str(meta.get('updateTime') or meta.get('period') or '')
        date = published[5:10].replace('-', '/') if len(published) >= 10 else ''
        songs = []
        for offset, track in enumerate(tracks[start:start + PAGE_SIZE]):
            songmid = track.get('mid') or ''
            if not re.fullmatch(r'[A-Za-z0-9]{6,32}', songmid):
                continue
            media = track.get('file') or {}
            pay = track.get('pay') or {}
            paid = int(pay.get('pay_play') or 0) != 0
            available = not paid and int(media.get('size_128mp3') or 0) > 0
            seconds = int(track.get('interval') or 0)
            singers = [clean_text(s.get('name')) for s in track.get('singer', []) if s.get('name')]
            songs.append({'source': source, 'id': songmid,
                          'title': clean_text(track.get('title') or track.get('name')),
                          'singer': '、'.join(singers),
                          'album': clean_text((track.get('album') or {}).get('name')),
                          'duration': duration(seconds), 'duration_seconds': seconds,
                          'hash': '', 'rank': start + offset + 1,
                          'playable': available, 'downloadable': available,
                          'availability': '需会员' if paid else '' if available else '暂不可用'})
    elif source == 'migu':
        # The 2019 public API returns a whole column; paginate locally just
        # like the QQ board. VIP tracks are flagged by migu.chart_song() so
        # the player can disable them without a failed download attempt.
        from migu import BOARD_NAMES, chart_song, chart_tracks
        tracks = chart_tracks(board)
        total = len(tracks)
        name = BOARD_NAMES[chart]
        date = ''
        songs = []
        for offset, track in enumerate(tracks[start:start + PAGE_SIZE]):
            if not track.get('contentId'):
                continue
            songs.append(chart_song(track, start + offset + 1))
    else:
        from kuwo import clean_text
        response = get_json('https://kbangserver.kuwo.cn/ksong.s',
                            {'from': 'pc', 'fmt': 'json', 'pn': page - 1, 'rn': PAGE_SIZE,
                             'type': 'bang', 'data': 'content', 'id': board, 'show_copyright_off': 0})
        tracks = response['musiclist']
        total = int(response['num'])
        name = response['name']
        date = response.get('pub', '')[5:10].replace('-', '/')
        songs = []
        for rank, track in enumerate(tracks, start + 1):
            seconds = int(track.get('song_duration') or track.get('duration') or 0)
            songs.append({'source': source, 'id': track['id'], 'title': clean_text(track['name']),
                          'singer': clean_text(track.get('artist', '')),
                          'album': clean_text(track.get('album', '')), 'hash': '',
                          'duration': duration(seconds), 'duration_seconds': seconds, 'rank': rank})

    if start < total and not songs:
        raise ValueError('平台暂未提供后续曲目，请稍后再试')
    return {'songs': songs, 'failed': [], 'name': name, 'updated': date,
            'source': source, 'total': total, 'page': page,
            'has_more': bool(songs) and start + len(songs) < total}
