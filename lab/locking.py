"""Process-lifetime local workspace mutex. Not a network/distributed lock."""
import os, errno
from pathlib import Path

class WorkspaceBusy(RuntimeError):
    pass

class WorkspaceLock:
    def __init__(self,workspace,filename='.worker.lock'):
        if Path(filename).name!=filename:raise ValueError('锁文件名不能包含目录')
        self.path=Path(workspace)/filename;self.handle=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        f=self.path.open('a+b');f.seek(0,os.SEEK_END)
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as ex:
            f.close()
            if ex.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):
                raise WorkspaceBusy('同一工作区已有计算进程，当前任务保持排队') from ex
            raise
        self.handle=f
        return self
    def __exit__(self,*exc):
        if self.handle:
            try:
                self.handle.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN)
            finally:self.handle.close();self.handle=None

MAX_WORKER_SLOTS=8

def slot_filename(index):
    return '.worker.lock' if index==0 else f'.worker_{index}.lock'

class WorkerSlot:
    """First free compute slot among the workspace's parallel limit. Slot 0 is the historical
    single-worker lock, so a limit of 1 behaves exactly like the former workspace mutex."""
    def __init__(self,workspace,limit=1):
        self.workspace=workspace;self.limit=max(1,min(MAX_WORKER_SLOTS,int(limit)));self.lock=None;self.index=None
    def __enter__(self):
        for index in range(self.limit):
            lock=WorkspaceLock(self.workspace,slot_filename(index))
            try:lock.__enter__()
            except WorkspaceBusy:continue
            self.lock=lock;self.index=index
            return self
        raise WorkspaceBusy('全部计算槽位均在使用，当前任务保持排队')
    def __exit__(self,*exc):
        if self.lock:self.lock.__exit__(*exc);self.lock=None

class AllWorkerSlots:
    """Exclusive maintenance access: holds every possible compute slot, whatever the current limit."""
    def __init__(self,workspace):
        self.workspace=workspace;self.locks=[]
    def __enter__(self):
        try:
            for index in range(MAX_WORKER_SLOTS):
                lock=WorkspaceLock(self.workspace,slot_filename(index));lock.__enter__();self.locks.append(lock)
        except BaseException:
            self.__exit__(None,None,None);raise
        return self
    def __exit__(self,*exc):
        while self.locks:self.locks.pop().__exit__(None,None,None)
