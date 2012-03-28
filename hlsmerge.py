#!/usr/bin/python
try:
    import signal
    from signal import SIGPIPE, SIG_IGN
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except ImportError:
    pass

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

import sys
import pycurl
import re
import pprint
import os
import subprocess
from subprocess import CalledProcessError
from urlparse import urljoin,urlparse
from optparse import OptionParser

def curl_multi(urls,max=5):
    num_urls = len(urls)
    num_conn = min(max, num_urls)
    m = pycurl.CurlMulti()
    m.handles = []
    for i in range(num_conn):
        c = pycurl.Curl()
        c.fp = None
        c.setopt(pycurl.FOLLOWLOCATION, 1)
        c.setopt(pycurl.MAXREDIRS, 5)
        c.setopt(pycurl.CONNECTTIMEOUT, 30)
        c.setopt(pycurl.TIMEOUT, 300)
        c.setopt(pycurl.NOSIGNAL, 1)
        m.handles.append(c)
    # Main loop
    freelist = m.handles[:]
    num_processed = 0
    while num_processed < num_urls:
        # If there is an url to process and a free curl object, add to multi stack
        while urls and freelist:
            url = urls.pop(0)
            c = freelist.pop()
            c.fp = open(url['file'], "wb")
            c.setopt(pycurl.URL, url['url'])
            c.setopt(pycurl.WRITEDATA, c.fp)
            m.add_handle(c)
            # store some info
            c.filename = url['file']
            c.url = url['url']
        # Run the internal curl state machine for the multi stack
        while 1:
            ret, num_handles = m.perform()
            if ret != pycurl.E_CALL_MULTI_PERFORM:
                break
        # Check for curl objects which have terminated, and add them to the freelist
        while 1:
            num_q, ok_list, err_list = m.info_read()
            for c in ok_list:
                c.fp.close()
                c.fp = None
                m.remove_handle(c)
                print "Success:", c.filename, c.url
                freelist.append(c)
            for c, errno, errmsg in err_list:
                c.fp.close()
                c.fp = None
                m.remove_handle(c)
                print "Failed: ", c.filename, c.url, errno, errmsg
                freelist.append(c)
            num_processed = num_processed + len(ok_list) + len(err_list)
            if num_q == 0:
                break
        # Currently no more I/O is pending, could do something in the meantime
        # (display a progress bar, etc.).
        # We just call select() to sleep until some more data is available.
        m.select(1.0)
    
    
    # Cleanup
    for c in m.handles:
        if c.fp is not None:
            c.fp.close()
            c.fp = None
        c.close()
    m.close()
    


def curl_cat(url):
    b = StringIO()
    c = pycurl.Curl()
    c.fp=b
    c.setopt(pycurl.URL,url)
    c.setopt(pycurl.WRITEFUNCTION,b.write)
    c.perform()
    b.seek(0)
    contents=b.getvalue()
    b.close()
    return contents

def parse_extm3u(string):
    lines=[]
    for line in re.split("[\r\n]+",string):
        if line=="": continue
        lines.append(line)
    
    if lines[0]!="#EXTM3U":
        raise Exception("doesn't look like an m3u playlist?")
    else:
        del lines[0]
    
    items=[]
    for i in range(0,len(lines)/2):
        item={}
        x=re.search("PROGRAM-ID=(\d+)",lines[i*2])
        if x:
            item['pid']=x.group(1)
        else:
            item['pid']=1
        
        x=re.search("BANDWIDTH=(\d+)",lines[i*2])
        if x:
            item['bandwidth']=int(x.group(1))
        else:
            item['bandwidth']=1
        
        item['playlist']=lines[(i*2)+1]
        items.append(item)
    return items


#print curl_cat("http://www.whatismyip.org")

parser = OptionParser()
parser.add_option("-p","--playlist",dest="playlist")
parser.add_option("-i","--program-id",dest="pid")
parser.add_option("-b","--bandwidth",dest="bandwidth")
parser.add_option("-s","--scratch",dest="scratch")
parser.add_option("-c","--connections",dest="connections",default=5)

(options, args) = parser.parse_args()

if options.scratch is None:
    raise Exception("scratch dir is a required option")
elif not os.path.isdir(options.scratch):
    os.makedirs(options.scratch)

if options.playlist is None:
    raise Exception("playlist is a required option")

playlist=curl_cat(options.playlist)
playlist=parse_extm3u(playlist)

bestbw={}
pids={}
for item in playlist:
    try:
        if item['bandwidth']>bestbw[item['pid']]:
            bestbw[item['pid']]=item['bandwidth']
    except KeyError:
        bestbw[item['pid']]=item['bandwidth']
    try:
        pids[item['pid']]+=1
    except KeyError:
        pids[item['pid']]=1


if len(pids)>1 and options.pid is None:
    pprint.pprint(pids)
    raise Exception("multiple pids -- specify one with -p")
elif options.pid is not None:
    pid=options.pid
else:
    pid=pids.keys()[0]

if options.bandwidth is None:
    bandwidth=bestbw[pid]
else:
    bandwidth=int(options.bandwidth)

nextlist=None
for item in playlist:
    if item['bandwidth']==bandwidth and item['pid']==pid:
        nextlist=item['playlist']

if nextlist is None:
    raise Exception('failed to find matching playlist item')
playlisturl=urljoin(options.playlist,nextlist)
playlist=curl_cat(playlisturl)
segments=[]
dsegments=[]
for line in re.split("[\r\n]+",playlist):
    if line=="": continue
    if re.match("#",line): continue
    file="%s/%s"%(options.scratch,os.path.basename(line))
    segment={
            'url':urljoin(playlisturl,line),
            'file':file
            }
    segments.append(segment)
    if os.path.isfile(file): continue
    dsegments.append(segment)
curl_multi(dsegments)

tsfile="%s/combined.ts"%options.scratch
if not os.path.isfile(tsfile):
    tshandle=open(tsfile,"w",-1)
    tshandle.seek(0)
    for segment in segments:
        print segment['file']
        segmenthandle=open(segment['file'],"r",-1)
        while 1:
            buffer=segmenthandle.read()
            if len(buffer)==0: break
            tshandle.write(buffer)
        segmenthandle.close()
    tshandle.close()

# oh ffmpeg
try:
    ident=subprocess.check_output(["ffmpeg","-i",tsfile],stderr=subprocess.STDOUT)
except CalledProcessError as e:
    ident=e.output
    pass
x=re.search("(\d+\.\d+) tbr",ident);
fps=x.group(1)
print "FPS %s"%fps

print "extracting audio"
audio="%s/audio.aac"%options.scratch
if not os.path.isfile(audio):
    try:
        eaudio=subprocess.check_output(["ffmpeg","-y","-i",tsfile,"-acodec","copy",audio],stderr=subprocess.STDOUT)
    except CalledProcessError as e:
        print e.output

print "extracting video"
video="%s/video.h264"%options.scratch
if not os.path.isfile(video):
    try:
        evideo=subprocess.check_output(["ffmpeg","-y","-i",tsfile,"-vcodec","copy",video],stderr=subprocess.STDOUT)
    except CalledProcessError as e:
        print e.output

mkv="%s/final.mkv"%options.scratch
if not os.path.isfile(mkv):
    try:
        evideo=subprocess.check_output(["mkvmerge","-o",mkv,"--default-duration","0:%sfps"%fps,'-A',video,'-D',audio],stderr=subprocess.STDOUT)
    except CalledProcessError as e:
        print e.output