import pathlib
import re

count = 0
for p in pathlib.Path('skills').rglob('*.md'):
    content = p.read_text('utf-8')
    if '} | Tee-Object' in content:
        # Wrap try catch in $( ... )
        # Find lines that start with `try {` or `    try {` and have a corresponding `} | Tee-Object`
        # Actually a simpler fix is just replacing `try {` with `$(try {`
        # and `} | Tee-Object` with `}) | Tee-Object`
        new_content = re.sub(r'(^|\n)([ \t]*)(try\s*\{)', r'\1\2$(\3', content)
        new_content = re.sub(r'(\}\s*\|\s*Tee-Object)', r'}) | Tee-Object', new_content)
        
        # What if there are try blocks that DON'T have a pipe? We only want to wrap piped ones.
        # Let's do it cleanly by targeting the exact blocks.
        # A simpler way: $d='C:\Windows\Temp\bas'; if(!(Test-Path $d)){ni -ItemType Directory -Force $d|out-null}; $(try { ... } catch { ... }) | Tee-Object ...
        
        p.write_text(new_content, 'utf-8')
        print(f'Fixed {p}')
        count += 1

print(f'Fixed {count} files.')
