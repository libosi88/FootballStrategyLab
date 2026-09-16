import os,sys,time,tempfile,subprocess,unittest,json
from pathlib import Path
from lab.common import atomic_json,atomic_replace


@unittest.skipUnless(os.name=='nt','Windows file sharing')
class WindowsRealReaderLock(unittest.TestCase):
    def test_one_and_half_second_real_handle_lock_does_not_break_publication(self):
        code='''import ctypes,sys,time
from ctypes import wintypes
k=ctypes.WinDLL("kernel32",use_last_error=True)
k.CreateFileW.argtypes=(wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
k.CreateFileW.restype=wintypes.HANDLE
k.CloseHandle.argtypes=(wintypes.HANDLE,)
h=k.CreateFileW(sys.argv[1],0x80000000,3,None,3,0,None)
if h==wintypes.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
print("LOCKED",flush=True)
time.sleep(1.5)
k.CloseHandle(h)
'''
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for use_json in (False,True):
                target=root/('state.json' if use_json else 'cache.bin');target.write_text('{"old":true}',encoding='utf-8')
                process=subprocess.Popen([sys.executable,'-B','-c',code,str(target)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    self.assertEqual(process.stdout.readline().strip(),'LOCKED')
                    started=time.monotonic()
                    if use_json:atomic_json(target,{'new':'完整状态'})
                    else:
                        pending=root/'cache.tmp';pending.write_bytes(b'complete-cache');atomic_replace(pending,target,timeout=5)
                    elapsed=time.monotonic()-started
                    self.assertGreater(elapsed,1.2)
                    if use_json:self.assertEqual(json.loads(target.read_text(encoding='utf-8')),{'new':'完整状态'})
                    else:self.assertEqual(target.read_bytes(),b'complete-cache')
                    self.assertEqual(process.wait(timeout=5),0)
                finally:
                    process.wait(timeout=5);process.stdout.close();process.stderr.close()


if __name__=='__main__':unittest.main()
