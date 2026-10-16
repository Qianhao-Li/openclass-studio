"""Verify that the standalone source has no dependency on private platform code."""
import ast
import json
from pathlib import Path
import re

root=Path(__file__).resolve().parents[1]
package=root/'apps/api/openclass_core'
if not package.is_dir():raise SystemExit('Run this check in the standalone Studio checkout.')
forbidden={'auth_store','auth_service','account_settings','billing_service','openrouter_provisioning',
           'repository_project_store','course_pr_store','project_collaboration','platform_ai_execution','codex_text_proxy'}
issues=[]
files=list(package.rglob('*.py'))
for file in files:
    if file.stem in forbidden:issues.append(str(file.relative_to(root))+': private module')
    for node in ast.walk(ast.parse(file.read_text())):
        names=[]
        if isinstance(node,ast.Import):names=[name.name for name in node.names]
        elif isinstance(node,ast.ImportFrom):
            names=[node.module or '',*[(node.module or '')+'.'+name.name for name in node.names]]
        for name in names:
            if name=='app' or name.startswith('app.') or name.startswith('openclass_core.') and name.split('.')[-1] in forbidden:
                issues.append(f'{file.relative_to(root)}:{node.lineno}: private import')
for file in (root/'packages/studio-ui/src').rglob('*'):
    if file.suffix in {'.ts','.tsx'} and re.search(r'from\s+["\']@/',file.read_text()):
        issues.append(str(file.relative_to(root))+': host alias in shared UI')
if issues:raise SystemExit('\n'.join(issues))
print(json.dumps({'portable_python_modules':len(files),'private_dependencies':0}))
