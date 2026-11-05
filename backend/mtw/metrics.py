"""Linux /proc samples. CPU excludes waited-for children; RSS is never summed."""
import os
from pathlib import Path

HZ=os.sysconf('SC_CLK_TCK')
PAGE=os.sysconf('SC_PAGE_SIZE')


def role_for(words,root_role,is_root,local_pid):
    python=bool(words and Path(words[0]).name.startswith('python'))
    if python and 'mtw.service' in words and 'compute-3' in words:return 'compute'
    if python and 'mtw.worker' in words:return 'worker'
    if words and words[0].endswith('/mono_export'):return 'native'
    if words and words[0].endswith(('/ffmpeg','/ffprobe')):return 'decoder'
    if is_root:return root_role
    if root_role=='container_runtime':return 'container_init' if local_pid==1 else 'container_helper'
    return root_role+'_child'


def process_sample(pid,proc=Path('/proc')):
    path=proc/str(pid)
    try:
        fields=(path/'stat').read_text().rsplit(')',1)[1].split()
        status=(path/'status').read_text().splitlines()
        nspid=next((line.split()[1:] for line in status if line.startswith('NSpid:')),[])
        return {'host_pid':int(pid),'ppid':int(fields[1]),'state':fields[0],
            'start_ticks':int(fields[19]),'cpu_ticks':int(fields[11])+int(fields[12]),
            'rss_bytes':int(fields[21])*PAGE,'pid':int(nspid[-1]) if nspid else None,
            'pid_namespace':os.readlink(path/'ns/pid'),
            '_argv':(path/'cmdline').read_bytes().split(b'\0')}
    except (OSError,ValueError,IndexError): return None


def system_sample(proc=Path('/proc')):
    result={'cpu_count':os.cpu_count(),'cpu_percent':None,'memory_total_bytes':None,
        'memory_available_bytes':None,'swap_total_bytes':None,'swap_free_bytes':None,'errors':[]}
    ticks=None;uptime=None
    try:
        cpu=(proc/'stat').read_text().splitlines()[0].split()[1:9]
        values=list(map(int,cpu));ticks=(sum(values),values[3]+values[4])
    except (OSError,ValueError,IndexError): result['errors'].append('cpu_unavailable')
    try: uptime=float((proc/'uptime').read_text().split()[0])
    except (OSError,ValueError,IndexError): result['errors'].append('uptime_unavailable')
    try:
        memory={line.split(':')[0]:int(line.split()[1])*1024 for line in (proc/'meminfo').read_text().splitlines() if len(line.split())>=2}
        for key,name in [('memory_total_bytes','MemTotal'),('memory_available_bytes','MemAvailable'),('swap_total_bytes','SwapTotal'),('swap_free_bytes','SwapFree')]:
            result[key]=memory.get(name)
    except (OSError,ValueError): result['errors'].append('memory_unavailable')
    return result,ticks,uptime


class Rates:
    def __init__(self): self.previous={};self.system=None;self.at=None

    def apply(self,processes,system,ticks,uptime,at):
        elapsed=at-self.at if self.at is not None else 0
        if elapsed>0 and ticks and self.system:
            total=ticks[0]-self.system[0];idle=ticks[1]-self.system[1]
            if total>0 and idle>=0: system['cpu_percent']=round(max(0,min(100,100*(total-idle)/total)),2)
        current={}
        for p in processes:
            key=(p['host_pid'],p['start_ticks']);previous=self.previous.get(key)
            p['cpu_percent']=round(100*(p['cpu_ticks']-previous)/HZ/elapsed,2) if previous is not None and elapsed>0 and p['cpu_ticks']>=previous else None
            p['cpu_seconds']=p['cpu_ticks']/HZ
            p['elapsed_seconds']=round(max(0,uptime-p['start_ticks']/HZ),2) if uptime is not None else None
            p['metrics_status']='available' if p['cpu_percent'] is not None else 'warming_up'
            current[key]=p['cpu_ticks']
        self.previous,self.system,self.at=current,ticks,at
