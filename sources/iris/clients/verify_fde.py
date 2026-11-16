#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pure offline FDE1 verifier. No devices, transports, subprocesses or writes.
verify_bytes takes complete bounded bytes and trusted expected bindings.
verify_file reads at most MAX+1 bytes; wire data never supplies local paths.
A matching envelope is content consistency, never hardware authentication.
"""
import hashlib
import json
import os
import stat
import struct
HEADER=288
FRAME=24576
TRACE_MAX=49152
MAX=HEADER+TRACE_MAX+3*16+73728+32
BINDINGS=('client_source','client_elf','kernel_image','stream_fir1','oracle_fnv1','final_record')
QBUF=3227014671
DQBUF=3227014673

def require(ok, reason):
    if not ok:
        raise ValueError(reason)

def unique(pairs):
    d={}
    for k,v in pairs:
        require(k not in d,'duplicate JSON key')
        d[k]=v
    return d

def trace_check(data, frames, stream):
    require(data.endswith(b'\n') and b'\x00' not in data,'trace termination')
    lines=data.splitlines()
    require(0<len(lines)<=4100,'trace row bound')
    rows=[]
    for line in lines:
        require(0<len(line)<2048,'trace line bound')
        r=json.loads(line,object_pairs_hook=unique,parse_constant=lambda x:require(False,'JSON constant'))
        require(type(r) is dict,'trace object')
        rows.append(r)
    calls=[r for r in rows if 'request' in r]
    require([r.get('seq') for r in calls]==list(range(1,len(calls)+1)),'ioctl sequence')
    require(calls and all(type(r.get('rc')) is int and r['rc']==0 and r.get('errno')==0 for r in calls),'ioctl failure')
    ts=[r.get('monotonic') for r in calls]
    require(all(type(t) in (float,int) and 0<=t<1e12 for t in ts) and ts==sorted(ts),'monotonic trace')
    require(ts[-1]-ts[0]<=30,'decode observation bound')
    kinds={}
    for r in rows:
        if 'kind' in r:
            require(r['kind'] not in kinds,'duplicate origin/hold')
            kinds[r['kind']]=r
    require(set(kinds)=={'opened_character_device','querycap','hold_request_external_stop_retain'},'origin/hold kinds')
    node=kinds['opened_character_device']
    require(type(node.get('major')) is int and node['major']>0 and type(node.get('minor')) is int and node['minor']>=0,'device origin')
    cap=kinds['querycap']
    require(bytes.fromhex(cap['driver_hex']).split(b'\0')[0]==b'iris_driver' and bytes.fromhex(cap['card_hex']).split(b'\0')[0]==b'Iris Decoder','querycap origin')
    caps=cap.get('device_caps') if cap.get('capabilities',0)&0x80000000 else cap.get('capabilities',0)
    require(type(caps) is int and caps&0x4000 and caps&0x04000000,'M2M streaming capabilities')
    hold=kinds['hold_request_external_stop_retain']
    require(rows[-1] is hold and hold.get('frames')==3 and hold.get('pts_seen_mask')==7 and hold.get('errno')==0 and hold.get('eos') is True,'complete final HOLD')
    require(all(hold.get(k) is False for k in ('hardware_decode_verified','dma_stop_verified','kernel_stop_retain_verified')),'false proof flags required')
    require(not any(r['request']==1074026003 or (r['request']==3222558216 and r.get('count')==0) for r in calls),'forbidden cleanup')
    formats=[r for r in calls if r['request']==3234878980 and r.get('type')==9]
    require(len(formats)==1,'capture G_FMT')
    require(all(formats[0].get(k)==v for k,v in {'fourcc':int.from_bytes(b'NV12','little'),'width':128,'height':128,'planes':1,'stride':128,'sizeimage':24576}.items()),'fixed capture geometry')
    selections=[r for r in calls if r['request']==3225441886 and r.get('type')==9]
    require(len(selections)==1 and all(selections[0].get(k)==v for k,v in {'left':0,'top':0,'width':128,'height':128}.items()),'visible selection')
    pts=[1000000+i*40000 for i in range(3)]
    ins=[r for r in calls if r['request']==QBUF and r.get('type')==10]
    require([r.get('pts_us') for r in ins]==pts and [r.get('bytesused') for r in ins]==stream,'input AU/PTS binding')
    out=[r for r in calls if r['request']==DQBUF and r.get('type')==10]
    dqs=[r for r in calls if r['request']==DQBUF]
    require(len(out)==3,'output dequeue count')
    for r in dqs:
        require(r.get('planes')==1 and type(r.get('flags')) is int and not r['flags']&0x40,'DQ ERROR/plane count')
        require(0<=r['data_offset']<=r['bytesused']<=r['length']<=8*1024*1024 and 0<=r['index']<8,'DQ bounds')
    caps=[r for r in dqs if r.get('type')==9]
    actual=[r for r in caps if r['bytesused']>r['data_offset']]
    require(len(actual)==3 and sorted(r['pts_us'] for r in actual)==pts,'capture PTS set')
    for r in actual:
        require(r['bytesused']-r['data_offset']==FRAME,'raw extent')
        require(frames[pts.index(r['pts_us'])][0]==r['pts_us'],'capture to envelope PTS')
    last=[r for r in caps if r['flags']&0x100000]
    stop=[r for r in calls if r['request']==3225966176]
    require(len(last)==1 and last[0]['seq']>=actual[-1]['seq'] and len(stop)==1 and stop[0].get('decoder_cmd')==1 and out[-1]['seq']<stop[0]['seq']<last[0]['seq'],'STOP/LAST ordering')
    return {'node':node,'capture_arrival_pts':[r['pts_us'] for r in actual],'trace_rows':len(rows)}

def verify_bytes(blob, *, oracle, stream, expected_attempt, expected_bindings):
    """expected_bindings: exactly six 32-byte SHA256 values in BINDINGS order.
    Caller independently hashes immutable source/ELF/kernel/final record;
    passing header values back as expected values invalidates that binding.
    """
    require(type(blob) is bytes and HEADER+32<=len(blob)<=MAX,'envelope byte bound')
    require(len(expected_bindings)==6 and all(type(x) is bytes and len(x)==32 and any(x) for x in expected_bindings),'expected binding types')
    magic,version,header,total,attempt,count,w,h,fmt,payload,trace_len=struct.unpack_from('<4sIIIQIII4sII',blob)
    require((magic,version,header,total)==(b'FDE1',1,HEADER,len(blob)),'header identity/declared length')
    require(type(expected_attempt) is int and 0<expected_attempt<2**64 and attempt==expected_attempt,'attempt')
    require((count,w,h,fmt,payload)==(3,128,128,b'NV12',73728),'fixed geometry/count/total')
    require(0<trace_len<=TRACE_MAX and total==HEADER+trace_len+3*(16+FRAME)+32,'section length')
    require(blob[272:HEADER]==bytes(16),'reserved bytes')
    require([blob[48+i*32:80+i*32] for i in range(6)]==list(expected_bindings),'source/run binding')
    require(hashlib.sha256(blob[:-32]).digest()==blob[-32:],'envelope SHA256')
    trace=blob[HEADER:HEADER+trace_len]
    require(hashlib.sha256(trace).digest()==blob[240:272],'trace SHA256')
    require(type(oracle) is bytes and len(oracle)==73744 and oracle[:16]==struct.pack('<4sIII',b'FNV1',128,128,3),'FNV1 bound/header')
    require(hashlib.sha256(oracle).digest()==expected_bindings[4],'oracle binding')
    require(type(stream) is bytes and 8<len(stream)<=8*1024*1024 and stream[:8]==struct.pack('<4sI',b'FIR1',3),'FIR1 bound/header')
    require(hashlib.sha256(stream).digest()==expected_bindings[3],'stream binding')
    au=[];p=8
    for _ in range(3):
        require(p+4<=len(stream),'FIR1 truncation')
        n=struct.unpack_from('<I',stream,p)[0];p+=4
        require(n>0 and p+n<=len(stream),'FIR1 AU extent');au.append(n);p+=n
    require(p==len(stream),'FIR1 trailing bytes')
    frames=[];p=HEADER+trace_len
    for i in range(3):
        index,n,pts=struct.unpack_from('<IIQ',blob,p);p+=16
        require((index,n,pts)==(i,FRAME,1000000+40000*i),'frame index/bytes/PTS')
        raw=blob[p:p+n];p+=n
        require(raw==oracle[16+i*FRAME:16+(i+1)*FRAME],'actual capture/oracle bytes')
        frames.append((pts,hashlib.sha256(raw).hexdigest()))
    require(p+32==len(blob),'strict EOF')
    origin=trace_check(trace,frames,au)
    return {'offline_capture_consistent':True,'hardware_decode_verified':False,'hardware_task_proven':False,'dma_stop_verified':False,'kernel_stop_retain_verified':False,'envelope_bytes':len(blob),'payload_bytes':73728,'envelope_sha256':hashlib.sha256(blob).hexdigest(),'frames':[{'index':i,'pts_us':p,'bytes':FRAME,'sha256':sha} for i,(p,sha) in enumerate(frames)],'origin':origin,'origin_authenticated':False}

def verify_file(path, **expected):
    """Pure offline regular-host-file entry point. No devices, FIFOs or resync.
    Intermediate directories are trusted caller-owned paths, not wire inputs.
    """
    before=os.lstat(path)
    require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=MAX,'bounded regular host file')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    with os.fdopen(fd,'rb') as f:
        opened=os.fstat(f.fileno())
        require(stat.S_ISREG(opened.st_mode) and opened.st_nlink==1 and opened.st_size<=MAX and (opened.st_dev,opened.st_ino)==(before.st_dev,before.st_ino),'host file changed/type')
        blob=f.read(MAX+1)
        after=os.fstat(f.fileno())
        require((opened.st_size,opened.st_mtime_ns,opened.st_ctime_ns)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns),'host file changed during read')
    return verify_bytes(blob,**expected)
