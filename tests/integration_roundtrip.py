"""Explicit real-cloud acceptance; never imported by unittest discovery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_space as c


def main():
    parser = argparse.ArgumentParser(description='Creates a real cloud test archive; leaves its cloud copy intact')
    parser.add_argument('--live', action='store_true', required=True)
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='project-cloud-space-test-') as temp:
        root = Path(temp)
        c.STATE = root / '.state'
        c.STATE.mkdir()
        c.REGISTRY = c.STATE / 'projects.json'
        project_root = root / 'integration-project'
        project_root.mkdir()
        project = c.register(project_root)
        source = Path(project['cloud']) / 'nested' / 'sample.bin'
        source.parent.mkdir()
        content = os.urandom(8 * 1024 * 1024 + 123)
        digest = hashlib.sha256(content).hexdigest()
        source.write_bytes(content)
        client = c.Cloud()
        try:
            record = client.archive(project, source)
            assert not source.exists()
            assert record['sha256'] == digest and record.get('cloud_index')
            assert any(p['remote'] == project['remote'] for p in client.cloud_projects())
            # A fresh state directory has neither projects.json nor local receipts.
            c.STATE = root / 'fresh-state'
            c.STATE.mkdir()
            c.REGISTRY = c.STATE / 'projects.json'
            assert c.projects() == []
            target = root / 'recovered'
            report = client.recover_project(project['remote'], target)
            assert report['state'] == 'complete' and len(report['completed']) == 1
            restored = target / 'nested' / 'sample.bin'
            assert hashlib.sha256(restored.read_bytes()).hexdigest() == digest
            mtime = restored.stat().st_mtime_ns
            again = client.recover_project(project['remote'], target)
            assert again['state'] == 'complete' and restored.stat().st_mtime_ns == mtime
            print(json.dumps({'pass': True, 'bytes': len(content), 'sha256': digest,
                              'remote_test_project': project['remote'],
                              'fresh_state_recovery': True, 'repeat_recovery_skips': True}, ensure_ascii=False))
        finally:
            client.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'pass': False, 'error': c.safe_error(exc)}, ensure_ascii=False))
        sys.exit(1)
