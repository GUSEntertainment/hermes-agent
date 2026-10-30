"""Official seven-verb HTTP adapter; uses the existing launchd server only.

Client is injected from the admitted mcp_client.Client. This module never opens
PGLite, reads credentials, creates grants, or starts a second server.
"""
import json


def unpack(result):
    if result.get('isError'):
        raise RuntimeError('gbrain_tool_error')
    if isinstance(result.get('structuredContent'), dict):
        return result['structuredContent']
    if 'content' in result:
        chunks = [c['text'] for c in result['content'] if c.get('type') == 'text']
        return json.loads('\n'.join(chunks))
    return result


def fact_text(p):
    return '[tier:'+p['tier']+'] As of '+p['date'][:10]+': '+p['fact']


class HTTPTransport:
    def __init__(self, clients):
        # Realm-to-client routing is process-owned, never model-controlled.
        self.clients = clients

    def call(self, tier, name, args):
        return unpack(self.clients[tier].call(name, args))

    def remember(self, p):
        provenance = json.dumps({k:p[k] for k in (
            'source','date','original_message_ref','event_id','entity','attribute','tier')},
            separators=(',', ':'))
        # Do not truncate the original message ref and pretend it was retained.
        if len(provenance) > 500:
            raise ValueError('provenance_exceeds_remote_schema')
        result = self.call(p['tier'], 'remember', {
            'fact':fact_text(p), 'entity':p['entity'], 'kind':'fact',
            'visibility':'world', 'provenance':provenance})
        if result.get('status') not in {'inserted','duplicate','superseded'}:
            raise RuntimeError('unconfirmed_write')
        return result

    def _facts(self, p, receipt=None, history=False):
        result = self.call(p['tier'], 'recall', {
            'entity':receipt['entity_slug'] if receipt else p['old_entity_slug'],
            'include_expired':history, 'limit':100})
        facts = result.get('facts')
        if not isinstance(facts, list):
            raise RuntimeError('invalid_recall')
        # Never declare an absent/unique fact from a silently truncated page.
        if result.get('has_more') or len(facts) >= 100 or result.get('total', len(facts)) > len(facts):
            raise RuntimeError('recall_requires_pagination')
        return facts

    @staticmethod
    def _id(f):
        return str(f.get('fact_id', f.get('id')))

    @staticmethod
    def _owns(f, p):
        prov = f.get('provenance', {})
        try:
            prov = json.loads(prov) if isinstance(prov, str) else prov
        except (ValueError, TypeError):
            return False
        return isinstance(prov, dict) and prov.get('event_id') == p['event_id']

    def reconcile(self, p):
        matches = [f for f in self._facts(p, history=True)
                   if f.get('fact') == fact_text(p) and self._owns(f, p) and not f.get('expired_at')]
        if len(matches) != 1:
            return None
        f = matches[0]
        return {'id':self._id(f), 'entity_slug':f['entity_slug'], 'status':'reconciled'}

    def verify_new(self, p, receipt):
        found = [f for f in self._facts(p, receipt) if self._id(f) == str(receipt['id'])
                 and f.get('fact') == fact_text(p) and self._owns(f, p) and not f.get('expired_at')]
        if len(found) != 1 or str(receipt['id']) == p['old_remote_id']:
            raise RuntimeError('new_fact_readback_failed')

    def verify_old(self, p, *, allow_expired=False):
        old = [f for f in self._facts(p, history=True) if self._id(f) == p['old_remote_id']]
        if (len(old) != 1 or old[0].get('fact') != p['old_fact'] or
            old[0].get('entity_slug') != p['old_entity_slug'] or
            not p['old_fact'].startswith('[tier:'+p['tier']+'] ')):
            raise RuntimeError('old_fact_identity_mismatch')
        if old[0].get('expired_at') and not allow_expired:
            raise RuntimeError('old_fact_already_superseded')
        return old[0]

    def supersede(self, p, receipt):
        self.verify_old(p, allow_expired=True)
        history = self._facts(p, history=True)
        old = [f for f in history if self._id(f) == p['old_remote_id']]
        if len(old) != 1:
            raise RuntimeError('old_fact_missing')
        if not old[0].get('expired_at'):
            self.call(p['tier'], 'forget', {
                'id':p['old_remote_id'],
                'reason':'superseded_by='+str(receipt['id'])+'; source=chris_correction; valid_until='+p['date']})

    def verify_history(self, p, receipt):
        facts = self._facts(p, history=True)
        old = [f for f in facts if self._id(f) == p['old_remote_id']]
        if len(old) != 1 or not old[0].get('expired_at'):
            raise RuntimeError('old_fact_not_superseded')
        active = self._facts(p, receipt)
        if any(self._id(f) == p['old_remote_id'] for f in active):
            raise RuntimeError('superseded_fact_still_active')
        self.verify_new(p, receipt)
