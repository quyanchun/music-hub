"""Qualify public Bilibili AU audio using the API flow in yt-dlp/Listen1."""
import argparse,datetime,hashlib,json,os,subprocess,threading,time
from pathlib import Path
from urllib.parse import urlsplit
import requests
if not __debug__: raise SystemExit("Run without -O; validation assertions must be enabled.")
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',default=os.environ.get('PROBE_OUTPUT','.local/source-qualification/bilibili-au'))
ROOT=Path(parser.parse_args().output);ROOT.mkdir(parents=True,exist_ok=True)
MAX_FILE=40*1024*1024
HEADERS={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36','Referer':'https://www.bilibili.com/'}
SESSION=requests.Session();SESSION.headers.update(HEADERS)
next_request=0
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
def api(path,params):
 global next_request
 time.sleep(max(0,next_request-time.monotonic()));next_request=time.monotonic()+3
 with SESSION.get('https://www.bilibili.com/audio/music-service-c/web/'+path,params=params,timeout=(5,12),stream=True) as response:
  response.raise_for_status();data=json.loads(response.raw.read(2*1024*1024,decode_content=True))
 return data

def download(url,path,expected,seconds):
 assert urlsplit(url).scheme in ('http','https') and (urlsplit(url).hostname or '').endswith('.bilivideo.com')
 sha=hashlib.sha256();md5=hashlib.md5();received=0
 if path.is_file():
  with SESSION.head(url,headers={'Accept-Encoding':'identity'},timeout=(5,15),allow_redirects=True) as response:
   response.raise_for_status();length=int(response.headers.get('Content-Length') or 0)
  assert length>0 and path.stat().st_size==length,'cached body differs from CDN Content-Length'
  with path.open('rb') as stream:
   while chunk:=stream.read(65536):
    received+=len(chunk);sha.update(chunk);md5.update(chunk)
 else:
  with SESSION.get(url,headers={'Accept-Encoding':'identity'},timeout=(5,20),stream=True) as response:
   response.raise_for_status();length=int(response.headers.get('Content-Length') or 0)
   assert 0<length<=MAX_FILE,'missing or oversized CDN length'
   with path.open('wb') as output:
    for chunk in response.iter_content(65536):
     received+=len(chunk);assert received<=MAX_FILE
     output.write(chunk);sha.update(chunk);md5.update(chunk)
 assert 100000<received<=MAX_FILE and received==length,'incomplete audio body'
 probe=subprocess.run(['ffprobe','-v','error','-threads','1','-show_entries','stream=codec_type,codec_name,bit_rate:stream_disposition=attached_pic:format=duration,format_name','-of','json',str(path)],capture_output=True,text=True,timeout=20,check=True)
 info=json.loads(probe.stdout);duration=float(info['format']['duration']);streams=info.get('streams',[])
 assert any(s.get('codec_type')=='audio' for s in streams)
 assert not any(s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic') for s in streams)
 assert abs(duration-seconds)<=max(3,seconds*.03)
 decode=subprocess.run(['ffmpeg','-v','error','-nostdin','-threads','1','-filter_threads','1','-i',str(path),'-map','0:a:0','-vn','-f','null','-'],capture_output=True,text=True,timeout=180)
 assert decode.returncode==0 and not decode.stderr.strip(),decode.stderr[:300]
 return {'bytes':received,'http_content_length':length,'api_size_field':expected,'api_size_matches_file':expected==received,'sha256':sha.hexdigest(),'md5':md5.hexdigest(),'duration':duration,'declared_duration':seconds,'codecs':[s.get('codec_name') for s in streams],'full_decode':True,'path':path.name}

report={'source':'bilibili_au','checked_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'upstream':['https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py','https://github.com/listen1/listen1_chrome_extension/blob/master/js/provider/bilibili.js'],'tracks':[],'qualified':False}
try:
 album=api('song/of-menu',{'sid':'10624','pn':1,'ps':5});assert album['code']==0
 items=album['data']['data'];report['catalog']={'playlist_id':10624,'count':album['data']['totalSize'],'sample_ids':[s['id'] for s in items]}
 assert any(s['id']==2445151 for s in items)
 for sid in (1003142,2445151):
  report['stage']='metadata '+str(sid)
  info=api('song/info',{'sid':str(sid)});assert info['code']==0 and info['data']['id']==sid
  track=info['data'];assert not track.get('limit') and track['duration']>60
  media=api('url',{'sid':str(sid)});assert media['code']==0 and media['data']['sid']==sid
  data=media['data'];url=data['cdns'][0]
  report['stage']='audio verification '+str(sid)
  result={'id':sid,'title':track['title'],'artist':track.get('author') or track['uname'],'audio_host':urlsplit(url).hostname,**download(url,ROOT/('bilibili-au-'+str(sid)+'.m4a'),int(data['size']),track['duration'])}
  report['tracks'].append(result);print(json.dumps(result,ensure_ascii=True),flush=True)
 negative=api('url',{'sid':'999999999'});report['negative_control']={'code':negative.get('code'),'has_audio':bool((negative.get('data') or {}).get('cdns'))}
 assert negative.get('code')!=0 and not report['negative_control']['has_audio']
 assert len({t['sha256'] for t in report['tracks']})==2
 report['qualified']=True;report['stage']='complete'
except Exception as error:
 report['error']={'type':type(error).__name__,'detail':str(error)[:500],'stage':report.get('stage')}
finally:
 stop.set();report['memory']=memory();report['memory_peaks']=peaks if report['memory'] else None
 (ROOT/'bilibili-au-qualification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'qualified':report['qualified'],'error':report.get('error'),'memory_peaks':peaks},ensure_ascii=True),flush=True)

raise SystemExit(0 if report['qualified'] else 1)
