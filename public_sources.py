"""Anonymous editorial playlists, using the bundled Folia catalog's permission rules."""
import ast
import html
import json
import re

import requests


def fetch(url, params=None, source='netease'):
    referer = {'qq': 'https://y.qq.com/', 'kuwo': 'https://www.kuwo.cn/',
               'migu': 'https://m.music.migu.cn/'}.get(source, 'https://music.163.com/')
    with requests.get(url, params=params, headers={'User-Agent': 'Mozilla/5.0', 'Referer': referer},
                      timeout=(3, 7), stream=True) as response:
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_content(32768):
            raw.extend(chunk)
            if len(raw) > 6 * 1024 * 1024:
                raise ValueError('playlist_response_too_large')
    try:
        body = json.loads(raw)
    except ValueError:
        body = ast.literal_eval(raw.decode('utf-8'))
    if not isinstance(body, dict) or body.get('code') not in (None, 0, 200, '000000'):
        raise ValueError('playlist_unavailable')
    if body.get('subcode', 0) != 0 or body.get('errcode', 0) != 0:
        raise ValueError('playlist_restricted')
    return body


def clean(value):
    return html.unescape(str(value or '')).strip()


def collection(source, identifier, name, count=0):
    return {'source': source, 'id': str(identifier), 'name': clean(name), 'count': int(count or 0)}


def list_playlists(source):
    if source == 'netease':
        body = fetch('https://music.163.com/api/playlist/list',
                     {'cat': '全部', 'order': 'hot', 'limit': 12, 'offset': 0})
        return [collection(source, r['id'], r['name'], r.get('trackCount')) for r in body.get('playlists') or []][:12]
    if source == 'qq':
        body = fetch('https://u.y.qq.com/cgi-bin/musicu.fcg', {'data': json.dumps({
            'comm': {'ct': 24, 'cv': 0}, 'req': {'module': 'music.playlist.PlaylistSquare',
            'method': 'GetRecommendFeed', 'param': {'From': 0, 'Size': 12}}})}, source)
        reply = body.get('req') or {}
        if reply.get('code') != 0:
            raise ValueError('qq_playlist_unavailable')
        rows = [r.get('Playlist', {}).get('basic', {}) for r in reply.get('data', {}).get('List') or []]
        return [collection(source, r['tid'], r['title'], r.get('song_cnt')) for r in rows if r.get('tid')][:12]
    if source == 'kugou':
        body = fetch('https://m.kugou.com/plist/index', {'json': 'true'}, source)
        rows = body.get('plist', {}).get('list', {}).get('info') or []
        return [collection(source, r['specialid'], r['specialname'], r.get('songcount')) for r in rows][:12]
    if source == 'kuwo':
        body = fetch('https://wapi.kuwo.cn/api/pc/classify/playlist/getRcmPlayList',
                     {'loginUid': 0, 'loginSid': 0, 'appUid': 76039576, 'pn': 1, 'rn': 12, 'order': 'hot'}, source)
        return [collection(source, r['id'], r['name'], r.get('total')) for r in body.get('data', {}).get('data') or []][:12]
    if source == 'migu':
        body = fetch('https://app.c.nf.migu.cn/pc/bmw/page-data/playlist-square-recommend/v1.0',
                     {'templateVersion': 2, 'pageNo': 1}, source)
        rows, seen = [], set()

        def visit(node):
            if isinstance(node, dict):
                key = str(node.get('resId') or '')
                if str(node.get('resType')) == '2021' and key.isdigit() and key not in seen:
                    seen.add(key)
                    rows.append(collection(source, key, node.get('txt')))
                for item in node.values():
                    visit(item)
            elif isinstance(node, list):
                for item in node:
                    visit(item)
        visit(body.get('data'))
        return rows[:12]
    raise ValueError('unsupported_playlist_source')


def song(source, key, title, singer, album, duration, free=True):
    return {'source': source, 'id': str(key), 'title': clean(title), 'singer': clean(singer),
            'album': clean(album), 'duration_seconds': max(0, int(duration or 0)),
            'playable': free, 'downloadable': free}


def playlist_tracks(source, key, offset, limit=30):
    limited = False
    if source == 'netease':
        body = fetch('https://music.163.com/api/playlist/detail', {'id': key})
        listing = body.get('result') or body.get('playlist')
        if not isinstance(listing, dict) or not isinstance(listing.get('tracks'), list):
            raise ValueError('playlist_detail_unavailable')
        rows = listing['tracks']
        total = len(rows)
        limited = int(listing.get('trackCount') or total) > total
        songs = [song(source, r['id'], r['name'], '、'.join(a['name'] for a in r.get('artists', r.get('ar', []))),
                      (r.get('album') or r.get('al') or {}).get('name'),
                      (r.get('duration') or r.get('dt') or 0) // 1000, r.get('fee') not in (1, 4))
                 for r in rows[offset:offset + limit]]
    elif source == 'qq':
        body = fetch('https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg',
                     {'disstid': key, 'format': 'json', 'type': 1, 'json': 1, 'utf8': 1, 'onlysong': 0,
                      'g_tk': 5381, 'loginUin': 0, 'hostUin': 0, 'platform': 'yqq', 'needNewCode': 0}, source)
        rows = (body.get('cdlist') or [{}])[0].get('songlist')
        if not isinstance(rows, list):
            raise ValueError('qq_playlist_detail_unavailable')
        total = len(rows)
        songs = [song(source, r.get('songmid') or r.get('mid'), r.get('songname') or r.get('title'),
                      '、'.join(a['name'] for a in r.get('singer') or []), r.get('albumname') or r.get('album', {}).get('name'),
                      r.get('interval'), int(r.get('pay', {}).get('payplay') or r.get('pay', {}).get('pay_play') or 0) == 0)
                 for r in rows[offset:offset + limit]]
    elif source == 'kuwo':
        body = fetch('https://nplserver.kuwo.cn/pl.svc', {'op': 'getlistinfo', 'pid': key, 'pn': offset // limit,
                     'rn': limit, 'encode': 'utf8', 'keyset': 'pl2012', 'identity': 'kuwo', 'pcmp4': 1,
                     'vipver': 'MUSIC_9.0.5.0_W1', 'newver': 1}, source)
        rows = body.get('musiclist')
        if not isinstance(rows, list):
            raise ValueError('kuwo_playlist_detail_unavailable')
        total = int(body.get('total') or len(rows))
        songs = [song(source, re.sub(r'^MUSIC_', '', str(r.get('id') or r.get('rid') or r.get('musicrid'))),
                      r.get('name') or r.get('songname'), r.get('artist'), r.get('album'), r.get('duration') or r.get('song_duration'),
                      str(r.get('online', '1')) != '0' and not any(str((r.get('payInfo') or {}).get(f, '0')) == '1'
                      for f in ('cannotOnlinePlay', 'listen_fragment'))) for r in rows]
    elif source == 'kugou':
        body = fetch('http://mobilecdn.kugou.com/api/v3/special/song',
                     {'specialid': key, 'page': offset // limit + 1, 'pagesize': limit}, source)
        rows = body.get('data', {}).get('info')
        if not isinstance(rows, list):
            raise ValueError('kugou_playlist_detail_unavailable')
        total = int(body['data'].get('total') or len(rows))
        songs = []
        for r in rows:
            name = clean(r.get('filename') or r.get('name'))
            singer, _, title = name.partition(' - ')
            item = song(source, r.get('hash'), r.get('songname') or title or name,
                        r.get('singername') or singer, r.get('album_name'), r.get('duration'), int(r.get('pay_type') or 0) == 0)
            item['hash'] = item['id']
            songs.append(item)
    elif source == 'migu':
        body = fetch('https://app.c.nf.migu.cn/MIGUM3.0/resource/playlist/song/v2.0',
                     {'playlistId': key, 'pageNo': offset // limit + 1, 'pageSize': limit}, source)
        data = body.get('data') or {}
        rows = data.get('songList')
        if not isinstance(rows, list):
            raise ValueError('migu_playlist_detail_unavailable')
        total = int(data.get('totalCount') or len(rows))
        songs = []
        for r in rows:
            if not r.get('contentId'):
                continue
            pq = next((a for a in r.get('audioFormats') or [] if a.get('formatType') == 'PQ'), {})
            songs.append(song(source, r['contentId'], r.get('songName'), r.get('singer') or '、'.join(
                a.get('name', '') for a in r.get('singerList') or []), r.get('album'), r.get('duration'),
                'vip' not in (pq.get('showTags') or r.get('showTags') or [])))
    else:
        raise ValueError('unsupported_playlist_source')
    return {'songs': songs, 'total': total, 'limited': limited, 'has_more': offset + limit < total}
