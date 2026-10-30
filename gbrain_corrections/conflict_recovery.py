"""Reconcile a checkpointed lower-priority remote write; never blindly retry."""
import json


def reconcile_conflict(client, row, fact):
    def call(name, args):
        result = client.call(name, args)
        if isinstance(result, dict) and 'facts' in result:
            return result
        from .http_transport import unpack
        return unpack(result)
    text = '[tier:'+fact['tier']+'] As of '+str(fact.get('date') or 'unknown')+': '+fact['fact']
    matches = [r for r in call('recall', {'query':text, 'grep':text,
               'include_expired':True, 'limit':100}).get('facts', []) if r.get('fact') == text]
    if row['remote_id']:
        matches = [r for r in matches if str(r.get('fact_id',r.get('id'))) == str(row['remote_id'])]
    if len(matches) != 1:
        raise RuntimeError('conflicting_write_requires_reconciliation')
    remote = matches[0]
    provenance = remote.get('provenance', {})
    provenance = json.loads(provenance) if isinstance(provenance, str) else provenance
    if (not isinstance(provenance, dict) or provenance.get('source') == 'chris_correction' or
            not ((fact.get('source_hash') and provenance.get('sha256') == fact['source_hash']) or
                 provenance.get('fact_key') == row['hash'])):
        raise RuntimeError('conflicting_write_identity_unverified')
    remote_id = str(remote.get('fact_id',remote.get('id')))
    if not remote.get('expired_at'):
        call('forget', {'id':remote_id,'reason':'Superseded by explicit Chris correction; preserve history.'})
    history = call('recall', {'entity':remote['entity_slug'], 'grep':text,
                            'include_expired':True, 'limit':100}).get('facts', [])
    if not any(str(r.get('fact_id',r.get('id'))) == remote_id and
               r.get('expired_at') for r in history):
        raise RuntimeError('conflicting_write_expiration_unverified')
    return {'id':remote_id,'entity_slug':remote['entity_slug'],'status':'expired_conflict'}
