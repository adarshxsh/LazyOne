import sys
import os

print("--- Python Environment Diagnostic ---")
print(f"Executable Path: {sys.executable}")
print("\nLibrary Search Paths (sys.path):")
for path in sys.path:
    print(path)
print("--- End of Diagnostic ---")
