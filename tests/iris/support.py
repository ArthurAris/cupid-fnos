# SPDX-License-Identifier: GPL-2.0-only
"""Host-only C extraction; outputs always in a fresh directory."""
import argparse, pathlib, re, subprocess, tempfile, hashlib, json
p=argparse.ArgumentParser(description="Actual published C host logic, not device proof")
p.add_argument("source",nargs="?",type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[2]/"sources/iris/research")
p.add_argument("--output",type=pathlib.Path,help="Existing nonsymlink non-root parent, fresh child created")
p.add_argument("--cc",default="cc")
a=p.parse_args();d=a.source.resolve()
if (d/"driver").is_dir():d=d/"driver"
manifest=json.loads((d/"source-manifest.json").read_text())
source_hashes={pathlib.Path(f["path"]).name:f["sha256"] for f in manifest["files"]}
extracted=[]
def source_text(n):
 f=d/n
 if not f.is_file() or f.is_symlink():raise ValueError("regular source required")
 b=f.read_bytes()
 if hashlib.sha256(b).hexdigest()!=source_hashes[n]:raise ValueError("source hash mismatch: "+n)
 return b.decode()
def extract(n,name):
 t=source_text(n);m=re.search(r"(?m)^(?:static )?(?:int|bool|void|long|u32|struct iris_inst \*)\s*"+re.escape(name)+r"\([^;]*?\)\n\{",t)
 if not m:raise ValueError((n,name))
 masked=re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',lambda z:" "*len(z[0]),t,flags=re.S)
 b=t.index("{",m.start());i=b+1;depth=1
 while depth:depth+=(masked[i]=="{")-(masked[i]=="}");i+=1
 text=t[m.start():i];extracted.append({"source":n,"function":name,"source_sha256":source_hashes[n],"extracted_sha256":hashlib.sha256(text.encode()).hexdigest()});return text
parent=a.output or pathlib.Path(tempfile.gettempdir())
if not parent.is_dir() or parent.is_symlink() or parent.resolve()==pathlib.Path(parent.anchor):raise ValueError("existing nonsymlink non-root output parent required")
for ancestor in [parent,*parent.parents]:
 if ancestor.is_symlink():raise ValueError("symlink output ancestor refused")
work=pathlib.Path(tempfile.mkdtemp(prefix="iris-host-",dir=parent))
def run_host(name,code,extra=()):
 c=work/(name+".c");c.write_text(code);results=[]
 for mode,flags in [("native",["-O2"]),("asan-ubsan",["-O1","-fsanitize=address,undefined","-fno-omit-frame-pointer","-fno-pie","-no-pie"])]:
  exe=work/(name+"-"+mode);cmd=[a.cc,"-std=gnu11","-g","-Wall","-Wextra","-Werror",*extra,*flags,str(c),"-o",str(exe)]
  r=subprocess.run(cmd,capture_output=True,text=True,timeout=90)
  if r.returncode:raise RuntimeError(r.stderr)
  r=subprocess.run([str(exe)],capture_output=True,text=True,timeout=30)
  if r.returncode:raise RuntimeError(r.stdout+r.stderr)
  print(mode,r.stdout.strip());results.append({"mode":mode,"passed":True,"summary":r.stdout.strip()})
 report={"schema":"iris-published-host-logic-1","case":name,"generated_c_sha256":hashlib.sha256(c.read_bytes()).hexdigest(),"functions":extracted,"results":results,"host_facades_only":True,"physical_dma_proof":False,"device_verified":False,"full_kernel_built":False,"published_gates_modified":False}
 (work/(name+"-result.json")).write_text(json.dumps(report,indent=2)+"\n");print(json.dumps(report))
