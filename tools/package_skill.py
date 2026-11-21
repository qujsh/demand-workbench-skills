"""Package an explicit public-file whitelist, never local runtime state."""
from pathlib import Path
import hashlib
import zipfile

root = Path(__file__).resolve().parents[1]
name = 'demand-workbench-archive'
files = ['SKILL.md', 'agents/openai.yaml', 'references/setup.md', 'references/numbered-journal.md', 'references/native-edit.md',
         'scripts/archive.py', 'scripts/availability.py', 'scripts/mcp_client.py', 'scripts/journal.py', 'scripts/numbered_journal.py', 'scripts/native_edit.py']
out = root / 'dist'
out.mkdir(exist_ok=True)
archive = out / (name + '.zip')
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
    for relative in files:
        bundle.write(root / 'skills' / name / relative, name + '/' + relative)
    bundle.write(root / 'LICENSE', name + '/LICENSE')
(out / 'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
print(archive)
