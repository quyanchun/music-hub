"""Verify the daily cached recommendation and local-only playlist contract."""
import hashlib
import json
from pathlib import Path
import secrets
from urllib.parse import urlencode
from urllib.request import Request,urlopen


def main():
    account=json.loads(Path('/experiment/data/credentials.json').read_text())
    def request(endpoint,extra=None,headers=None):
        salt=secrets.token_hex(8)
        params={'u':account['username'],'t':hashlib.md5((account['password']+salt).encode()).hexdigest(),
                's':salt,'v':'1.16.1','c':'daily-cache-verifier','f':'json',**(extra or {})}
        with urlopen(Request('http://127.0.0.1:8766/rest/'+endpoint+'?'+urlencode(params),headers=headers or {}),timeout=60) as response:
            return response.status,response.read()
    def api(endpoint,extra=None):
        body=json.loads(request(endpoint,extra)[1])['subsonic-response']
        assert body['status']=='ok',endpoint
        return body
    first=api('getRandomSongs',{'size':50})['randomSongs']['song']
    second=api('getRandomSongs',{'size':50})['randomSongs']['song']
    assert len(first)<=50 and all(s['id'].startswith('cache-') for s in first)
    if len(first)==50: assert [s['id'] for s in first]==[s['id'] for s in second]
    playlist=api('getPlaylist',{'id':'cached-songs'})['playlist']
    assert all(s['id'].startswith('cache-') for s in playlist['entry'])
    audio=[]
    for song in first[:3]:
        status,head=request('stream',{'id':song['id']},{'Range':'bytes=0-1023'})
        assert status==206 and len(head)==1024
        audio.append({'id':song['id'],'range':True})
    report={'version':api('ping')['serverVersion'],'ready':len(first),'batch_complete':len(first)==50,
            'cached_playlist':len(playlist['entry']),'daily_stable':len(first)==50,'audio':audio,
            'note':'Incomplete initial batches continue in the background; playback uses local-only IDs.'}
    Path('/experiment/data/daily-cache-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
