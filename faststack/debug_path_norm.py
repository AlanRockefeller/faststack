
import os
from pathlib import Path

def norm_path(p: Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))

p1 = Path("C:/Test/File.JPG")
p2 = Path("C:/Test/file.jpg")

print(f"p1: {p1}")
print(f"p2: {p2}")
print(f"p1 == p2: {p1 == p2}")

n1 = Path(norm_path(p1))
n2 = Path(norm_path(p2))

print(f"n1: {n1}")
print(f"n2: {n2}")
print(f"n1 == n2: {n1 == n2}")
print(f"n1 == p1: {n1 == p1}")

