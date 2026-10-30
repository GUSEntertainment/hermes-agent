"""Fail-safe retention of native artifact bytes; never copies or grants access.

A durable attachment elsewhere is not proof that the accepted reference was
retargeted or handed off. Keep referenced workspace bytes until an explicit
operator purge or a separately implemented verified retention release.
"""
import json
from pathlib import Path


def retains_artifact(conn, task_id):
    try:
        row = conn.execute('SELECT workspace_path FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or not row[0]:
            return False
        root = Path(row[0]).expanduser()
        if not root.is_absolute():
            return True
        root = root.resolve(strict=False)

        def inside(ref):
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError('Malformed artifact reference')
            p = Path(ref)
            # Non-file evidence (inline text, URLs) cannot be workspace bytes.
            if not p.is_absolute():
                return False
            # Preserve both lexical and resolved containment (including symlinks).
            return p.is_relative_to(root) or p.resolve(strict=False).is_relative_to(root)

        def related(workspace):
            return bool(workspace) and inside(workspace)

        for ref, digest, workspace in conn.execute('SELECT g.review_artifact_ref,g.review_artifact_digest,t.workspace_path FROM owner_cell_goals g JOIN tasks t ON t.id=g.task_id'):
            if ref is None and digest is None:
                continue
            if isinstance(ref,str) and ref.strip() and inside(ref):return True
            if not isinstance(ref,str) or not ref.strip() or not isinstance(digest,str) or not digest.strip():
                if related(workspace):return True
                continue
        for raw, workspace in conn.execute('SELECT r.metadata,t.workspace_path FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE r.metadata IS NOT NULL'):
            try:
                value=json.loads(raw)
                if not isinstance(value,dict):raise ValueError('Malformed run metadata')
                # Known references protect bytes even if a sibling field is corrupt.
                artifacts=value.get('artifacts')
                artifact=value.get('owner_cell_artifact')
                refs=list(artifacts) if isinstance(artifacts,list) else []
                if isinstance(artifact,dict):refs.append(artifact.get('ref'))
                if any(isinstance(ref,str) and ref.strip() and inside(ref) for ref in refs):return True
                if 'artifacts' in value:
                    if not isinstance(artifacts,list):raise ValueError('Malformed artifacts list')
                    for ref in artifacts:inside(ref)
                if 'owner_cell_artifact' in value:
                    if not isinstance(artifact,dict) or not artifact.get('digest'):raise ValueError('Malformed artifact metadata')
                    inside(artifact.get('ref'))
            except (ValueError,TypeError):
                if related(workspace):return True
        for (ref,) in conn.execute('SELECT stored_path FROM task_attachments'):
            if inside(ref):
                return True
        return False
    except (ValueError,TypeError,OSError,KeyError):
        return True
    except Exception:
        # Missing/corrupt schema or a failed read cannot authorize deletion.
        return True
