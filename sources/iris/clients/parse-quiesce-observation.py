#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline 180-byte quiesce observation parser; no device access or proof promotion.
Run Python -I -B. Expected nonce/bindings/node are independent caller inputs.
CLOSE ACK is absent from this immutable receipt: complete quiesce is unverified.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import struct

CORE_SHA256 = '7a14d9e9ca33da6da535eeb7914116390c535a919280057051502225d03e48ba'
VERIFIER_SHA256 = '2fdef228181b9b7bc7f8c1182002662dfdd41a69ba802c5d551d2a8a1f309e5e'
FIELDS = tuple(('version session kind index generation header packet type port size addr_lo addr_hi '
 'request_header request_packet request_generation flags stop_blocked close_attempted cpu_drained orphaned '
 'lifecycle_generation lifecycle_stage submitted returned ah ap arh arp arf aa bh bp brh brp brf ba '
 'rx_header rx_packet rx_flags rx_buffer_flags rx_generation rx_result ordinal buffer_index retired').split())
assert len(FIELDS) == 45
STATUS_KIND = 0xffffffff
KINDS = (3,5,6,7,8,9)
# iris_buffer_type -> actual HFI buffer type, decoder port.
TYPE_PORT = {0:(1,1),1:(2,2),3:(7,1),5:(10,1),6:(11,1),7:(8,1),8:(6,2),9:(12,0)}
COMMON = tuple('session stop_blocked close_attempted cpu_drained orphaned lifecycle_generation lifecycle_stage ah ap arh arp arf aa bh bp brh brp brf ba'.split())
PROOFS = dict(close_ack_proven=False,quiesce_verified=False,safe_reclamation_verified=False,
 physical_reclaim_verified=False,native_decode_verified=False,hardware_decode_verified=False,
 hardware_task_proven=False,dma_stop_verified=False,kernel_stop_retain_verified=False)

def require(value, message):
 if not value: raise ValueError(message)

def unique(pairs):
 out={}
 for k,v in pairs:
  require(k not in out,'duplicate JSON key');out[k]=v
 return out

def uint(v):return type(v) is int and 0 <= v <= 0xffffffff

def sint(v):return type(v) is int and -4095 <= v <= 0

def decode_receipt(row):
 require(type(row) is dict and set(row)=={'kind','bytes','data_hex'},'receipt record fields')
 require(row['kind']=='quiesce_receipt' and type(row['bytes']) is int and row['bytes']==180,'receipt extent')
 require(type(row['data_hex']) is str and re.fullmatch('[0-9a-f]{360}',row['data_hex']),'exact lowercase 180B hex')
 r=dict(zip(FIELDS,struct.unpack('<45I',bytes.fromhex(row['data_hex']))))
 if r['rx_result'] >= 0x80000000:r['rx_result']-=0x100000000
 require(r['version']==1,'receipt version')
 return r

def decode_trace(trace):
 """Decode actual collector output even when collection/control failed.
 Does NOT validate ownership or claim tuple success. Capacity failures stay errors.
 """
 require(type(trace) is bytes and 0<len(trace)<=49152 and trace.endswith(b'\n') and b'\0' not in trace,'bounded trace')
 rows=[]
 for line in trace.splitlines():
  require(0<len(line)<2048,'trace row bound')
  row=json.loads(line,object_pairs_hook=unique,parse_constant=lambda x:require(False,'JSON constant'))
  require(type(row) is dict,'trace object');rows.append(row)
 controls=[r for r in rows if r.get('kind')=='quiesce_control']
 holds=[r for r in rows if r.get('kind')=='quiesce_hold_observation']
 require(len(controls)<=1 and len(holds)==1,'single control/final observation')
 hold=holds[0]
 require(set(hold)=={'kind','control_rc','export_rc','receipts','frames','pts_mask','safe_reclamation_verified','native_decode_verified','dma_stop_verified','reclaim_enabled'},'observation fields')
 require(sint(hold['control_rc']) and sint(hold['export_rc']) and uint(hold['receipts']) and hold['receipts']<=65,'observation bounds')
 require(uint(hold['frames']) and hold['frames']<=3 and uint(hold['pts_mask']) and hold['pts_mask']<=7,'fixture bounds')
 require(all(hold[k] is False for k in ('safe_reclamation_verified','native_decode_verified','dma_stop_verified','reclaim_enabled')),'proof escalation')
 for c in controls:
  require(set(c)=={'kind','command','rc','result','stage','session','generation','explicit_once','reclaim_enabled'},'control fields')
  require(type(c['command']) is int and c['command']==0xc01856ca and sint(c['rc']) and sint(c['result']),'control identity/result')
  require(uint(c['stage']) and uint(c['session']) and c['session']>0 and uint(c['generation']) and c['generation']>0,'control binding')
  require(c['explicit_once'] is True and c['reclaim_enabled'] is False and c['rc']==hold['control_rc'],'control correlation')
 receipts=[decode_receipt(r) for r in rows if r.get('kind')=='quiesce_receipt']
 require(len(receipts)<=64 and hold['receipts']>=len(receipts),'receipt cap/count')
 if hold['export_rc']==0:require(hold['receipts']==len(receipts),'complete export count')
 q=[r for r in rows if str(r.get('kind','')).startswith('quiesce')]
 require(all(r.get('kind') in ('quiesce_control','quiesce_receipt','quiesce_hold_observation') for r in q),'unknown quiesce record')
 require(q[-1] is hold,'final quiesce record')
 if controls:require(q[0] is controls[0],'control before receipts')
 return {'rows':rows,'control':controls[0] if controls else None,'hold':hold,'receipts':receipts,
  'quiesce_tuple_subset_validated':False,'status':'quiesce_unverified',**PROOFS}

def validate_subset(decoded, app_submits=None):
 c,h,rs=decoded['control'],decoded['hold'],decoded['receipts']
 require(c is not None and c['rc']==c['result']==h['control_rc']==h['export_rc']==0,'successful complete control/export required')
 require(h['frames']==3 and h['pts_mask']==7 and c['generation']==1 and c['stage']==5,'fixed lifecycle prerequisites')
 require(len(rs)==h['receipts'] and rs and rs[0]['kind']==STATUS_KIND,'status first/complete receipts')
 status=rs[0]
 require(sum(r['kind']==STATUS_KIND for r in rs)==1,'unique status')
 require(status['index']==status['ordinal']==status['buffer_index']==status['retired']==0,'status selectors')
 require(status['session']==c['session'] and status['lifecycle_generation']==c['generation'] and status['lifecycle_stage']==5,'session/generation/stage binding')
 require(status['stop_blocked']==status['close_attempted']==status['cpu_drained']==1 and status['orphaned']==0,'blocked/close-attempt/CPU-drain subset')
 for k in ('a','b'):
  require(status[k+'a']==1 and status[k+'h']>0 and status[k+'p']>0 and status[k+'h']==status[k+'rh'] and status[k+'p']==status[k+'rp'],'matched STOP ACK')
  require(status[k+'rf']&1 and not status[k+'rf']&~3,'STOP SUCCESS flags')
 require((status['ah'],status['ap'])!=(status['bh'],status['bp']),'distinct STOP identities')
 rank={kind:i for i,kind in enumerate((0,1,*KINDS))}
 require(all(r['kind'] in rank for r in rs[1:]) and [rank[r['kind']] for r in rs[1:]]==sorted(rank[r['kind']] for r in rs[1:]),'collector per-kind export order')
 owner_fields=FIELDS[4:16]+('submitted','returned','rx_header','rx_packet','rx_flags','rx_buffer_flags','rx_generation','rx_result')
 require(all(status[k]==0 for k in owner_fields),'status cannot forge owner fields')
 identities=set();keys=set();groups={k:[] for k in (0,1,*KINDS)};relations=[]
 for r in rs[1:]:
  require(all(r[k]==status[k] for k in COMMON),'receipt frozen lifecycle correlation')
  kind=r['kind'];require(kind in groups,'unsupported kind')
  require(r['flags']==1,'valid returned owner only; unknown/readonly/pending rejected')
  require((r['type'],r['port'])==TYPE_PORT[kind],'HFI buffer type/port')
  require(r['generation']>0 and r['header']>0 and r['packet']>0 and r['size']>0 and (r['addr_lo'] or r['addr_hi']),'nonzero submitted identity')
  require(0<r['submitted']==r['returned']<=0xffffffff and r['rx_result']==0 and r['rx_generation']==r['generation'],'actual equal return counts/generation')
  require(r['rx_flags']&1 and not r['rx_flags']&~3 and not r['rx_buffer_flags']&0x10,'SUCCESS/nonREADONLY')
  if kind<2:
   require(r['index']<32 and r['ordinal']==0 and r['buffer_index']==r['index'] and r['retired']==0,'APP original slot identity')
   key=(kind,r['index']);require(r['rx_buffer_flags']&~0x30000000==0,'APP buffer flags')
  else:
   require(r['index']==0 and r['ordinal']<31 and r['buffer_index']<32 and r['retired'] in (0,1),'internal ordinal/original buffer index')
   key=(kind,r['ordinal']);require(not r['rx_buffer_flags']&~1,'internal RELEASE flags')
  require(key not in keys,'duplicate slot/ordinal');keys.add(key);groups[kind].append(r)
  identity=(kind,r['generation'],r['header'],r['packet'],r['addr_hi'],r['addr_lo'],r['size'])
  require(identity not in identities,'duplicate original ownership tuple');identities.add(identity)
  release=bool(r['rx_buffer_flags']&1)
  if kind<2:
   require(not any(r[k] for k in ('request_header','request_packet','request_generation')),'APP no RELEASE request')
   expected=(r['header'],r['packet']);relation='latest_submit'
  elif release:
   require(r['request_header']>0 and r['request_packet']>0 and r['request_generation']==r['generation'],'exact RELEASE request generation')
   expected=(r['request_header'],r['request_packet']);relation='PSC_retired_RELEASE' if r['retired'] else 'exact_RELEASE'
  else:
   require(kind!=9 and not r['retired'] and not any(r[k] for k in ('request_header','request_packet','request_generation')),'PERSIST/PSC must exact RELEASE')
   if kind==8:expected=(r['header'],r['packet']);relation='DPB_latest_submit'
   else:expected=(status['ah'],status['ap']) if r['port']==1 else (status['bh'],status['bp']);relation='new_generation_STOP'
  require((r['rx_header'],r['rx_packet'])==expected,'exact perbuffer return header/packet relation')
  relations.append({'kind':kind,'index':r['index'],'ordinal':r['ordinal'],'buffer_index':r['buffer_index'],'retired':r['retired'],'relation':relation})
 for kind in KINDS:
  require([r['ordinal'] for r in groups[kind]]==list(range(len(groups[kind]))),'complete ordered per-kind ordinals')
 require(groups[9] and groups[8],'PERSIST and DPB mandatory')
 for kind in (0,1):
  require(groups[kind] and [r['index'] for r in groups[kind]]==sorted(r['index'] for r in groups[kind]),'APP unique ascending slots')
 if app_submits is not None:
  actual={(kind,r['index']):r['submitted'] for kind in (0,1) for r in groups[kind]}
  require(actual==app_submits,'APP receipt exact actual QBUF counts')
 return {'quiesce_tuple_subset_validated':True,'status':'quiesce_unverified','status_receipt':status,
  'receipts':rs,'return_relations':relations,'missing_evidence':['raw correlated CLOSE ACK fields absent from immutable 180B receipt'],**PROOFS}

def trusted_verifier():
 path=Path(__file__).resolve().parent/'verify_fde.py'
 require(hashlib.sha256(path.read_bytes()).hexdigest()==VERIFIER_SHA256,'published FDE verifier (original logic; SPDX-only derivative)')
 spec=importlib.util.spec_from_file_location('quiesce_original_fde',path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

def parse(blob, *, oracle, stream, expected_attempt, expected_bindings, expected_node):
 require(type(blob) is bytes and 320<=len(blob)<=131072 and blob[:4]==b'FDE1','bounded FDE1')
 require(struct.unpack_from('<I',blob,8)[0]==288 and struct.unpack_from('<I',blob,12)[0]==len(blob),'FDE header/extent')
 n=struct.unpack_from('<I',blob,44)[0]
 require(0<n<=49152 and len(blob)==288+n+3*(16+24576)+32,'FDE exact section bounds')
 require(hashlib.sha256(blob[:-32]).digest()==blob[-32:] and hashlib.sha256(blob[288:288+n]).digest()==blob[240:272],'original envelope/trace SHA256')
 decoded=decode_trace(blob[288:288+n]);rows=decoded['rows']
 require(rows[-1].get('kind')=='hold_request_external_stop_retain','original final HOLD kept')
 quiesce_end=next(i for i,r in enumerate(rows) if r.get('kind')=='quiesce_hold_observation')
 require(quiesce_end==len(rows)-2,'quiesce precedes original final HOLD')
 control_index=next((i for i,r in enumerate(rows) if r.get('kind')=='quiesce_control'),-1)
 require(control_index>=0 and all(r.get('kind')=='quiesce_receipt' for r in rows[control_index+1:quiesce_end]),'contiguous control/receipt/HOLD collection')
 require(all('request' not in r for r in rows[control_index:]),'no decode/cleanup ioctl after quiesce control')
 # Exact projection removes ONLY three new record kinds. Oracle/PTS/node/querycap/
 # monotonic ioctls/LAST/HOLD/nonce/six independent expected pins remain unchanged.
 keep=[r for r in rows if r.get('kind') not in ('quiesce_control','quiesce_receipt','quiesce_hold_observation')]
 trace=b''.join((json.dumps(r,separators=(',',':'))+'\n').encode() for r in keep)
 header=bytearray(blob[:288]);struct.pack_into('<I',header,44,len(trace));header[240:272]=hashlib.sha256(trace).digest()
 content=bytes(header)+trace+blob[288+n:-32];header=bytearray(content[:288]);struct.pack_into('<I',header,12,len(content)+32);content=bytes(header)+content[288:]
 frames=trusted_verifier().verify_bytes(content+hashlib.sha256(content).digest(),oracle=oracle,stream=stream,expected_attempt=expected_attempt,expected_bindings=expected_bindings)
 require(type(expected_node) in (tuple,list) and len(expected_node)==2 and all(uint(x) for x in expected_node),'independent expected node')
 node=frames['origin']['node'];require((node['major'],node['minor'])==tuple(expected_node),'actual opened node binding')
 cap=next(r for r in keep if r.get('kind')=='querycap')
 for field,size in (('driver_hex',16),('card_hex',32),('bus_info_hex',32)):
  require(type(cap.get(field)) is str and re.fullmatch('[0-9a-f]{'+str(size*2)+'}',cap[field]),'querycap exact fixed array')
 require(uint(cap.get('capabilities')) and uint(cap.get('device_caps')),'querycap u32 capabilities')
 require(bytes.fromhex(cap['driver_hex']).startswith(b'iris_driver\0') and bytes.fromhex(cap['card_hex']).startswith(b'Iris Decoder\0'),'terminated actual driver/card')
 app={}
 for r in keep:
  if r.get('request')==3227014671:
   require(r.get('type') in (9,10) and uint(r.get('index')) and r['index']<32,'actual QBUF index/type')
   key=(0 if r['type']==10 else 1,r['index']);app[key]=app.get(key,0)+1
 result=validate_subset(decoded,app)
 return {'schema':'iris-quiesce-observation-180B-1','receipt_core_sha256':CORE_SHA256,
  'scope':'offline tuple/count consistency subset only; no raw CLOSE ACK, safe reclamation, physical DMA or native acceptance',
  'reclaim_enabled':False,
  'envelope_sha256':hashlib.sha256(blob).hexdigest(),'original_trace_sha256':hashlib.sha256(blob[288:288+n]).hexdigest(),
  'frame_report':frames,'projection_used_only_for_original_frame_verifier':True,
  'control':decoded['control'],'hold':decoded['hold'],**result}

def read_regular(path, limit):
 before=os.lstat(path);require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=limit,'bounded regular file')
 fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
 with os.fdopen(fd,'rb') as f:
  s=os.fstat(f.fileno());require((s.st_dev,s.st_ino)==(before.st_dev,before.st_ino) and stat.S_ISREG(s.st_mode),'unchanged regular file')
  data=f.read(limit+1);after=os.fstat(f.fileno());require((s.st_size,s.st_mtime_ns,s.st_ctime_ns)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns),'file changed')
 require(len(data)<=limit,'file cap');return data

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('capture',type=Path);p.add_argument('--oracle',type=Path,required=True);p.add_argument('--stream',type=Path,required=True)
 p.add_argument('--expected-attempt',type=int,required=True);p.add_argument('--expected-binding',action='append',required=True);p.add_argument('--expected-node',nargs=2,type=int,required=True);a=p.parse_args()
 print(json.dumps(parse(read_regular(a.capture,131072),oracle=read_regular(a.oracle,73744),stream=read_regular(a.stream,8*1024*1024),expected_attempt=a.expected_attempt,expected_bindings=[bytes.fromhex(x) for x in a.expected_binding],expected_node=a.expected_node),indent=2))
