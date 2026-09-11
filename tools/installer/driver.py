"""Prepare and qualify unpublished wheels only in newly owned remote containers."""

import io
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

root = Path(__file__).resolve().parents[2]
run_id = "sibyl14-installer-" + uuid4().hex
remote_root = "/home/dev/dev/eval-runs/" + run_id
ssh = ["ssh", "-o", "BatchMode=yes", "devbox-stef-gradial-com-main"]
archive = io.BytesIO()
with tarfile.open(fileobj=archive, mode="w:gz") as tar:
    for source, target in [
        (root / "install.sh", "install.sh"),
        (root / "tools/installer/qualify.py", "qualify.py"),
        (root / "tools/installer/Dockerfile", "Dockerfile"),
    ]:
        tar.add(source, arcname=target)
    for wheel in (root / "dist").glob("*.whl"):
        tar.add(wheel, arcname="wheels/" + wheel.name)
extract = f'import os,sys,tarfile; os.makedirs({remote_root!r},exist_ok=False);tarfile.open(fileobj=sys.stdin.buffer,mode="r|gz").extractall({remote_root!r},filter="data")'
subprocess.run(  # noqa: S603 - fixed SSH host and owned payload
    [*ssh, "env -i PATH=/usr/bin:/bin python3 -c " + shlex.quote(extract)],
    input=archive.getvalue(),
    check=True,
)
remote = r"""
import hashlib,json,os,pathlib,shutil,subprocess
r=pathlib.Path(ROOT)
name=NAME
base=pathlib.Path('/home/dev/dev/eval-runs/sibyl14-official-api-67a3b43ea7f44f2a804fc1165a975c44')
shutil.copyfile('/home/dev/.proto/tools/uv/0.12.5/uv',r/'uv');(r/'uv').chmod(0o755)
inputs=r/'inputs';inputs.mkdir()
for item in ('install.sh','qualify.py','wheels'):
    shutil.move(str(r/item),str(inputs/item))
results=r/'results';results.mkdir()
# Freeze input bytes before any installer process exists.
(r/'inputs.json').write_text(json.dumps({str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() for p in r.rglob('*') if p.is_file()},indent=2))
env={'PATH':'/usr/bin:/bin','DOCKER_HOST':'unix:///run/devbox-docker/docker.sock'}
def docker(*args,**kw):return subprocess.run(['/usr/bin/docker',*args],env=env,text=True,**kw)
with (r/'build.log').open('w') as log:
    docker('build','--label','sibyl.qualification='+name,'-t',name,str(r),stdout=log,stderr=subprocess.STDOUT).check_returncode()
image=json.loads(subprocess.check_output(['/usr/bin/docker','image','inspect',name],env=env))[0]
(r/'image.json').write_text(json.dumps(image,indent=2))
assert image['Config']['Labels']['sibyl.qualification']==name
for mode in ('remote','daemon','bootstrap'):
    cname=name+'-'+mode
    try:
        with (r/(mode+'.log')).open('w') as log:
            result=docker('run','--name',cname,'--hostname',cname,'--env','SIBYL_INSTALLER_RUN_ID='+name,'--env','SIBYL_INSTALLER_ROLE='+mode,'--label','sibyl.qualification='+name,'--cap-drop=ALL','--security-opt=no-new-privileges','--mount','type=bind,source='+str(inputs)+',target=/input,readonly','--mount','type=bind,source='+str(results)+',target=/output','--entrypoint','python',image['Id'],'/input/qualify.py',('remote' if mode=='bootstrap' else mode),'--inputs','/input','--output','/output/'+mode,*(['--bootstrap'] if mode=='bootstrap' else []),stdout=log,stderr=subprocess.STDOUT)
        result.check_returncode()
    finally:
        check=docker('inspect',cname,capture_output=True)
        if check.returncode==0:
            data=json.loads(check.stdout)[0]
            assert data['Config']['Labels']['sibyl.qualification']==name and data['Image']==image['Id']
            (r/(mode+'-container.json')).write_text(json.dumps(data,indent=2))
            removed=docker('rm','-f','-v',cname,capture_output=True)
            absent=docker('inspect',cname,capture_output=True).returncode != 0
            (r/(mode+'-cleanup.json')).write_text(json.dumps({'container_id':data['Id'],'remove_exit':removed.returncode,'container_absent':absent,'host_ports':data['HostConfig'].get('PortBindings')},indent=2))
            removed.check_returncode()
            assert absent
        expected=json.loads((r/'inputs.json').read_text())
        assert all(hashlib.sha256((r/path).read_bytes()).hexdigest()==digest for path,digest in expected.items())
        (r/(mode+'-input-verification.json')).write_text(json.dumps({'verified_files':len(expected),'unchanged':True}))
print('QUALIFIED',str(r),flush=True)
""".replace("ROOT", repr(remote_root)).replace("NAME", repr(run_id))
sys.stdout.write(f"OWNED_ROOT {remote_root}\n")
sys.stdout.flush()
result = subprocess.run(  # noqa: S603 - fixed SSH host and owned payload
    [*ssh, "env -i PATH=/usr/bin:/bin python3 -"], input=remote, text=True, check=False
)
raise SystemExit(result.returncode)
