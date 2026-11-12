"""Portable entry point and process supervisor for a single-machine deployment."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from . import __version__, paths


def parser():
    root=argparse.ArgumentParser(prog='vivideo',description='ViVideo 本地视频与对话服务')
    root.add_argument('--version',action='version',version=__version__)
    commands=root.add_subparsers(dest='command',required=True)
    for name,help_text in [('serve','启动网关、对话 Worker 和下载 Worker'),('doctor','检查数据目录和媒体工具'),
                           ('worker','只启动对话 Worker'),('downloader','只启动下载 Worker')]:
        cmd=commands.add_parser(name,help=help_text)
        cmd.add_argument('--data-dir',help='持久化目录，覆盖 VIVIDEO_DATA_DIR')
        cmd.add_argument('--tools-dir',help='FFmpeg、ffprobe、Node 所在目录，加入子进程 PATH')
        if name!='doctor':cmd.add_argument('--zmq-endpoint',help='覆盖 VIVIDEO_ZMQ_ENDPOINT，默认 tcp://127.0.0.1:5555')
        if name=='serve':
            cmd.add_argument('--host',default=os.getenv('VIVIDEO_HOST','127.0.0.1'))
            cmd.add_argument('--port',type=int,default=os.getenv('VIVIDEO_PORT','8000'))
            cmd.add_argument('--mode',choices=['zmq','demo'],default=os.getenv('VIVIDEO_MODE','zmq'))
            cmd.add_argument('--no-local-video',action='store_true',help='使用旧版链接/嵌入播放器模式')
            cmd.add_argument('--log-level',choices=['debug','info','warning','error'],default='info')
            cmd.add_argument('--reload',action='store_true',help='开发模式：Python 源码变化时统一重启三个服务，需要可编辑安装')
    return root


def environment(args):
    env=dict(os.environ)
    if args.data_dir:env['VIVIDEO_DATA_DIR']=str(Path(args.data_dir).expanduser().resolve())
    if getattr(args,'zmq_endpoint',None):env['VIVIDEO_ZMQ_ENDPOINT']=args.zmq_endpoint
    if getattr(args,'no_local_video',False):env['VIVIDEO_LOCAL_VIDEO']='0'
    if getattr(args,'mode',None):env['VIVIDEO_MODE']=args.mode
    tools=Path(args.tools_dir).expanduser().resolve() if args.tools_dir else None
    if tools and not tools.is_dir():raise ValueError('媒体工具目录不存在')
    if tools:env['PATH']=str(tools)+os.pathsep+env.get('PATH','')
    # The same absolute data root is shared by every child, independent of CWD.
    previous=os.environ.get('VIVIDEO_DATA_DIR')
    try:
        if env.get('VIVIDEO_DATA_DIR'):os.environ['VIVIDEO_DATA_DIR']=env['VIVIDEO_DATA_DIR']
        env['VIVIDEO_DATA_DIR']=str(paths.data_root())
    finally:
        if previous is None:os.environ.pop('VIVIDEO_DATA_DIR',None)
        else:os.environ['VIVIDEO_DATA_DIR']=previous
    return env


def tool_report(env):
    tools={};missing=[]
    for name in ('ffmpeg','ffprobe','node'):
        if name=='node' and env.get('VIVIDEO_JS_RUNTIME'):
            tools[name]=env['VIVIDEO_JS_RUNTIME'];continue
        path=env.get('VIVIDEO_'+name.upper()+'_BIN') or shutil.which(name,path=env.get('PATH','')) or paths.bundled_tool(name)
        if path and Path(path).is_file() and os.access(path,os.X_OK):tools[name]=path
        else:missing.append(name)
    if importlib.util.find_spec('yt_dlp') is None:missing.append('yt-dlp')
    return {'version':__version__,'python':sys.version.split()[0],
            'data_dir':env['VIVIDEO_DATA_DIR'],'tools':tools,'missing':missing}


def check_data_directory(env):
    root=Path(env['VIVIDEO_DATA_DIR']);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    # A real write check also catches readonly container mounts and permissions.
    import tempfile
    with tempfile.TemporaryFile(dir=root):pass


def check_bind(host,port):
    if not 1<=port<=65535:raise ValueError('端口必须在 1～65535 之间')
    errors=[]
    for family,kind,protocol,_,address in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM):
        try:
            with socket.socket(family,kind,protocol) as probe:probe.bind(address)
            return
        except OSError as exc:errors.append(exc)
    raise ValueError(f'无法监听 {host}:{port}，请检查地址或端口占用') from (errors[-1] if errors else None)


def source_snapshot(directory):
    snapshot={}
    for path in Path(directory).rglob('*.py'):
        if '__pycache__' in path.parts:continue
        try:
            stat=path.stat();snapshot[str(path)]=(stat.st_mtime_ns,stat.st_size)
        except FileNotFoundError:pass  # Editors may atomically replace a file.
    return snapshot


def stop_children(children):
    for _,process in children:
        if process.poll() is None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
    deadline=time.monotonic()+12
    for _,process in children:
        try:process.wait(max(.01,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait()


def supervise(commands,env,reload_dir=None):
    if os.name!='posix':raise ValueError('当前服务进程管理支持 Linux/macOS；Windows 请在 WSL 中运行')
    stopping=threading.Event();children=[];old_handlers={};exit_code=0
    def stop(signum,frame):stopping.set()
    def launch():
        for name,command in commands:
            if stopping.is_set():break
            children.append((name,subprocess.Popen(command,env=env,start_new_session=True)))
    for sig in (signal.SIGINT,signal.SIGTERM):old_handlers[sig]=signal.signal(sig,stop)
    previous=source_snapshot(reload_dir) if reload_dir else None
    changed_at=None
    try:
        launch()
        while not stopping.wait(.2):
            if reload_dir:
                current=source_snapshot(reload_dir)
                if current!=previous:
                    previous=current;changed_at=time.monotonic()
                if changed_at is not None and time.monotonic()-changed_at>=.4:
                    print('检测到 Python 源码变化，正在重启全部服务…',flush=True)
                    stop_children(children);children=[]
                    if stopping.is_set():break
                    launch();changed_at=None
            ended=next(((name,p) for name,p in children if p.poll() is not None),None)
            if ended:
                name,process=ended
                if reload_dir:
                    print(f'{name} 已退出（{process.returncode}），停止其余服务，等待源码修复…',file=sys.stderr)
                    stop_children(children);children=[]
                else:
                    print(f'{name} 已退出（{process.returncode}），正在停止其余服务。',file=sys.stderr)
                    exit_code=process.returncode if process.returncode and process.returncode>0 else 1
                    break
    finally:
        stop_children(children)
        for sig,handler in old_handlers.items():signal.signal(sig,handler)
    return exit_code


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        env=environment(args)
        if args.command=='doctor':
            report=tool_report(env)
            try:check_data_directory(env);report['data_writable']=True
            except OSError:report['data_writable']=False
            print(json.dumps(report,ensure_ascii=False,indent=2))
            return 0 if report['data_writable'] and not report['missing'] else 1
        if os.name!='posix':raise ValueError('当前支持 Linux/macOS；Windows 请使用 WSL')
        check_data_directory(env)
        if args.command in ('worker','downloader'):
            if args.command=='downloader' and tool_report(env)['missing']:
                raise ValueError('缺少媒体依赖，请运行 vivideo doctor 查看详情')
            module='vivideo.worker' if args.command=='worker' else 'vivideo.download_worker'
            return supervise([(args.command,[sys.executable,'-m',module])],env)
        reload_dir=None
        if args.reload:
            checkout=paths.checkout_root()
            if not checkout:raise ValueError('热更新需要源码可编辑安装，请在项目根目录执行 python -m pip install -e . 后重试')
            reload_dir=checkout/'python'/'vivideo'
        check_bind(args.host,args.port)
        commands=[]
        if args.mode=='zmq':
            if env.get('VIVIDEO_LOCAL_VIDEO','1')!='0':
                missing=tool_report(env)['missing']
                if missing:raise ValueError('缺少媒体依赖：'+', '.join(missing)+'。请安装系统工具或使用 --tools-dir；可运行 vivideo doctor 检查。')
            commands.append(('对话 Worker',[sys.executable,'-m','vivideo.worker']))
            if env.get('VIVIDEO_LOCAL_VIDEO','1')!='0':
                commands.append(('下载 Worker',[sys.executable,'-m','vivideo.download_worker']))
        commands.append(('FastAPI',[sys.executable,'-m','uvicorn','vivideo.app:app','--host',args.host,'--port',str(args.port),'--log-level',args.log_level]))
        print('ViVideo 数据目录：'+env['VIVIDEO_DATA_DIR'],flush=True)
        return supervise(commands,env,reload_dir=reload_dir) if reload_dir else supervise(commands,env)
    except (ValueError,OSError) as exc:
        print('启动失败：'+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
