"""Fixed local Node adapter, server configuration only. Protected operator paths only."""
import json
import pathlib
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
from .errors import Denied
class NodeService:
    def __init__(self,*,registry_file,form_directory,node,origin,secure_core):
        from .paths import private_file,private_directory,code_file
        registry_file=pathlib.Path(registry_file);form_directory=pathlib.Path(form_directory)
        private_file(registry_file);private_directory(form_directory);code_file(node);code_file(secure_core)
        parsed=urlsplit(origin)
        if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:raise Denied('Configured HTTPS origin required')
        node=pathlib.Path(node)
        if not node.is_absolute() or not node.is_file():raise Denied('Configured Node required')
        self.core=str(secure_core)
        self.node,self.origin,self.registry,self.forms=str(node),origin,str(registry_file),str(form_directory)
    def prepare(self,identity):
        result=subprocess.run([self.node,'--experimental-strip-types',str(pathlib.Path(__file__).with_name('node_intake_service.mjs'))],input=json.dumps(identity),text=True,capture_output=True,timeout=15,env={'SECURE_PHOTON_REGISTRY_FILE':self.registry,'SECURE_RESERVED_FORM_DIRECTORY':self.forms,'NODE_NO_WARNINGS':'1','SECURE_RESOLVER_CORE':self.core})
        if result.returncode or len(result.stdout)>8192:raise Denied('Form unavailable')
        return json.loads(result.stdout)
    def verified_url(self,metadata):
        # Node returns relative path; configured origin is never model-selected.
        if set(metadata)!={'owner','tenant','request','service','purpose','expires','id','url'} or not isinstance(metadata.get('id'),str) or not re.fullmatch(r'[a-f0-9-]{36}',metadata['id']) or metadata.get('url')!='/secure/'+metadata['id']:raise Denied('Unexpected metadata')
        return self.origin+metadata['url']
