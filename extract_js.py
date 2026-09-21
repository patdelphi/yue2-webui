"""Extract inline JS from app.py head parameter into static/js/app.js.

Escaping rule: In the Python source, \\ (two backslash bytes) represents a single
backslash in the Python string, which should be a single backslash in JS source.
So we replace every pair of backslash bytes (5c 5c) with a single backslash (5c).
"""

with open('app.py', 'rb') as f:
    raw = f.read()

head_pos = raw.find(b'head=')
script_start_marker = b'<script>\n'
script_end_marker = b'\n        </script>'

start = raw.find(script_start_marker, head_pos) + len(script_start_marker)
end = raw.find(script_end_marker, start)

js_bytes = raw[start:end]

# Convert: \\(5c 5c) -> \(5c) throughout
js_converted = js_bytes.replace(b'\\\\', b'\\')

# Verify the conversion on known patterns
assert b"marker = '\\n['" in js_converted, "marker conversion failed"
assert b"split('\\n')" in js_converted, "split conversion failed"
assert b"/\\s/g" in js_converted, "regex conversion failed"

with open('static/js/app.js', 'wb') as f:
    f.write(js_converted)

print(f"Extracted {len(js_bytes)} bytes -> {len(js_converted)} bytes")
print(f"Removed {len(js_bytes) - len(js_converted)} backslash bytes")

# Show a few lines to verify
lines = js_converted.decode('utf-8').split('\n')
for i, line in enumerate(lines):
    if 'marker' in line and 'const' in line:
        print(f"  Line {i}: {line.strip()}")
    if 'split(' in line:
        print(f"  Line {i}: {line.strip()}")
    if 'replace(/' in line:
        print(f"  Line {i}: {line.strip()}")
