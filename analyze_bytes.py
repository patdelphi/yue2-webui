"""Analyze the raw bytes of the JS section in app.py to understand escaping."""

with open('app.py', 'rb') as f:
    raw = f.read()

# Find the script section
script_start = raw.find(b'<script>\n', raw.find(b'head=')) + len(b'<script>\n')
script_end = raw.find(b'\n        </script>', script_start)
js_bytes = raw[script_start:script_end]

# Find all split() calls
idx = 0
while True:
    idx = js_bytes.find(b'split(', idx)
    if idx < 0:
        break
    snippet = js_bytes[idx:idx+20]
    print(f"Offset {idx}: {snippet!r}")
    # Show hex of bytes inside the quotes
    quote_start = js_bytes.find(b"'", idx)
    quote_end = js_bytes.find(b"'", quote_start + 1)
    if quote_start >= 0 and quote_end >= 0:
        inside = js_bytes[quote_start+1:quote_end]
        print(f"  Inside quotes: {inside!r} (hex: {inside.hex()})")
    idx += 1

print("\n--- Checking \\\\n patterns ---")
# Find \\n in raw bytes (should be 5c 5c 6e)
idx = 0
count = 0
while count < 5:
    idx = js_bytes.find(b'\\\\n', idx)
    if idx < 0:
        break
    context = js_bytes[max(0,idx-10):idx+15]
    print(f"Offset {idx}: {context!r}")
    idx += 1
    count += 1

print("\n--- Checking single backslash + n (5c 6e) ---")
# Find \n in raw bytes that are NOT \\n (should be 5c 6e not preceded by 5c)
import re
for m in re.finditer(rb'(?<!\\)\\n', js_bytes):
    pos = m.start()
    context = js_bytes[max(0,pos-10):pos+15]
    print(f"Offset {pos}: {context!r}")
    if pos > 100:
        break
