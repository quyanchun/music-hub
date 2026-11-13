"""Qualify public, non-preview StreetVoice CN playback; not the restricted original-file API."""
import argparse,datetime,hashlib,json,os,re,subprocess,threading,time
from pathlib import Path
from urllib.parse import urljoin,urlsplit
import requests
if not __debug__: raise SystemExit("Run without -O; validation assertions must be enabled.")
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',default=os.environ.get('PROBE_OUTPUT','.local/source-qualification/streetvoice'))
ROOT=Path(parser.parse_args().output);ROOT.mkdir(parents=True,exist_ok=True)
HEADERS={'User-Agent':'MusicHubSourceQualification/1.0','Referer':'https://www.streetvoice.cn/','x-requested-with':'XMLHttpRequest'}
SESSION=requests.Session();SESSION.headers.update(HEADERS);next_request=0;MAX_FILE=40*1024*1024
peaks={'working':0,'charged':0};stop=threading.Event()
def memory():
 for root,current,limit in (('/sys/fs/cgroup','memory.current','memory.max'),('/sys/fs/cgroup/memory','memory.usage_in_bytes','memory.limit_in_bytes')):
  try:
   p=Path(root);used=int((p/current).read_text());cap=int((p/limit).read_text());stat=dict(l.split() for l in (p/'memory.stat').read_text().splitlines());return {'limit':cap,'charged':used,'working':max(0,used-int(stat.get('inactive_file',stat.get('total_inactive_file',0))))}
  except (OSError,ValueError):pass
 return {}
def monitor():
 while not stop.wait(.1):
  state=memory()
  for key in peaks:peaks[key]=max(peaks[key],state.get(key,0))
threading.Thread(target=monitor,daemon=True).start()
def request(method,url,**kwargs):
 global next_request
 time.sleep(max(0,next_request-time.monotonic()));next_request=time.monotonic()+3
 return SESSION.request(method,url,timeout=(5,15),stream=True,**kwargs)
def api(method,url,**kwargs):
 with request(method,url,**kwargs) as response:
  response.raise_for_status();return json.loads(response.raw.read(2*1024*1024,decode_content=True))
def track(sid):
 meta=api('GET',f'https://www.streetvoice.cn/api/v5/song/{sid}/')
 assert meta['id']==sid and meta['public'] and meta['enable'] and not meta.get('is_blocked') and not meta.get('is_auditioning')
 assert 60<=meta['length']<=600
 media=api('POST',f'https://www.streetvoice.cn/api/v5/song/{sid}/hls/file/',data=b'')
 url=media['file'];assert urlsplit(url).hostname=='hls.streetvoice.cn'
 with request('GET',url) as response:
  response.raise_for_status();text=response.raw.read(256*1024,decode_content=True).decode()
 assert text.startswith('#EXTM3U') and '#EXT-X-ENDLIST' in text
 assert '#EXT-X-KEY' not in text and '#EXT-X-MAP' not in text and '#EXT-X-DISCONTINUITY' not in text
 segments=[urljoin(url,line.strip()) for line in text.splitlines() if line.strip() and not line.startswith('#')]
 assert 1<=len(segments)<=100 and all(urlsplit(s).hostname=='hls.streetvoice.cn' and urlsplit(s).path.endswith('.ts') for s in segments)
 seconds=sum(float(x) for x in re.findall(r'#EXTINF:([0-9.]+)',text));assert abs(seconds-meta['length'])<=max(3,meta['length']*.03)
 received=0;sha=hashlib.sha256();transport=ROOT/('streetvoice-'+str(sid)+'.ts');target=transport.with_suffix('.mp3')
 with transport.open('wb') as output:
  for n,segment in enumerate(segments):
   with request('GET',segment,headers={'Accept-Encoding':'identity'}) as response:
    response.raise_for_status();length=int(response.headers.get('Content-Length') or 0);part=0
    for chunk in response.iter_content(65536):
     part+=len(chunk);received+=len(chunk);assert received<=MAX_FILE
     output.write(chunk);sha.update(chunk)
    assert length>0 and part==length,'incomplete segment'
   if (n+1)%10==0:print(json.dumps({'id':sid,'segments_complete':n+1,'segments_total':len(segments)}),flush=True)
 remux=subprocess.run(['ffmpeg','-v','error','-nostdin','-threads','1','-i',str(transport),'-map','0:a:0','-vn','-c:a','copy','-y',str(target)],capture_output=True,text=True,timeout=60)
 assert remux.returncode==0,remux.stderr[:250]
 info=json.loads(subprocess.run(['ffprobe','-v','error','-threads','1','-show_entries','stream=codec_type,codec_name:format=duration','-of','json',str(target)],capture_output=True,text=True,timeout=20,check=True).stdout)
 duration=float(info['format']['duration']);assert abs(duration-meta['length'])<=max(3,meta['length']*.03)
 assert all(s.get('codec_type')=='audio' for s in info['streams'])
 decode=subprocess.run(['ffmpeg','-v','error','-nostdin','-threads','1','-filter_threads','1','-i',str(target),'-map','0:a:0','-f','null','-'],capture_output=True,text=True,timeout=180)
 assert decode.returncode==0 and not decode.stderr.strip(),decode.stderr[:250]
 return {'id':sid,'title':meta['name'],'artist':meta['user']['profile']['nickname'],'declared_duration':meta['length'],'playlist_duration':seconds,'duration':duration,'segments':len(segments),'source_bytes':received,'source_sha256':sha.hexdigest(),'file_bytes':target.stat().st_size,'full_decode':True,'audio_only':True,'path':target.name}
report={'source':'streetvoice_cn','checked_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'public full playback; original-file download permission is not assumed','upstream':'https://github.com/CharlesPikachu/musicdl/blob/master/musicdl/modules/sources/streetvoice.py','tracks':[],'qualified':False}
try:
 report['stage']='keyword search'
 with request('GET','https://www.streetvoice.cn/search/',params={'page':1,'q':'流浪','type':'song','_pjax':'#pjax-container'}) as response:
  response.raise_for_status();text=response.raw.read(2*1024*1024,decode_content=True).decode()
 ids=list(dict.fromkeys(int(i) for i in re.findall(r'data-id=["\'](\d+)["\']',text)));report['search']={'keyword':'流浪','ids':ids}
 assert 579807 in ids and 830458 in ids
 for sid in (579807,830458):
  report['stage']='full playback '+str(sid);result=track(sid);report['tracks'].append(result);print(json.dumps(result,ensure_ascii=True),flush=True)
 report['stage']='invalid ID'
 with request('GET','https://www.streetvoice.cn/api/v5/song/999999999/') as response:
  report['negative_control']={'http':response.status_code};assert response.status_code==404,'invalid song accepted'
 assert len({t['source_sha256'] for t in report['tracks']})==2
 report['qualified']=True;report['stage']='complete'
except Exception as error:report['error']={'type':type(error).__name__,'detail':str(error)[:300],'stage':report.get('stage')}
finally:
 stop.set();report['memory']=memory();report['memory_peaks']=peaks if report['memory'] else None
 (ROOT/'streetvoice-qualification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'qualified':report['qualified'],'error':report.get('error'),'memory_peaks':peaks}),flush=True)

raise SystemExit(0 if report['qualified'] else 1)
