"""Portable paths and process supervision, using temporary files and local children."""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vivideo import cli, paths, storage
from vivideo.model_configs import database as settings_database


class PortableStartupTests(unittest.TestCase):
    def test_checkout_default_and_explicit_directory(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(paths.data_root(),paths.checkout_root()/'data')
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'VIVIDEO_DATA_DIR':directory}):
            self.assertEqual(paths.data_root(),Path(directory).resolve())
            with settings_database() as db:db.execute('SELECT 1')
            storage.create_session('learning')
            self.assertTrue((Path(directory)/'settings.db').is_file())
            self.assertTrue((Path(directory)/'spaces/learning/space.db').is_file())

    def test_installed_package_uses_user_data_directory(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True),patch('vivideo.paths.checkout_root',return_value=None),patch('vivideo.paths.user_data_path',return_value=Path(directory)/'vivideo'):
            self.assertEqual(paths.data_root(),Path(directory)/'vivideo')
            self.assertIsNone(paths.bundled_tool('ffmpeg'))

    def test_cli_shares_absolute_paths_and_current_interpreter(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'VIVIDEO_DATA_DIR':directory}):
            args=cli.parser().parse_args(['serve','--mode','demo','--data-dir',directory,'--port','8123'])
            env=cli.environment(args)
            self.assertEqual(env['VIVIDEO_DATA_DIR'],str(Path(directory).resolve()))
            self.assertEqual(env['VIVIDEO_MODE'],'demo')
            with patch('vivideo.cli.check_bind'),patch('vivideo.cli.supervise',return_value=0) as run:
                self.assertEqual(cli.main(['serve','--mode','demo','--data-dir',directory,'--port','8123']),0)
            commands=run.call_args[0][0]
            self.assertEqual(len(commands),1)
            self.assertEqual(commands[0][1][0],sys.executable)

    def test_doctor_reports_missing_dependencies_without_starting_services(self):
        with tempfile.TemporaryDirectory() as directory,patch('vivideo.cli.tool_report',return_value={'missing':['ffmpeg']}),patch('vivideo.cli.supervise') as run:
            self.assertEqual(cli.main(['doctor','--data-dir',directory]),1)
            run.assert_not_called()

    def test_occupied_port_does_not_launch_any_worker(self):
        with socket.socket() as occupied,tempfile.TemporaryDirectory() as directory:
            occupied.bind(('127.0.0.1',0));occupied.listen();port=occupied.getsockname()[1]
            with patch('vivideo.cli.supervise') as run:
                self.assertEqual(cli.main(['serve','--mode','demo','--port',str(port),'--data-dir',directory]),1)
                run.assert_not_called()

    def test_reload_requires_editable_source(self):
        with tempfile.TemporaryDirectory() as directory,patch('vivideo.cli.paths.checkout_root',return_value=None),patch('vivideo.cli.supervise') as run:
            self.assertEqual(cli.main(['serve','--mode','demo','--reload','--data-dir',directory]),1)
            run.assert_not_called()

    def test_source_watcher_ignores_assets_data_and_bytecode(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);module=root/'app.py';module.write_text('version=1')
            original=cli.source_snapshot(root)
            (root/'style.css').write_text('body{}');(root/'space.db').write_bytes(b'data')
            cache=root/'__pycache__';cache.mkdir();(cache/'extra.py').write_text('ignored')
            self.assertEqual(cli.source_snapshot(root),original)
            module.write_text('version=222')
            self.assertNotEqual(cli.source_snapshot(root),original)
            module.unlink();self.assertEqual(cli.source_snapshot(root),{})

    @unittest.skipUnless(os.name=='posix','POSIX process supervision')
    def test_early_exit_stops_sibling_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile=Path(directory)/'child.pid'
            sleeper=[sys.executable,'-c','import os,sys,time;open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(60)',str(pidfile)]
            failed=[sys.executable,'-c','import time;time.sleep(.6);raise SystemExit(7)']
            code=cli.supervise([('sleeper',sleeper),('failed',failed)],dict(os.environ))
            self.assertEqual(code,7)
            pid=int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
