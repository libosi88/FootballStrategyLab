"""Cooperative directory-metadata sampling; no content hashing or unsafe size guesses."""
from pathlib import Path
import os,time
from .mining import Paused

def sample_output_size(root,should_pause=None):
    root=Path(root).resolve();stack=[root];seen=set();total=count=0;started=time.monotonic()
    while stack:
        directory=stack.pop()
        if directory in seen:continue
        seen.add(directory)
        if should_pause and should_pause():raise Paused()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child=Path(entry.path).resolve()
                            if root in child.parents:stack.append(child)
                        elif entry.is_file(follow_symlinks=False):
                            total+=entry.stat(follow_symlinks=False).st_size;count+=1
                            if count%512==0 and should_pause and should_pause():raise Paused()
                    except FileNotFoundError:pass
        except FileNotFoundError:pass
    return {'generated_bytes':total,'resource_files_sampled':count,'resource_scan_seconds':round(time.monotonic()-started,4),'resource_size_method':'scandir_metadata_snapshot'}
