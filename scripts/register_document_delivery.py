#!/usr/bin/env python3
"""Installed operator broker. Input is an opaque persisted app request ID only."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys


def main():
    if len(sys.argv)!=2:
        return {'state':'rejected','code':'invalid_request'}
    # This script is installed INSIDE the immutable reviewed native release.
    # -I disables external PYTHONPATH; no runtime path comes from the request.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    try:
        with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
            from gateway.secondary_documents import register,resolve_request,Held
            from gateway.app_business_authority import ROOT,protected
            authority=resolve_request(sys.argv[1])
            db=ROOT/'kanban'/'boards'/authority['board']/'kanban.db'
            before=protected(db,ROOT)
            with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=rw',uri=True,timeout=1,isolation_level=None)) as c:
                c.row_factory=sqlite3.Row
                after=protected(db,ROOT)
                if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise Held('Native source replaced')
                register(c,sys.argv[1])
        return {'state':'registered','code':'native_intent_registered'}
    except PermissionError:
        return {'state':'held','code':'current_authority_or_policy_unavailable'}
    except Exception:
        return {'state':'held','code':'native_registration_unavailable'}

if __name__=='__main__':
    print(json.dumps(main(),separators=(',',':')))
