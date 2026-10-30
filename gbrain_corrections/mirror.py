"""Coordinator-side mirror and correction priority, staged for joint activation.

Caller holds lower_priority_write across remember, readback AND mirror insert.
Ledger reads are allowed only after the recorded caught-up activation condition.
No credential access, AI, connection creation or ledger writes in this module.
"""
import json
from .corrections import TIERS, unsafe, key


def mirror_verified(journal, row, redact):
    row = dict(row)
    if row.get('status') != 'verified': return 'not_verified'
    f = json.loads(row['payload'])
    if (f.get('tier') not in TIERS or f.get('kind') not in {'fact','preference','belief'} or
        not isinstance(f.get('entity'),str) or not f['entity'].strip() or
        not isinstance(f.get('attribute'),str) or not f['attribute'].strip()): return 'no_standing_key'
    if not row.get('remote_id') or not row.get('receipt'): return 'missing_receipt'
    receipt = json.loads(row['receipt'])
    if str(receipt.get('id')) != str(row['remote_id']) or not receipt.get('entity_slug'):
        raise ValueError('mirror_receipt_mismatch')
    from .safety_filters import EXCLUDED
    if (unsafe(' '.join(f[k] for k in ('entity','attribute','fact')),redact) or
        EXCLUDED.search(str(f.get('source_ref','')))): return 'filtered'
    # Use exact backfill rendering; do NOT normalize its date or remote fact text.
    remote = '[tier:'+f['tier']+'] As of '+str(f.get('date') or 'unknown')+': '+f['fact']
    prior = journal.db.execute('SELECT * FROM facts WHERE id=?', (row['hash'],)).fetchone()
    if prior:
        if (prior['remote_id'] != str(row['remote_id']) or prior['remote_fact'] != remote or
            (prior['entity'],prior['attribute']) != key(f['entity'],f['attribute'])):
            raise ValueError('mirror_seed_changed')
        return 'existing'
    if journal.protects(f['entity'],f['attribute'],f['tier']): return 'protected'
    journal.add_seed(id=row['hash'],entity=f['entity'],attribute=f['attribute'],fact=f['fact'],
        tier=f['tier'],source='backfill',date=f.get('date') or 'unknown',
        remote_id=row['remote_id'],entity_slug=receipt['entity_slug'],
        provenance={'source_ref':f.get('source_ref'),'source_hash':f.get('source_hash')},remote_fact=remote)
    return 'added'


def sync_verified(journal, ledger, redact):
    """Run under shared process lock; ledger is coordinator's existing connection."""
    counts = {}
    for row in ledger.execute("SELECT * FROM facts WHERE status='verified'"):
        result = mirror_verified(journal,row,redact)
        counts[result] = counts.get(result,0)+1
    # Tombstone externally superseded mirrors rather than letting old seeds match.
    for row in ledger.execute("SELECT hash FROM facts WHERE status='superseded'"):
        with journal.db:
            journal.db.execute("UPDATE facts SET superseded_by='backfill-superseded' WHERE id=? AND superseded_by IS NULL AND source='backfill'", (row['hash'],))
    return counts
