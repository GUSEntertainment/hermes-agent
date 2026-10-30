// Fixed server-only stage entry. No broad API key, secrets or model-selected path.
import {pathToFileURL} from 'node:url';
let input='';
try {
 for await (const chunk of process.stdin) {input+=chunk;if(input.length>4096)throw Error('bounded request required');}
 const identity=JSON.parse(input);
 const {PhotonSecureResolver}=await import(pathToFileURL(process.env.SECURE_RESOLVER_CORE).href);
 const service=new PhotonSecureResolver({registryFile:process.env.SECURE_PHOTON_REGISTRY_FILE,formDirectory:process.env.SECURE_RESERVED_FORM_DIRECTORY,currentIngress:()=>identity});
 try{process.stdout.write(JSON.stringify(service.reserve()));}finally{service.close();}
} catch {process.stdout.write(JSON.stringify({error:'Form unavailable'}));process.exitCode=1;}
