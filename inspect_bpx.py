
from bpx.public import Public

public = Public()

print("\n--- Public Methods ---")
for method in dir(public):
    if not method.startswith('_'):
        print(method)
